"""Encoding detection, BuildList/.sln/.csproj scope resolution, and noise filtering.

100% Python standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
from pathlib import Path
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
import xml.etree.ElementTree as ET


NON_SHIPPING_DIR_RE = re.compile(
    r"(?:^|/)(?:DemoAndTest|UnitTests?|Tests?|TestData|TestAssets|TestFixtures?|Fixtures|Mocks|Fakes|Simulator|AIStudy)(?:/|$)",
    re.IGNORECASE,
)

NON_SHIPPING_PROJECT_RE = re.compile(
    r"(?:\.UnitTests?|\.Tests?|Demo|Tester|Simulator)$",
    re.IGNORECASE,
)

SKIP_WALK_DIRS = frozenset({
    ".git",
    ".github",
    ".vs",
    ".svn",
    ".idea",
    "__pycache__",
    "obj",
    "packages",
    "node_modules",
    "$recycle.bin",
    # Heavy native C++/AI third-party SDK trees with zero managed desktop UI definitions
    "openvino",
    "openvino2022.2",
    "cuda",
    "tensorflow",
    "pcl 1.11.0",
    "azure_kinect_sdk",
    "entlib",
    "fonts",
})

_REF_DLL_SKIP_DIRS = frozenset({
    ".git",
    ".github",
    ".vs",
    ".svn",
    ".idea",
    "__pycache__",
    "obj",
    "node_modules",
    "$recycle.bin",
})


def iter_reference_dll_files(
    dirs: Iterable[Path],
    *,
    skip_dll_prefixes: Sequence[str] = (),
) -> Tuple[List[Tuple[Path, str]], List[str]]:
    """Walk external NuGet/SDK DLL directories WITHOUT skipping `packages/`.

    Returns `(matched_dlls, missing_dir_strs)` where `matched_dlls` is deterministically sorted
    `(abs_path, logical_rel_label)` pairs excluding `skip_dll_prefixes`.
    """
    matched: List[Tuple[str, Path]] = []
    missing: List[str] = []
    seen_roots: Set[str] = set()
    prefixes_low = tuple(p.lower() for p in skip_dll_prefixes)

    for raw_d in dirs:
        raw_s = str(raw_d or "").strip()
        if not raw_s:
            continue
        p = Path(raw_s)
        if not p.is_dir():
            missing.append(raw_s)
            continue
        root_resolved = p.resolve()
        rkey = str(root_resolved).lower()
        if rkey in seen_roots:
            continue
        seen_roots.add(rkey)

        seen_dirs: Set[str] = {rkey}
        stack: List[Path] = [root_resolved]
        while stack:
            cur = stack.pop()
            try:
                entries = sorted(cur.iterdir(), key=lambda x: x.name.lower())
            except OSError:
                continue
            sub_dirs: List[Path] = []
            for entry in entries:
                name_low = entry.name.lower()
                if entry.is_dir():
                    if name_low in _REF_DLL_SKIP_DIRS:
                        continue
                    try:
                        resolved_sub = str(entry.resolve()).lower()
                    except OSError:
                        continue
                    if resolved_sub in seen_dirs:
                        continue
                    seen_dirs.add(resolved_sub)
                    sub_dirs.append(entry)
                elif entry.is_file() and name_low.endswith(".dll"):
                    if prefixes_low and any(name_low.startswith(pfx) for pfx in prefixes_low):
                        continue
                    rel = f"{root_resolved.name}/{entry.relative_to(root_resolved).as_posix()}"
                    matched.append((rel, entry))
            for sd in reversed(sub_dirs):
                stack.append(sd)

    matched.sort(key=lambda item: item[0].lower())
    return [(abs_p, rel) for rel, abs_p in matched], missing

SLN_PROJECT_RE = re.compile(
    r'Project\("\{[0-9A-Fa-f\-]+\}"\)\s*=\s*"([^"]+)"\s*,\s*"([^"]+\.csproj)"\s*,\s*"\{([0-9A-Fa-f\-]+)\}"',
    re.IGNORECASE,
)


@dataclass
class CsProjInfo:
    rel_path: str
    name: str
    assembly_name: str
    root_namespace: str
    output_type: str
    project_dir: str
    is_shipping: bool
    project_refs: List[str] = field(default_factory=list)
    assembly_refs: List[str] = field(default_factory=list)
    hint_dll_paths: List[str] = field(default_factory=list)
    explicit_items: Set[str] = field(default_factory=set)


@dataclass
class RepoScope:
    repo_id: str
    repo_root: Path
    shipping_solutions: List[str]
    projects: Dict[str, CsProjInfo]  # keyed by normalized rel_path
    dir_to_project: List[Tuple[str, CsProjInfo]]  # sorted longest prefix first
    explicit_item_map: Dict[str, CsProjInfo]
    referenced_dlls: Set[str]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_text_bytes(raw: bytes) -> Tuple[str, str]:
    """Decode source/config bytes reliably across UTF-8, UTF-8-BOM, UTF-16LE/BE, and GB18030."""
    if not raw:
        return "", "utf-8"
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw[3:].decode("utf-8"), "utf-8-bom"
        except UnicodeDecodeError:
            return raw[3:].decode("gb18030", errors="replace"), "gb18030"

    sample = raw[:256]
    if b"\x00" in sample and len(sample) >= 4:
        even_zeros = sum(1 for idx in range(0, len(sample), 2) if sample[idx] == 0)
        odd_zeros = sum(1 for idx in range(1, len(sample), 2) if sample[idx] == 0)
        half = max(1, len(sample) // 2)
        if odd_zeros / half > 0.35 and even_zeros / half < 0.1:
            return raw.decode("utf-16-le", errors="replace"), "utf-16-le-nobom"
        if even_zeros / half > 0.35 and odd_zeros / half < 0.1:
            return raw.decode("utf-16-be", errors="replace"), "utf-16-be-nobom"

    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace"), "gb18030"


def normalize_rel_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_non_shipping_path(rel_posix: str) -> bool:
    return bool(NON_SHIPPING_DIR_RE.search(rel_posix))


def matches_exclude_patterns(rel_posix: str, exclude_patterns: Optional[Sequence[str]], *, repo_id: str = "") -> bool:
    """Return True if `rel_posix` (or `<repo_id>/<rel_posix>`) matches any configured path exclusion pattern."""
    if not exclude_patterns:
        return False
    norm_rel = rel_posix.replace("\\", "/").strip("/")
    low_rel = norm_rel.lower()
    low_prefixed = f"{repo_id.lower()}/{low_rel}" if repo_id else low_rel
    segments = [seg for seg in low_rel.split("/") if seg]
    for raw_pat in exclude_patterns:
        pat = str(raw_pat or "").replace("\\", "/").strip().strip("/").lower()
        if not pat:
            continue
        has_glob = any(ch in pat for ch in ("*", "?", "["))
        if has_glob:
            if fnmatch.fnmatch(low_rel, pat) or fnmatch.fnmatch(low_prefixed, pat):
                return True
            if "/" not in pat and any(fnmatch.fnmatch(seg, pat) for seg in segments):
                return True
        else:
            if "/" in pat:
                if f"/{pat}/" in f"/{low_rel}/" or f"/{pat}/" in f"/{low_prefixed}/":
                    return True
            else:
                if pat in segments or (repo_id and pat == repo_id.lower()):
                    return True
    return False


def is_build_intermediate_source_path(rel_posix: str) -> bool:
    lowered = f"/{rel_posix.lower()}/"
    return "/obj/" in lowered or "/bin/debug/" in lowered or "/bin/release/" in lowered or "/bin/x64/" in lowered or "/bin/x86/" in lowered


def iter_repo_files(repo_root: Path) -> Iterable[Tuple[Path, str]]:
    """Yield (abs_path, rel_posix_path) deterministically while pruning heavy/irrelevant dirs."""
    root_resolved = repo_root.resolve()
    stack: List[Path] = [root_resolved]
    files: List[Tuple[str, Path]] = []

    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        dirs_to_push: List[Path] = []
        for entry in entries:
            name_lower = entry.name.lower()
            if entry.is_dir():
                if name_lower in SKIP_WALK_DIRS:
                    continue
                dirs_to_push.append(entry)
            elif entry.is_file():
                rel = entry.relative_to(root_resolved).as_posix()
                files.append((rel, entry))
        for d in reversed(dirs_to_push):
            stack.append(d)

    files.sort(key=lambda item: item[0])
    for rel, abs_path in files:
        yield abs_path, rel


def parse_build_list_slns(build_list_files: Iterable[Path], repo_id: str) -> Set[str]:
    """Extract shipping .sln paths matching `repo_id` from BuildList files."""
    matched: Set[str] = set()
    prefix_re = re.compile(rf"(?:^|/){re.escape(repo_id)}/([^,\r\n]+\.sln)", re.IGNORECASE)
    for bf in build_list_files:
        if not bf.is_file():
            continue
        try:
            text, _ = decode_text_bytes(bf.read_bytes())
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cleaned = line.replace("\\", "/")
            m = prefix_re.search(cleaned)
            if m:
                matched.add(m.group(1).strip("/"))
    return matched


def _strip_xml_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _resolve_relative_norm(base_dir_posix: str, rel_ref: str) -> str:
    ref_clean = rel_ref.replace("\\", "/").strip()
    combined = f"{base_dir_posix}/{ref_clean}" if base_dir_posix else ref_clean
    parts: List[str] = []
    for part in combined.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def parse_csproj(csproj_path: Path, rel_posix: str) -> Optional[CsProjInfo]:
    try:
        text, _ = decode_text_bytes(csproj_path.read_bytes())
        root = ET.fromstring(text)
    except (OSError, ET.ParseError):
        return None

    proj_stem = csproj_path.stem
    assembly_name = proj_stem
    root_namespace = proj_stem
    output_type = "Library"
    project_refs: List[str] = []
    assembly_refs: List[str] = []
    hint_dlls: List[str] = []
    explicit_items: Set[str] = set()

    proj_dir = "" if "/" not in rel_posix else rel_posix.rsplit("/", 1)[0]

    for elem in root.iter():
        lname = _strip_xml_ns(elem.tag)
        if lname == "AssemblyName" and (elem.text or "").strip():
            assembly_name = elem.text.strip()
        elif lname == "RootNamespace" and (elem.text or "").strip():
            root_namespace = elem.text.strip()
        elif lname == "OutputType" and (elem.text or "").strip():
            output_type = elem.text.strip()
        elif lname == "ProjectReference":
            inc = elem.attrib.get("Include", "").strip()
            if inc:
                project_refs.append(_resolve_relative_norm(proj_dir, inc))
        elif lname == "Reference":
            inc = elem.attrib.get("Include", "").strip()
            if inc:
                asm_short = inc.split(",", 1)[0].strip()
                if asm_short:
                    assembly_refs.append(asm_short)
        elif lname == "HintPath" and (elem.text or "").strip():
            raw_hint = elem.text.strip()
            if raw_hint.lower().endswith(".dll"):
                hint_dlls.append(_resolve_relative_norm(proj_dir, raw_hint))
        elif lname in {"Page", "Compile", "EmbeddedResource", "ApplicationDefinition", "Content", "None", "Resource"}:
            inc = elem.attrib.get("Include", "").strip()
            if inc and "*" not in inc:
                explicit_items.add(_resolve_relative_norm(proj_dir, inc))

    non_shipping = is_non_shipping_path(rel_posix) or bool(NON_SHIPPING_PROJECT_RE.search(proj_stem))
    return CsProjInfo(
        rel_path=rel_posix,
        name=proj_stem,
        assembly_name=assembly_name,
        root_namespace=root_namespace,
        output_type=output_type,
        project_dir=proj_dir,
        is_shipping=not non_shipping,
        project_refs=sorted(set(project_refs)),
        assembly_refs=sorted(set(assembly_refs)),
        hint_dll_paths=sorted(set(hint_dlls)),
        explicit_items=explicit_items,
    )


def discover_shipping_scope(
    repo_root: Path,
    repo_id: str,
    *,
    all_files: Optional[List[Tuple[Path, str]]] = None,
    external_build_lists: Optional[Iterable[Path]] = None,
) -> RepoScope:
    """Resolve shipping solutions, projects, and ProjectReference closures for one repo."""
    file_entries = all_files if all_files is not None else list(iter_repo_files(repo_root))
    sln_files: Dict[str, Path] = {}
    csproj_files: Dict[str, Path] = {}
    local_build_lists: List[Path] = []

    for abs_path, rel in file_entries:
        lower = rel.lower()
        if lower.endswith(".sln"):
            sln_files[rel] = abs_path
        elif lower.endswith(".csproj"):
            csproj_files[rel] = abs_path
        elif "buildlist" in abs_path.name.lower() and abs_path.suffix == "":
            local_build_lists.append(abs_path)

    build_lists = list(local_build_lists)
    if external_build_lists:
        build_lists.extend(external_build_lists)

    explicit_slns_lower = {s.lower() for s in parse_build_list_slns(build_lists, repo_id)}

    shipping_slns: List[str] = []
    for rel in sorted(sln_files):
        if is_non_shipping_path(rel) or NON_SHIPPING_PROJECT_RE.search(Path(rel).stem):
            continue
        if explicit_slns_lower:
            if rel.lower() in explicit_slns_lower:
                shipping_slns.append(rel)
        else:
            shipping_slns.append(rel)

    # Fallback if BuildList did not list any .sln in this repo (e.g. standalone utility/common repo)
    if not shipping_slns and sln_files:
        for rel in sorted(sln_files):
            if not is_non_shipping_path(rel) and not NON_SHIPPING_PROJECT_RE.search(Path(rel).stem):
                shipping_slns.append(rel)

    projects: Dict[str, CsProjInfo] = {}
    lower_to_rel_csproj: Dict[str, str] = {}
    for rel, abs_path in sorted(csproj_files.items()):
        info = parse_csproj(abs_path, rel)
        if info is not None:
            projects[rel] = info
            lower_to_rel_csproj[rel.lower()] = rel

    if shipping_slns:
        sln_included_lower: Set[str] = set()
        for sln_rel in shipping_slns:
            sln_dir = "" if "/" not in sln_rel else sln_rel.rsplit("/", 1)[0]
            try:
                sln_text, _ = decode_text_bytes(sln_files[sln_rel].read_bytes())
            except OSError:
                continue
            for m in SLN_PROJECT_RE.finditer(sln_text):
                proj_ref = _resolve_relative_norm(sln_dir, m.group(2))
                canonical = lower_to_rel_csproj.get(proj_ref.lower())
                if canonical:
                    sln_included_lower.add(canonical.lower())

        # Compute transitive ProjectReference closure from shipping .sln roots
        queue = [lower_to_rel_csproj[k] for k in sorted(sln_included_lower) if k in lower_to_rel_csproj]
        visited: Set[str] = set(queue)
        while queue:
            cur_rel = queue.pop()
            cur_proj = projects[cur_rel]
            for ref in cur_proj.project_refs:
                target_rel = lower_to_rel_csproj.get(ref.lower())
                if target_rel and target_rel not in visited:
                    visited.add(target_rel)
                    queue.append(target_rel)

        if visited:
            for rel, proj in projects.items():
                if rel not in visited:
                    proj.is_shipping = False
                elif is_non_shipping_path(rel) or NON_SHIPPING_PROJECT_RE.search(proj.name):
                    proj.is_shipping = False

    referenced_dlls: Set[str] = set()
    for proj in projects.values():
        if proj.is_shipping:
            for hint in proj.hint_dll_paths:
                referenced_dlls.add(hint.lower())

    explicit_item_map: Dict[str, CsProjInfo] = {}
    for rel_proj in sorted(projects):
        proj = projects[rel_proj]
        for item_rel in proj.explicit_items:
            explicit_item_map.setdefault(item_rel.lower(), proj)

    dir_pairs = sorted(
        ((proj.project_dir.lower(), proj) for proj in projects.values()),
        key=lambda item: (-len(item[0]), item[0]),
    )
    return RepoScope(
        repo_id=repo_id,
        repo_root=repo_root,
        shipping_solutions=shipping_slns,
        projects=projects,
        dir_to_project=dir_pairs,
        explicit_item_map=explicit_item_map,
        referenced_dlls=referenced_dlls,
    )


def match_owning_project(rel_posix: str, scope: RepoScope) -> Optional[CsProjInfo]:
    """Find the owning .csproj for a source file by O(1) case-insensitive explicit item lookup or directory prefix."""
    rel_low = rel_posix.lower()
    direct = scope.explicit_item_map.get(rel_low)
    if direct is not None:
        return direct
    for proj_dir_low, proj in scope.dir_to_project:
        if not proj_dir_low or rel_low.startswith(proj_dir_low + "/"):
            return proj
    return None
