"""Single-repository deterministic UI AST & binary metadata scanner.

100% Python standard library. Produces canonical `.snapshot.json.gz` payloads.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from autowork_core.ui_inventory.scanner import SCANNER_SCHEMA_VERSION
from autowork_core.ui_inventory.scanner.csharp_wiring import scan_csharp_file
from autowork_core.ui_inventory.scanner.pe_baml_reader import (
    parse_dot_resources_strings,
    parse_managed_dll,
)
from autowork_core.ui_inventory.scanner.resx_config_parser import (
    classify_locale_bucket,
    extract_assembly_xmlns_defs,
    extract_csharp_string_constants,
    parse_default_config_xml,
    parse_resx_entries,
    split_resx_stem_and_locale,
)
from autowork_core.ui_inventory.scanner.scope_gate import (
    decode_text_bytes,
    discover_shipping_scope,
    is_build_intermediate_source_path,
    is_non_shipping_path,
    iter_reference_dll_files,
    iter_repo_files,
    match_owning_project,
    matches_exclude_patterns,
    sha256_bytes,
)
from autowork_core.ui_inventory.scanner.winforms_parser import parse_winforms_designer
from autowork_core.ui_inventory.scanner.xaml_parser import parse_xaml_file


MAX_MANAGED_UI_DLL_BYTES = 32 * 1024 * 1024  # 32 MiB ceiling skips 100MB+ native CUDA/MKL binaries
SKIP_DLL_PREFIXES = (
    "system.",
    "microsoft.",
    "mscorlib",
    "netstandard",
    "windowsbase",
    "presentationcore",
    "presentationframework",
    "newtonsoft.",
    "nlog",
    "log4net",
    "sqlite",
    "entityframework",
    "google.protobuf",
    "grpc.",
    "vtk",
    "itk",
    "opencv",
    "cudart",
    "cublas",
    "cudnn",
    "cufft",
    "mkl_",
    "libiomp",
)


def _detect_git_commit_and_branch(repo_root: Path, explicit_branch: str = "") -> Tuple[str, str]:
    commit = "uncommitted-worktree"
    branch = explicit_branch or "HEAD"
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            commit = proc.stdout.strip()
        if not explicit_branch:
            bproc = subprocess.run(
                ["git", "-c", "safe.directory=*", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            if bproc.returncode == 0 and bproc.stdout.strip():
                branch = bproc.stdout.strip()
    except OSError:
        pass
    return commit, branch


def scan_repository(
    repo_root: Path,
    repo_id: str,
    *,
    branch: str = "",
    commit_override: str = "",
    external_build_lists: Optional[Iterable[Path]] = None,
    exclude_path_patterns: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Scan a single repository into a deterministic, canonical JSON-serializable snapshot dict."""
    root_path = repo_root.resolve()
    commit, resolved_branch = _detect_git_commit_and_branch(root_path, explicit_branch=branch)
    if commit_override:
        commit = commit_override

    excludes = [str(p).strip() for p in (exclude_path_patterns or []) if str(p or "").strip()]
    all_files = [
        (abs_p, rel)
        for abs_p, rel in iter_repo_files(root_path)
        if not matches_exclude_patterns(rel, excludes, repo_id=repo_id)
    ]
    scope = discover_shipping_scope(
        root_path,
        repo_id,
        all_files=all_files,
        external_build_lists=external_build_lists,
    )

    proj_rel_to_asm = {rp.lower(): pi.assembly_name for rp, pi in scope.projects.items()}
    assemblies: List[Dict[str, Any]] = []
    for rel_proj in sorted(scope.projects):
        pinfo = scope.projects[rel_proj]
        ref_asms: Set[str] = set()
        for pref in pinfo.project_refs:
            target_asm = proj_rel_to_asm.get(pref.lower()) or Path(pref).stem
            if target_asm and target_asm != pinfo.assembly_name:
                ref_asms.add(target_asm)
        for aref in pinfo.assembly_refs:
            aref_low = aref.lower()
            if aref_low == "system" or any(aref_low.startswith(pfx) for pfx in SKIP_DLL_PREFIXES):
                continue
            if aref_low.startswith(("uiautomation", "windowsformsintegration", "xunit", "nunit", "nsubstitute", "moq")):
                continue
            if aref != pinfo.assembly_name:
                ref_asms.add(aref)
        for hp in pinfo.hint_dll_paths:
            h_stem = Path(hp).stem
            h_low = h_stem.lower()
            if h_stem and h_low != "system" and not any(h_low.startswith(pfx) for pfx in SKIP_DLL_PREFIXES):
                if h_stem != pinfo.assembly_name:
                    ref_asms.add(h_stem)
        assemblies.append({
            "name": pinfo.assembly_name,
            "kind": pinfo.output_type,
            "csproj": pinfo.rel_path,
            "root_namespace": pinfo.root_namespace,
            "is_shipping": pinfo.is_shipping,
            "ref_assemblies": sorted(ref_asms),
        })

    xmlns_defs: List[Dict[str, str]] = []
    types: List[Dict[str, Any]] = []
    definitions: List[Dict[str, Any]] = []
    elements: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    vm_properties: List[Dict[str, str]] = []
    configs: List[Dict[str, str]] = []
    diagnostics: List[Dict[str, Any]] = []
    pure_icon_dict_count = 0

    # Bilingual string accumulator: (assembly, res_class, key) -> [val_en, val_zh, source_kind]
    # Carrier hierarchy: standalone `flat_resources` > class-scoped `scoped_resources`/`resx` > `resdict` > `cs_const`.
    _STR_SRC_PRIO = {
        "flat_resources": 4,
        "scoped_resources": 3,
        "dot_resources": 3,
        "resx": 3,
        "resdict": 2,
        "cs_const": 1,
    }
    string_map: Dict[Tuple[str, str, str], List[str]] = {}

    def record_string(
        asm: str,
        res_cls: str,
        key: str,
        val_en: str = "",
        val_zh: str = "",
        source_kind: str = "resx",
    ) -> None:
        new_prio = _STR_SRC_PRIO.get(source_kind, 0)
        slot = string_map.get((asm, res_cls, key))
        if slot is None:
            string_map[(asm, res_cls, key)] = [val_en, val_zh, source_kind]
            return
        old_prio = _STR_SRC_PRIO.get(slot[2], 0)
        if new_prio > old_prio:
            slot[0] = val_en or (slot[0] if old_prio >= 2 else "")
            slot[1] = val_zh or (slot[1] if old_prio >= 2 else "")
            slot[2] = source_kind
        elif new_prio == old_prio:
            if val_en and not slot[0]:
                slot[0] = val_en
            if val_zh and not slot[1]:
                slot[1] = val_zh

    seen_dll_hashes: Set[str] = set()

    for abs_path, rel in all_files:
        lower = rel.lower()
        fname_lower = abs_path.name.lower()

        # 1. Managed DLL binary metadata & BAML templates
        if lower.endswith(".dll"):
            if any(fname_lower.startswith(pfx) for pfx in SKIP_DLL_PREFIXES):
                continue
            if is_build_intermediate_source_path(rel) or is_non_shipping_path(rel):
                continue
            try:
                st_size = abs_path.stat().st_size
                if st_size > MAX_MANAGED_UI_DLL_BYTES or st_size < 512:
                    continue
                raw_dll = abs_path.read_bytes()
            except OSError:
                continue
            dll_hash = sha256_bytes(raw_dll)
            if dll_hash in seen_dll_hashes:
                continue
            seen_dll_hashes.add(dll_hash)

            bmeta = parse_managed_dll(raw_dll, rel)
            if bmeta is None:
                continue
            xmlns_defs.extend(bmeta.xmlns_defs)
            types.extend(bmeta.types)
            definitions.extend(bmeta.baml_definitions)
            elements.extend(bmeta.baml_elements)
            edges.extend(bmeta.baml_edges)
            for s in bmeta.strings:
                record_string(
                    s["assembly"], s["res_class"], s["key"],
                    val_en=s["val_en"], val_zh=s["val_zh"], source_kind="scoped_resources",
                )
            diagnostics.extend(bmeta.diagnostics)
            continue

        if is_build_intermediate_source_path(rel):
            continue

        owning_proj = match_owning_project(rel, scope)
        asm_name = owning_proj.assembly_name if owning_proj else repo_id
        root_ns = owning_proj.root_namespace if owning_proj else repo_id
        file_is_shipping = (
            (owning_proj.is_shipping if owning_proj is not None else True)
            and not is_non_shipping_path(rel)
        )

        # 2. Standalone compiled `.resources` flat localization tables (e.g. `String-en-US.resources`, `String-zh-CHS.resources`)
        if lower.endswith(".resources") and not lower.endswith(".g.resources"):
            if not file_is_shipping:
                continue
            try:
                st_size = abs_path.stat().st_size
                if st_size > 16 * 1024 * 1024 or st_size < 28:
                    continue
                raw_res = abs_path.read_bytes()
            except OSError:
                continue
            res_cls, locale = split_resx_stem_and_locale(abs_path.stem)
            bucket = classify_locale_bucket(locale, rel)
            if bucket not in ("en", "zh"):
                continue
            entries = parse_dot_resources_strings(raw_res)
            for k, v in entries.items():
                if bucket == "zh":
                    record_string(asm_name, res_cls, k, val_zh=v, source_kind="flat_resources")
                else:
                    record_string(asm_name, res_cls, k, val_en=v, source_kind="flat_resources")
            continue

        # 3. WPF XAML files
        if lower.endswith(".xaml"):
            try:
                text, _ = decode_text_bytes(abs_path.read_bytes())
            except OSError as exc:
                diagnostics.append({
                    "code": "FILE_READ_ERROR",
                    "severity": "Warn",
                    "file": rel,
                    "line": 0,
                    "message": str(exc),
                })
                continue
            xres = parse_xaml_file(text, rel, asm_name, root_ns, file_is_shipping)
            if xres.is_pure_icon_or_brush_dict:
                pure_icon_dict_count += 1
                continue
            definitions.extend(xres.definitions)
            elements.extend(xres.elements)
            edges.extend(xres.edges)
            for s in xres.strings:
                record_string(
                    s["assembly"], s["res_class"], s["key"],
                    val_en=s["val_en"], val_zh=s["val_zh"], source_kind="resdict",
                )
            diagnostics.extend(xres.diagnostics)
            continue

        # 4. .resx localization tables (neutral + en-* + zh-* only; skip non-EN/ZH locales)
        if lower.endswith(".resx"):
            if not file_is_shipping:
                continue
            try:
                text, _ = decode_text_bytes(abs_path.read_bytes())
            except OSError:
                continue
            res_cls, locale = split_resx_stem_and_locale(abs_path.stem)
            bucket = classify_locale_bucket(locale, rel)
            if bucket not in ("en", "zh"):
                continue
            entries = parse_resx_entries(text)
            for k, v in entries.items():
                if bucket == "zh":
                    record_string(asm_name, res_cls, k, val_zh=v, source_kind="resx")
                else:
                    record_string(asm_name, res_cls, k, val_en=v, source_kind="resx")
            continue

        # 5. C# source files (.Designer.cs, AssemblyInfo.cs, ViewModels/Wiring/StringTables)
        if lower.endswith(".cs"):
            if lower.endswith(".g.cs") or lower.endswith(".g.i.cs"):
                continue
            try:
                text, _ = decode_text_bytes(abs_path.read_bytes())
            except OSError:
                continue

            if fname_lower == "assemblyinfo.cs" or "XmlnsDefinition" in text:
                xmlns_defs.extend(extract_assembly_xmlns_defs(text, asm_name))

            if lower.endswith(".designer.cs"):
                wf_res = parse_winforms_designer(text, rel, asm_name, root_ns, file_is_shipping)
                if wf_res is not None:
                    definitions.extend(wf_res.definitions)
                    elements.extend(wf_res.elements)
                    edges.extend(wf_res.edges)
                    continue

            if file_is_shipping:
                for s in extract_csharp_string_constants(text, asm_name):
                    record_string(
                        s["assembly"], s["res_class"], s["key"],
                        val_en=s["val_en"], val_zh=s["val_zh"], source_kind="cs_const",
                    )

            cs_res = scan_csharp_file(text, rel, asm_name, root_ns, file_is_shipping)
            types.extend(cs_res.types)
            edges.extend(cs_res.edges)
            vm_properties.extend(cs_res.vm_properties)
            continue

        # 5. Shipping .config / AddIn.Config / SystemConfig.xml
        if file_is_shipping and (
            lower.endswith(".config")
            or fname_lower in ("addin.config", "systemconfig.xml", "applicationconfig.xml", "uiconfig.xml")
        ):
            if fname_lower in ("packages.config",):
                continue
            try:
                text, _ = decode_text_bytes(abs_path.read_bytes())
            except OSError:
                continue
            configs.extend(parse_default_config_xml(text, rel))

    # Deduplicate and deterministically sort all tables
    xmlns_unique = sorted(
        {(d["assembly"], d["xml_uri"], d["clr_namespace"]): d for d in xmlns_defs}.values(),
        key=lambda d: (d["xml_uri"], d["clr_namespace"], d["assembly"]),
    )
    types_unique = sorted(
        {(t["assembly"], t["full_name"], t["source_kind"]): t for t in types}.values(),
        key=lambda t: (t["full_name"], t["assembly"], t["source_kind"]),
    )
    definitions.sort(key=lambda d: d["def_id"])
    elements.sort(key=lambda e: (e["def_id"], e["elem_id"]))
    edges.sort(
        key=lambda ed: (
            ed["src_def_id"],
            ed["src_elem_id"] or "",
            ed["edge_kind"],
            ed["dst_type_or_key"],
        )
    )
    strings_list = [
        {
            "assembly": asm,
            "res_class": rcls,
            "key": k,
            "val_en": vals[0],
            "val_zh": vals[1],
            "source_kind": vals[2],
        }
        for (asm, rcls, k), vals in sorted(string_map.items())
    ]

    # Merge `vm_properties` across partial classes with priority & multi-value dynamic detection
    _VP_PRIO = {"dynamic_multi": 10, "key_probe": 4, "static_ref": 3, "literal": 2, "alias": 1, "type_ref": 0}
    _CONCRETE_VP_KINDS = frozenset({"key_probe", "static_ref", "literal"})
    merged_vp: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for vp in vm_properties:
        key_vp = (vp["assembly"], vp["owner_class"], vp["prop"])
        prev = merged_vp.get(key_vp)
        if prev is None:
            merged_vp[key_vp] = dict(vp)
            continue
        if prev["kind"] == "dynamic_multi" or vp["kind"] == "dynamic_multi":
            merged_vp[key_vp] = {**prev, "kind": "dynamic_multi", "value": "*"}
            continue
        if (
            prev["kind"] in _CONCRETE_VP_KINDS
            and vp["kind"] in _CONCRETE_VP_KINDS
            and (prev["kind"], prev["value"]) != (vp["kind"], vp["value"])
        ):
            merged_vp[key_vp] = {**prev, "kind": "dynamic_multi", "value": "*"}
            continue
        if _VP_PRIO.get(vp["kind"], 0) > _VP_PRIO.get(prev["kind"], 0):
            merged_vp[key_vp] = dict(vp)

    vm_props_unique = sorted(
        merged_vp.values(),
        key=lambda p: (p["owner_class"], p["prop"], p["assembly"]),
    )
    configs.sort(key=lambda c: (c["rel_path"], c["xpath_key"], c["value"]))
    diagnostics.sort(key=lambda d: (d["file"], d["line"], d["code"], d["message"]))

    snapshot: Dict[str, Any] = {
        "schema_version": SCANNER_SCHEMA_VERSION,
        "repo_id": repo_id,
        "commit": commit,
        "branch": resolved_branch,
        "stats": {
            "shipping_solutions": len(scope.shipping_solutions),
            "assemblies": len(assemblies),
            "shipping_assemblies": sum(1 for a in assemblies if a["is_shipping"]),
            "xmlns_defs": len(xmlns_unique),
            "types": len(types_unique),
            "definitions": len(definitions),
            "elements": len(elements),
            "edges": len(edges),
            "strings": len(strings_list),
            "vm_properties": len(vm_props_unique),
            "configs": len(configs),
            "pure_icon_dicts_skipped": pure_icon_dict_count,
            "diagnostics": len(diagnostics),
        },
        "shipping_solutions": scope.shipping_solutions,
        "assemblies": assemblies,
        "xmlns_defs": xmlns_unique,
        "types": types_unique,
        "definitions": definitions,
        "elements": elements,
        "edges": edges,
        "strings": strings_list,
        "vm_properties": vm_props_unique,
        "configs": configs,
        "diagnostics": diagnostics,
    }
    return snapshot


def scan_reference_dll_dirs(dirs: Iterable[Path]) -> Optional[Dict[str, Any]]:
    """Scan optional external NuGet/SDK DLL directories (including `packages/`) for type/BAML/satellite metadata."""
    dll_files, missing_dirs = iter_reference_dll_files(dirs, skip_dll_prefixes=SKIP_DLL_PREFIXES)
    if missing_dirs:
        import sys as _sys
        for md in missing_dirs:
            print(f"[ui-inventory build] WARNING: Configured REFERENCE_DLL_DIRS path does not exist: {md}", file=_sys.stderr, flush=True)
    if not dll_files and not missing_dirs:
        return None

    xmlns_defs: List[Dict[str, str]] = []
    types: List[Dict[str, Any]] = []
    definitions: List[Dict[str, Any]] = []
    elements: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    string_map: Dict[Tuple[str, str, str], List[str]] = {}
    diagnostics: List[Dict[str, Any]] = [
        {
            "code": "REF_DLL_DIR_MISSING",
            "severity": "Warn",
            "file": md,
            "line": 0,
            "message": f"Configured REFERENCE_DLL_DIRS directory does not exist: {md}",
        }
        for md in missing_dirs
    ]
    seen_dll_hashes: Set[str] = set()

    for abs_path, rel_label in dll_files:
        try:
            st_size = abs_path.stat().st_size
            if st_size > MAX_MANAGED_UI_DLL_BYTES or st_size < 512:
                continue
            raw_dll = abs_path.read_bytes()
        except OSError:
            continue
        dll_hash = sha256_bytes(raw_dll)
        if dll_hash in seen_dll_hashes:
            continue
        seen_dll_hashes.add(dll_hash)
        bmeta = parse_managed_dll(raw_dll, rel_label)
        if bmeta is None:
            continue
        xmlns_defs.extend(bmeta.xmlns_defs)
        types.extend(bmeta.types)
        definitions.extend(bmeta.baml_definitions)
        elements.extend(bmeta.baml_elements)
        edges.extend(bmeta.baml_edges)
        for s in bmeta.strings:
            slot = string_map.setdefault((s["assembly"], s["res_class"], s["key"]), ["", ""])
            if s.get("val_en") and not slot[0]:
                slot[0] = s["val_en"]
            if s.get("val_zh") and not slot[1]:
                slot[1] = s["val_zh"]
        diagnostics.extend(bmeta.diagnostics)

    xmlns_unique = sorted(
        {(d["assembly"], d["xml_uri"], d["clr_namespace"]): d for d in xmlns_defs}.values(),
        key=lambda d: (d["xml_uri"], d["clr_namespace"], d["assembly"]),
    )
    types_unique = sorted(
        {(t["assembly"], t["full_name"], t["source_kind"]): t for t in types}.values(),
        key=lambda t: (t["full_name"], t["assembly"], t["source_kind"]),
    )
    definitions.sort(key=lambda d: d["def_id"])
    elements.sort(key=lambda e: (e["def_id"], e["elem_id"]))
    edges.sort(key=lambda ed: (ed["src_def_id"], ed["src_elem_id"] or "", ed["edge_kind"], ed["dst_type_or_key"]))
    strings_list = [
        {"assembly": asm, "res_class": rcls, "key": k, "val_en": vals[0], "val_zh": vals[1], "source_kind": "scoped_resources"}
        for (asm, rcls, k), vals in sorted(string_map.items())
    ]

    return {
        "schema_version": SCANNER_SCHEMA_VERSION,
        "repo_id": "__reference_dlls__",
        "commit": "external-ref-dlls",
        "branch": "",
        "is_reference_only": True,
        "stats": {
            "dlls_scanned": len(seen_dll_hashes),
            "xmlns_defs": len(xmlns_unique),
            "types": len(types_unique),
            "definitions": len(definitions),
            "elements": len(elements),
            "edges": len(edges),
            "strings": len(strings_list),
            "diagnostics": len(diagnostics),
        },
        "shipping_solutions": [],
        "assemblies": [],
        "xmlns_defs": xmlns_unique,
        "types": types_unique,
        "definitions": definitions,
        "elements": elements,
        "edges": edges,
        "strings": strings_list,
        "vm_properties": [],
        "configs": [],
        "diagnostics": diagnostics,
    }


def serialize_snapshot_gzip(snapshot: Dict[str, Any]) -> Tuple[bytes, str]:
    """Serialize snapshot deterministically (`sort_keys=True`, `gzip mtime=0`) and return `(gz_bytes, sha256)`."""
    json_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=6, mtime=0) as gz:
        gz.write(json_bytes)
    gz_bytes = buf.getvalue()
    return gz_bytes, hashlib.sha256(gz_bytes).hexdigest()


def write_snapshot_file(snapshot: Dict[str, Any], out_path: Path) -> str:
    """Atomically write `.snapshot.json.gz` (or `.json`) and return its SHA-256 hex digest."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    if out_path.name.endswith(".gz"):
        payload, digest = serialize_snapshot_gzip(snapshot)
    else:
        payload = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
    tmp_path.write_bytes(payload)
    for attempt in range(8):
        try:
            tmp_path.replace(out_path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.15 * (attempt + 1))
    return digest


def read_snapshot_file(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if path.name.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        text = gzip.decompress(raw).decode("utf-8")
    else:
        text = raw.decode("utf-8")
    return json.loads(text)
