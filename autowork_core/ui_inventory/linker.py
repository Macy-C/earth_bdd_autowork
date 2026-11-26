"""Pure in-memory cross-repository UI linker, UIA Peer shell dissolver, and surface assembler."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from autowork_core.ui_inventory.models import (
    DynamicUnmountedIssue,
    IdentityGapIssue,
    ProjectedNode,
    ProjectedSurface,
    ResolvedTypeSpec,
    UidShadowIssue,
    WorkspaceIndex,
)
from autowork_core.ui_inventory.scanner.markup_expr import (
    MarkupCall,
    MarkupExpr,
    MarkupLiteral,
    extract_binding_path as _parse_xaml_binding_path,
    parse_markup_expr,
)
from autowork_core.ui_inventory.scanner.repo_scanner import scan_reference_dll_dirs
from autowork_core.ui_inventory.scanner.wpf_builtin_specs import (
    WPF_BUILTIN_TYPE_SPECS,
    WPF_SHORT_NAME_TO_FULL,
)
from autowork_core.ui_inventory.solver import solve_all_surfaces
from autowork_core.ui_inventory.store import SnapshotStore


ELEM_SEQ_RE = re.compile(r"::e(\d+)$")
_CJK_ACCESS_KEY_SUFFIX_RE = re.compile(r"\s*\(_[A-Za-z0-9]\)\s*$")
_MARKUP_SKIP_ARG_TOKENS = (
    "service", "provider", "converter", "format", "culture", "mode", "target", "fallback", "null",
)
_MARKUP_KEY_ARG_PRIORITY = (
    "key", "resourcekey", "original", "stringkey", "id", "uid", "name", "text",
)
_LABEL_BEARING_ATTACHED_PROBE_UIA_TYPES = frozenset({
    "Button", "CheckBox", "RadioButton", "TabItem", "MenuItem", "Text", "Group", "HeaderItem", "Window",
})

_EMBEDDED_WEB_HOST_SHORT_NAMES = frozenset({
    "ChromiumWebBrowser",
    "WebView2",
    "WebBrowser",
})
_EMBEDDED_WEB_HOST_PREFIXES = (
    "CefSharp.Wpf.",
    "Microsoft.Web.WebView2.Wpf.",
)
_VM_PROP_PRIORITY = {
    "dynamic_multi": 10,
    "key_probe": 4,
    "res_key": 4,
    "static_ref": 3,
    "xstatic": 3,
    "literal": 2,
    "alias": 1,
    "type_ref": 0,
}
_CONCRETE_VM_KINDS = frozenset({"key_probe", "res_key", "static_ref", "xstatic", "literal"})

# UIA AutomationPeer authoritative `@name` property precedence by UIA ControlType
_INPUT_VALUE_ONLY_UIA_TYPES = frozenset({"Edit", "Document", "ComboBox", "Spinner", "Slider"})
_HEADERED_UIA_TYPES = frozenset({"TabItem", "Group", "MenuItem", "HeaderItem"})
_WINDOW_UIA_TYPES = frozenset({"Window"})
_TEXT_DISPLAY_UIA_TYPES = frozenset({"Text"})

# Composite UserControl suffixes that must NEVER be misclassified as leaf controls by external inference
_COMPOSITE_CONTROL_SUFFIXES = (
    "View", "Panel", "Control", "Group", "Wrapper", "Host", "Container",
    "Bar", "Editor", "Presenter", "Style", "Item", "Converter", "Template",
    "Helper", "Manager", "Service", "Page",
)


def _is_embedded_web_host_type(full_name: str, short_name: str) -> bool:
    if short_name in _EMBEDDED_WEB_HOST_SHORT_NAMES:
        return True
    return any(full_name.startswith(pfx) for pfx in _EMBEDDED_WEB_HOST_PREFIXES)


def _infer_external_uia_type(full_name: str, short_name: str) -> Tuple[str, bool, str]:
    """Strict suffix-only UIA control-type fallback for external NuGet/SDK leaf controls.

    Composite wrappers (`*View`, `*Panel`, `*Control`, etc.) conservatively stay `Custom`.
    """
    if _is_embedded_web_host_type(full_name, short_name):
        return "Pane", True, "verified"
    s = short_name
    if s.endswith(("Window", "Dialog")):
        return "Window", True, "inferred_external"
    if s.endswith(("TabItem", "TreeViewItem", "ListBoxItem", "ListViewItem", "MenuItem")):
        if s.endswith("TabItem"):
            return "TabItem", True, "inferred_external"
        if s.endswith("TreeViewItem"):
            return "TreeItem", True, "inferred_external"
        if s.endswith("MenuItem"):
            return "MenuItem", True, "inferred_external"
        return "ListItem", True, "inferred_external"
    if s.endswith("TabControl"):
        return "Tab", True, "inferred_external"
    if s.endswith("TreeView"):
        return "Tree", True, "inferred_external"
    if s.endswith("ListView"):
        return "List", True, "inferred_external"
    if s.endswith(_COMPOSITE_CONTROL_SUFFIXES):
        return "Custom", True, "verified"
    if s.endswith("RadioButton"):
        return "RadioButton", True, "inferred_external"
    if s.endswith("CheckBox"):
        return "CheckBox", True, "inferred_external"
    if s.endswith(("SplitButton", "SplitToggleButton", "SplitRadioButton")):
        return "ComboBox", True, "inferred_external"
    if s.endswith(("ComboBox", "ComboBoxDropDownOnly", "DropDownComboBox")):
        return "ComboBox", True, "inferred_external"
    if s.endswith("RichTextBox"):
        return "Document", True, "inferred_external"
    if s.endswith(("TextBox", "PasswordBox")):
        return "Edit", True, "inferred_external"
    if s.endswith(("NumericUpDown", "DoubleSpinner", "IntSpinner", "Spinner")):
        return "Spinner", True, "inferred_external"
    if s.endswith("Slider"):
        return "Slider", True, "inferred_external"
    if s.endswith("DataGrid"):
        return "DataGrid", True, "inferred_external"
    if s.endswith("ListBox"):
        return "List", True, "inferred_external"
    if s.endswith("Hyperlink"):
        return "Hyperlink", True, "inferred_external"
    if s.endswith("Button"):
        return "Button", True, "inferred_external"
    if s.endswith(("TextBlock", "Label")):
        return "Text", True, "inferred_external"
    return "Custom", True, "verified"


# WPF types whose AutomationPeer strips single `_` access-key mnemonics (verified via PresentationFramework)
_ACCESS_KEY_STRIPPING_UIA_TYPES = frozenset({
    "Button",
    "CheckBox",
    "RadioButton",
    "MenuItem",
    "TabItem",
    "Group",
})


def _strip_access_key(text: str) -> str:
    """Replicate WPF `AccessText.RemoveAccessKeyMarker`: first single `_` removed, `__` -> `_`."""
    if "_" not in text:
        return text
    out: List[str] = []
    consumed_mnemonic = False
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "_":
            if i + 1 < n and text[i + 1] == "_":
                out.append("_")
                i += 2
                continue
            if not consumed_mnemonic:
                consumed_mnemonic = True
                i += 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _norm_en_for_eq(text: str) -> str:
    return _strip_access_key(text).strip()


def _norm_zh_for_eq(text: str) -> str:
    return _CJK_ACCESS_KEY_SUFFIX_RE.sub("", _strip_access_key(text)).strip()


def _record_equiv_value(bucket: Dict[Any, Dict[str, str]], key: Any, raw_val: str, norm_fn: Any) -> None:
    """Record `raw_val` under its semantically normalized key; prefer cleaner display form without `_` mnemonic."""
    if not raw_val:
        return
    norm = norm_fn(raw_val)
    if not norm:
        return
    eq_map = bucket.setdefault(key, {})
    prev = eq_map.get(norm)
    if prev is None:
        eq_map[norm] = raw_val
    elif "_" in prev and "_" not in raw_val:
        eq_map[norm] = raw_val


def _finalize_equiv_table(
    bucket: Dict[Any, Dict[str, str]],
) -> Tuple[Dict[Any, str], Set[Any]]:
    unique_map: Dict[Any, str] = {}
    ambiguous: Set[Any] = set()
    for k, eq_map in bucket.items():
        if len(eq_map) == 1:
            unique_map[k] = next(iter(eq_map.values()))
        elif len(eq_map) > 1:
            ambiguous.add(k)
    return unique_map, ambiguous


def _elem_sort_key(elem: Dict[str, Any]) -> Tuple[int, int, str]:
    m = ELEM_SEQ_RE.search(elem["elem_id"])
    seq = int(m.group(1)) if m else 0
    return (int(elem.get("xml_order", 0)), seq, elem["elem_id"])


def build_workspace_index(
    snapshots: Iterable[Dict[str, Any]],
    *,
    default_locale: str = "en-US",
) -> WorkspaceIndex:
    """Link multiple repository snapshots purely in memory and solve zero-index selectors."""
    idx = WorkspaceIndex()
    snap_list = sorted(snapshots, key=lambda s: s["repo_id"])

    raw_types: List[Tuple[str, Dict[str, Any]]] = []
    direct_asm_refs: Dict[str, Set[str]] = {}
    cls_key_en_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    cls_key_zh_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    resdict_en_raw: Dict[str, Dict[str, str]] = {}
    resdict_zh_raw: Dict[str, Dict[str, str]] = {}
    loc_asm_en_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    loc_asm_zh_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    loc_src_asm_en_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    loc_src_asm_zh_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    loc_dll_asm_en_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    loc_dll_asm_zh_raw: Dict[Tuple[str, str], Dict[str, str]] = {}
    loc_flat_en_raw: Dict[str, Dict[str, str]] = {}
    loc_flat_zh_raw: Dict[str, Dict[str, str]] = {}
    loc_global_en_raw: Dict[str, Dict[str, str]] = {}
    loc_global_zh_raw: Dict[str, Dict[str, str]] = {}

    # Pass 1: Ingest raw tables across all repositories
    for snap in snap_list:
        repo_id = snap["repo_id"]
        if not snap.get("is_reference_only"):
            idx.repo_manifests[repo_id] = {
                "commit": snap.get("commit", ""),
                "branch": snap.get("branch", ""),
                "stats": snap.get("stats", {}),
            }

        for asm_rec in snap.get("assemblies") or []:
            aname = asm_rec.get("name") or ""
            if not aname:
                continue
            idx.assembly_repo.setdefault(aname, repo_id)
            rset = direct_asm_refs.setdefault(aname, set())
            for r_asm in asm_rec.get("ref_assemblies") or []:
                if r_asm and r_asm != aname:
                    rset.add(r_asm)

        for xd in snap.get("xmlns_defs") or []:
            lst = idx.xmlns_map.setdefault(xd["xml_uri"], [])
            pair = (xd["assembly"], xd["clr_namespace"])
            if pair not in lst:
                lst.append(pair)

        for st in snap.get("strings") or []:
            asm, rcls, key = st["assembly"], st["res_class"], st["key"]
            ven, vzh = st.get("val_en", ""), st.get("val_zh", "")
            src_kind = st.get("source_kind", "")
            prev = idx.strings_by_asm_cls_key.get((asm, rcls, key), ("", ""))
            merged = (ven or prev[0], vzh or prev[1])
            idx.strings_by_asm_cls_key[(asm, rcls, key)] = merged
            _record_equiv_value(cls_key_en_raw, (rcls, key), ven, _norm_en_for_eq)
            _record_equiv_value(cls_key_zh_raw, (rcls, key), vzh, _norm_zh_for_eq)
            if rcls == "$ResourceDictionary":
                _record_equiv_value(resdict_en_raw, key, ven, _norm_en_for_eq)
                _record_equiv_value(resdict_zh_raw, key, vzh, _norm_zh_for_eq)

            # Closed localization universe: strictly excludes C# constants (`cs_const`) and `{x:Static ...}` aliases
            is_loc_source = (
                src_kind in ("flat_resources", "scoped_resources", "dot_resources", "resx", "resdict")
                or (not src_kind and not ven.startswith("{x:Static"))
            )
            if is_loc_source and (ven or vzh) and not ven.startswith("{x:Static"):
                _record_equiv_value(loc_asm_en_raw, (asm, key), ven, _norm_en_for_eq)
                _record_equiv_value(loc_asm_zh_raw, (asm, key), vzh, _norm_zh_for_eq)
                if src_kind == "flat_resources":
                    _record_equiv_value(loc_flat_en_raw, key, ven, _norm_en_for_eq)
                    _record_equiv_value(loc_flat_zh_raw, key, vzh, _norm_zh_for_eq)
                    _record_equiv_value(loc_src_asm_en_raw, (asm, key), ven, _norm_en_for_eq)
                    _record_equiv_value(loc_src_asm_zh_raw, (asm, key), vzh, _norm_zh_for_eq)
                elif src_kind in ("scoped_resources", "dot_resources"):
                    _record_equiv_value(loc_dll_asm_en_raw, (asm, key), ven, _norm_en_for_eq)
                    _record_equiv_value(loc_dll_asm_zh_raw, (asm, key), vzh, _norm_zh_for_eq)
                else:
                    _record_equiv_value(loc_src_asm_en_raw, (asm, key), ven, _norm_en_for_eq)
                    _record_equiv_value(loc_src_asm_zh_raw, (asm, key), vzh, _norm_zh_for_eq)
                _record_equiv_value(loc_global_en_raw, key, ven, _norm_en_for_eq)
                _record_equiv_value(loc_global_zh_raw, key, vzh, _norm_zh_for_eq)

        for vp in snap.get("vm_properties") or []:
            owner_cls = vp.get("owner_class") or ""
            prop_name = vp.get("prop") or ""
            pkind = vp.get("kind") or ""
            pval = vp.get("value") or ""
            vp_asm = vp.get("assembly") or ""
            if not (owner_cls and prop_name and pkind and pval):
                continue
            if vp_asm and owner_cls not in idx.vm_assembly_by_class:
                idx.vm_assembly_by_class[owner_cls] = vp_asm
            cls_map = idx.vm_props_by_class.setdefault(owner_cls, {})
            prev_vp = cls_map.get(prop_name)
            if prev_vp is None:
                cls_map[prop_name] = (pkind, pval)
            elif prev_vp[0] == "dynamic_multi" or pkind == "dynamic_multi":
                cls_map[prop_name] = ("dynamic_multi", "*")
            elif (
                prev_vp[0] in _CONCRETE_VM_KINDS
                and pkind in _CONCRETE_VM_KINDS
                and prev_vp != (pkind, pval)
            ):
                cls_map[prop_name] = ("dynamic_multi", "*")
            elif _VM_PROP_PRIORITY.get(pkind, 0) > _VM_PROP_PRIORITY.get(prev_vp[0], 0):
                cls_map[prop_name] = (pkind, pval)
            short_owner = owner_cls.rsplit(".", 1)[-1]
            sc_list = idx.vm_props_by_short_class.setdefault(short_owner, [])
            if owner_cls not in sc_list:
                sc_list.append(owner_cls)

        idx.configs.extend(snap.get("configs") or [])

        for t in snap.get("types") or []:
            raw_types.append((repo_id, t))

        # Group elements & edges within this snapshot by (def_id) — keeping only the first file if duplicate def_id exists in one repo
        snap_elems_by_def: Dict[str, List[Dict[str, Any]]] = {}
        for elem in snap.get("elements") or []:
            snap_elems_by_def.setdefault(elem["def_id"], []).append(elem)

        accepted_defs_this_snap: Set[str] = set()
        seen_defs_in_snap: Set[str] = set()

        for d in snap.get("definitions") or []:
            def_id = d["def_id"]
            if def_id in seen_defs_in_snap:
                continue
            seen_defs_in_snap.add(def_id)

            def_copy = dict(d)
            def_copy["repo_id"] = repo_id
            def_copy["assembly"] = def_id.split("::", 1)[0]
            # Prefer shipping definitions (and XAML/WinForms source over BAML) if duplicate def_id exists across repos
            existing = idx.definitions.get(def_id)
            should_accept = existing is None or (
                def_copy.get("is_shipping") and not existing.get("is_shipping")
            )
            if not should_accept:
                continue

            if existing is not None:
                # Purge stale edges from the superseded lower-priority definition before ingesting replacements
                old_edges = idx.edges_by_src.pop(def_id, [])
                for oed in old_edges:
                    oeid = oed.get("src_elem_id")
                    if oeid and oeid in idx.edges_by_elem:
                        remaining = [e for e in idx.edges_by_elem[oeid] if e.get("src_def_id") != def_id]
                        if remaining:
                            idx.edges_by_elem[oeid] = remaining
                        else:
                            idx.edges_by_elem.pop(oeid, None)

            accepted_defs_this_snap.add(def_id)
            idx.definitions[def_id] = def_copy

            # Deduplicate elements by elem_id in case two files inside the same repo shared x:Class
            seen_eids: Set[str] = set()
            clean_elems: List[Dict[str, Any]] = []
            for el in snap_elems_by_def.get(def_id, []):
                eid = el["elem_id"]
                if eid not in seen_eids:
                    seen_eids.add(eid)
                    clean_elems.append(el)
            idx.elements_by_def[def_id] = clean_elems

            fcls = def_copy.get("full_class_name") or ""
            if fcls and def_copy.get("is_shipping", True):
                idx.defs_by_class[fcls] = def_id
                short_cls = fcls.rsplit(".", 1)[-1]
                lst_sc = idx.defs_by_short_class.setdefault(short_cls, [])
                if def_id not in lst_sc:
                    lst_sc.append(def_id)

            kind = def_copy.get("kind")
            if kind == "ControlTemplate" and def_copy.get("is_shipping", True):
                tt = def_copy.get("target_type") or ""
                rk = def_copy.get("res_key") or ""
                tt_short = tt.rsplit(".", 1)[-1]
                is_generic_wpf_base = (
                    tt.startswith("System.Windows.")
                    or tt_short in ("UserControl", "ContentControl", "Control", "HeaderedContentControl", "ItemsControl", "Window")
                )
                if tt and not is_generic_wpf_base:
                    idx.templates_by_target_type.setdefault(tt, []).append(def_id)
                    idx.templates_by_target_type.setdefault(tt_short, []).append(def_id)
                if rk and rk != tt:
                    idx.templates_by_res_key.setdefault(rk, []).append(def_id)
            elif kind == "Style" and def_copy.get("is_shipping", True):
                rk = def_copy.get("res_key") or ""
                if rk:
                    idx.styles_by_key[rk] = def_copy

        seen_edges: Set[Tuple[str, str, str, str]] = set()
        for edge in snap.get("edges") or []:
            s_did = edge["src_def_id"]
            if s_did in idx.definitions and s_did not in accepted_defs_this_snap:
                continue
            ekey = (s_did, edge.get("src_elem_id") or "", edge["edge_kind"], edge["dst_type_or_key"])
            if ekey in seen_edges:
                continue
            seen_edges.add(ekey)
            idx.edges_by_src.setdefault(s_did, []).append(edge)
            if edge.get("src_elem_id"):
                idx.edges_by_elem.setdefault(edge["src_elem_id"], []).append(edge)

    # Finalize transitive assembly dependency graph (BFS up to depth 8)
    for start_asm in direct_asm_refs:
        visited_refs: Set[str] = set()
        frontier = set(direct_asm_refs.get(start_asm, set()))
        for _ in range(8):
            nxt: Set[str] = set()
            for r_asm in frontier:
                if r_asm == start_asm or r_asm in visited_refs:
                    continue
                visited_refs.add(r_asm)
                nxt.update(direct_asm_refs.get(r_asm, set()))
            if not nxt:
                break
            frontier = nxt
        idx.assembly_refs[start_asm] = visited_refs

    # Finalize ambiguity-safe global class/key and ResourceDictionary lookup tables (never cross-splice conflicting EN/ZH!)
    ck_en, ck_amb_en = _finalize_equiv_table(cls_key_en_raw)
    ck_zh, ck_amb_zh = _finalize_equiv_table(cls_key_zh_raw)
    for ck in set(ck_en) | set(ck_zh):
        if ck in ck_amb_en:
            continue
        idx.strings_by_cls_key[ck] = (ck_en.get(ck, ""), "" if ck in ck_amb_zh else ck_zh.get(ck, ""))

    rd_en, rd_amb_en = _finalize_equiv_table(resdict_en_raw)
    rd_zh, rd_amb_zh = _finalize_equiv_table(resdict_zh_raw)
    for rk in set(rd_en) | set(rd_zh):
        if rk in rd_amb_en:
            continue
        idx.strings_by_key[rk] = (rd_en.get(rk, ""), "" if rk in rd_amb_zh else rd_zh.get(rk, ""))

    # Finalize carrier-tiered, EN/ZH-independent closed localization tables (with per-assembly ambiguity sets preserved)
    idx.loc_by_asm_en, idx.loc_by_asm_ambiguous_en = _finalize_equiv_table(loc_asm_en_raw)
    idx.loc_by_asm_zh, idx.loc_by_asm_ambiguous_zh = _finalize_equiv_table(loc_asm_zh_raw)
    idx.loc_src_by_asm_en, idx.loc_src_by_asm_ambiguous_en = _finalize_equiv_table(loc_src_asm_en_raw)
    idx.loc_src_by_asm_zh, idx.loc_src_by_asm_ambiguous_zh = _finalize_equiv_table(loc_src_asm_zh_raw)
    idx.loc_dll_by_asm_en, idx.loc_dll_by_asm_ambiguous_en = _finalize_equiv_table(loc_dll_asm_en_raw)
    idx.loc_dll_by_asm_zh, idx.loc_dll_by_asm_ambiguous_zh = _finalize_equiv_table(loc_dll_asm_zh_raw)
    idx.loc_flat_en, idx.loc_flat_ambiguous_en = _finalize_equiv_table(loc_flat_en_raw)
    idx.loc_flat_zh, idx.loc_flat_ambiguous_zh = _finalize_equiv_table(loc_flat_zh_raw)
    idx.loc_global_en, idx.loc_ambiguous_en_keys = _finalize_equiv_table(loc_global_en_raw)
    idx.loc_global_zh, idx.loc_ambiguous_zh_keys = _finalize_equiv_table(loc_global_zh_raw)

    # Populate backward-compatible aggregate views
    for ak in set(idx.loc_by_asm_en) | set(idx.loc_by_asm_zh):
        idx.loc_strings_by_asm_key[ak] = (idx.loc_by_asm_en.get(ak, ""), idx.loc_by_asm_zh.get(ak, ""))
    idx.loc_ambiguous_keys = set(idx.loc_ambiguous_en_keys)
    for k in set(idx.loc_global_en) | set(idx.loc_global_zh):
        idx.loc_strings_unique_by_key[k] = (idx.loc_global_en.get(k, ""), idx.loc_global_zh.get(k, ""))

    for elem_list in idx.elements_by_def.values():
        elem_list.sort(key=_elem_sort_key)

    # Pass 2: Build unified inheritance table (DLL + C# + XAML root classes + Builtins)
    _build_resolved_type_table(idx, raw_types)

    # Pass 2.5: Build MVVM View<->ViewModel, DataTemplate VM->View, and Region->Views indices
    _build_mvvm_and_region_indices(idx)

    # Pass 3: Audit x:Uid shadowing & duplicate x:Uid inside shipping definitions
    _collect_uid_shadow_issues(idx)

    # Pass 4: Compute component reference counts & identify Shared Components (>= 2 distinct host defs)
    _analyze_wiring_and_shared_components(idx)

    # Pass 5: Assemble & project UIA trees for HostWindows, Multi-Mode Hosts, AddIns, Dialogs, and SharedComponents
    _project_all_surfaces(idx, default_locale=default_locale)

    # Pass 6: Run Zero-Index Unique Selector Solver + Double Verification Gate across all surfaces
    solve_all_surfaces(idx, default_locale=default_locale)

    return idx


def load_and_build_store(
    store: SnapshotStore,
    *,
    default_locale: str = "en-US",
    reference_dll_dirs: Optional[Sequence[Path]] = None,
) -> WorkspaceIndex:
    repo_ids = store.list_available_repo_ids()
    snaps = [store.load_repo_snapshot(rid) for rid in repo_ids]
    if reference_dll_dirs:
        ref_snap = scan_reference_dll_dirs(reference_dll_dirs)
        if ref_snap is not None:
            snaps.append(ref_snap)
    return build_workspace_index(snaps, default_locale=default_locale)


# ---------------------------------------------------------------------------
# Pass 2: Cross-Repo CLR Type & AutomationPeer Inheritance Resolution
# ---------------------------------------------------------------------------
def _build_resolved_type_table(
    idx: WorkspaceIndex,
    raw_types: List[Tuple[str, Dict[str, Any]]],
) -> None:
    # Seed built-in WPF & WinForms types
    raw_lookup: Dict[str, Dict[str, Any]] = {}
    short_candidates: Dict[str, List[str]] = {}

    for full_name, (base_name, uia_type, has_peer) in WPF_BUILTIN_TYPE_SPECS.items():
        short = full_name.rsplit(".", 1)[-1]
        spec = ResolvedTypeSpec(
            full_name=full_name,
            short_name=short,
            assembly="PresentationFramework",
            base_full_name=base_name or "",
            uia_control_type=uia_type,
            has_uia_peer=has_peer,
        )
        idx.type_table[full_name] = spec
        idx.short_type_index.setdefault(short, []).append(spec)
        short_candidates.setdefault(short, []).append(full_name)

    # Register DLL types first (exact full_name + base_full_name), then C# source types
    sorted_raw = sorted(
        raw_types,
        key=lambda item: (0 if item[1].get("source_kind") == "Dll" else 1, item[1]["full_name"]),
    )
    for _, t in sorted_raw:
        fn = t["full_name"]
        if fn not in raw_lookup or t.get("source_kind") == "Dll":
            raw_lookup[fn] = t
            short = fn.rsplit(".", 1)[-1]
            if fn not in short_candidates.setdefault(short, []):
                short_candidates[short].append(fn)

    # Also register XAML definitions that declare full_class_name + base_clr_type
    for d in idx.definitions.values():
        fcls = d.get("full_class_name") or ""
        bclr = d.get("base_clr_type") or ""
        if fcls and bclr and fcls not in raw_lookup:
            resolved_bclr = _resolve_raw_clr_token(bclr, idx, raw_lookup, short_candidates)
            raw_lookup[fcls] = {
                "assembly": d.get("assembly", ""),
                "full_name": fcls,
                "base_full_name": resolved_bclr,
                "has_custom_peer": False,
                "addin_name": "",
            }
            short = fcls.rsplit(".", 1)[-1]
            if fcls not in short_candidates.setdefault(short, []):
                short_candidates[short].append(fn if False else fcls)

    def qualify_base_name(base_raw: str, owner_full: str, usings: List[str]) -> str:
        if not base_raw:
            return ""
        if base_raw in idx.type_table or base_raw in raw_lookup:
            return base_raw
        if "::" in base_raw:
            return _resolve_raw_clr_token(base_raw, idx, raw_lookup, short_candidates)
        if base_raw in WPF_SHORT_NAME_TO_FULL:
            return WPF_SHORT_NAME_TO_FULL[base_raw]
        owner_ns = owner_full.rsplit(".", 1)[0] if "." in owner_full else ""
        if owner_ns:
            same_ns = f"{owner_ns}.{base_raw}"
            if same_ns in raw_lookup or same_ns in idx.type_table:
                return same_ns
        for u in usings:
            cand = f"{u}.{base_raw}"
            if cand in raw_lookup or cand in idx.type_table:
                return cand
        short_hits = short_candidates.get(base_raw.rsplit(".", 1)[-1], [])
        if short_hits:
            return short_hits[0]
        return base_raw

    resolving: Set[str] = set()

    def resolve_one(full_name: str) -> ResolvedTypeSpec:
        if full_name in idx.type_table:
            return idx.type_table[full_name]
        if full_name in resolving:
            return ResolvedTypeSpec(
                full_name=full_name,
                short_name=full_name.rsplit(".", 1)[-1],
                assembly="",
                base_full_name="",
                uia_control_type="Custom",
                has_uia_peer=True,
            )
        resolving.add(full_name)
        tinfo = raw_lookup.get(full_name)
        if tinfo is None:
            # Unknown external custom type -> conservative suffix inference with explicit `inferred_external` provenance
            short = full_name.rsplit(".", 1)[-1]
            inf_uia, inf_peer, inf_src = _infer_external_uia_type(full_name, short)
            spec = ResolvedTypeSpec(
                full_name=full_name,
                short_name=short,
                assembly="",
                base_full_name="",
                uia_control_type=inf_uia,
                has_uia_peer=inf_peer,
                type_source=inf_src,
            )
            idx.type_table[full_name] = spec
            idx.short_type_index.setdefault(short, []).append(spec)
            resolving.discard(full_name)
            return spec

        q_base = qualify_base_name(
            tinfo.get("base_full_name", ""),
            full_name,
            tinfo.get("usings") or [],
        )
        has_custom_peer = bool(tinfo.get("has_custom_peer"))
        base_spec = resolve_one(q_base) if q_base else None

        if base_spec is not None:
            uia_t = base_spec.uia_control_type
            has_p = base_spec.has_uia_peer
            tsrc = base_spec.type_source
        else:
            uia_t = "Custom"
            has_p = True
            tsrc = "verified"

        if has_custom_peer and not has_p:
            has_p = True
            uia_t = uia_t or "Custom"

        short = full_name.rsplit(".", 1)[-1]
        spec = ResolvedTypeSpec(
            full_name=full_name,
            short_name=short,
            assembly=tinfo.get("assembly", ""),
            base_full_name=q_base,
            uia_control_type=uia_t,
            has_uia_peer=has_p,
            has_custom_peer=has_custom_peer,
            addin_name=tinfo.get("addin_name", ""),
            type_source=tsrc,
        )
        idx.type_table[full_name] = spec
        idx.short_type_index.setdefault(short, []).append(spec)
        resolving.discard(full_name)
        return spec

    for fn in sorted(raw_lookup):
        resolve_one(fn)


def _resolve_raw_clr_token(
    clr_token: str,
    idx: WorkspaceIndex,
    raw_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
    short_candidates: Optional[Dict[str, List[str]]] = None,
) -> str:
    if "::" not in clr_token:
        if clr_token in idx.type_table:
            return clr_token
        if clr_token in WPF_SHORT_NAME_TO_FULL:
            return WPF_SHORT_NAME_TO_FULL[clr_token]
        return clr_token

    uri, local_name = clr_token.split("::", 1)
    for _, clr_ns in idx.xmlns_map.get(uri, []):
        cand = f"{clr_ns}.{local_name}"
        if cand in idx.type_table or (raw_lookup and cand in raw_lookup) or cand in idx.defs_by_class:
            return cand
    if short_candidates and local_name in short_candidates:
        return short_candidates[local_name][0]
    if local_name in idx.short_type_index:
        return idx.short_type_index[local_name][0].full_name
    if local_name in WPF_SHORT_NAME_TO_FULL:
        return WPF_SHORT_NAME_TO_FULL[local_name]
    return local_name


def resolve_element_type_spec(clr_token: str, idx: WorkspaceIndex) -> ResolvedTypeSpec:
    resolved_name = _resolve_raw_clr_token(clr_token, idx)
    if resolved_name in idx.type_table:
        return idx.type_table[resolved_name]
    short = resolved_name.rsplit(".", 1)[-1]
    if short in WPF_SHORT_NAME_TO_FULL:
        return idx.type_table[WPF_SHORT_NAME_TO_FULL[short]]
    if short in idx.short_type_index:
        return idx.short_type_index[short][0]
    if resolved_name in idx.defs_by_class:
        d = idx.definitions[idx.defs_by_class[resolved_name]]
        kind = d.get("kind", "UserControl")
        uia_t = "Window" if kind in ("Window", "Form") else "Custom"
        return ResolvedTypeSpec(
            full_name=resolved_name,
            short_name=short,
            assembly=d.get("assembly", ""),
            base_full_name="",
            uia_control_type=uia_t,
            has_uia_peer=True,
        )
    inf_uia, inf_peer, inf_src = _infer_external_uia_type(resolved_name, short)
    return ResolvedTypeSpec(
        full_name=resolved_name,
        short_name=short,
        assembly="",
        base_full_name="",
        uia_control_type=inf_uia,
        has_uia_peer=inf_peer,
        type_source=inf_src,
    )


# ---------------------------------------------------------------------------
# Pass 2.5: Multi-Evidence MVVM View <-> ViewModel, DataTemplate & Region Indices
# ---------------------------------------------------------------------------
_VM_ROLE_SUFFIXES = ("ViewModel", "ViewModelBase", "VM", "Presenter", "Model")


def _vm_assembly(vm_cls: str, idx: WorkspaceIndex) -> str:
    asm = idx.vm_assembly_by_class.get(vm_cls, "")
    if asm:
        return asm
    tspec = idx.type_table.get(vm_cls)
    return tspec.assembly if tspec else ""


def _resolve_vm_class_name(raw_vm: str, src_assembly: str, idx: WorkspaceIndex) -> str:
    resolved = _resolve_raw_clr_token(raw_vm, idx)
    if resolved in idx.vm_props_by_class or resolved in idx.type_table:
        return resolved
    short = resolved.rsplit(".", 1)[-1]
    ref_asms = idx.assembly_refs.get(src_assembly, set())

    cands: List[str] = list(idx.vm_props_by_short_class.get(short, []))
    for tspec in idx.short_type_index.get(short, []):
        if tspec.full_name not in cands:
            cands.append(tspec.full_name)

    if cands:
        same_asm = [c for c in cands if _vm_assembly(c, idx) == src_assembly]
        if len(same_asm) == 1:
            return same_asm[0]
        if len(same_asm) > 1:
            return ""
        ref_hits = [c for c in cands if _vm_assembly(c, idx) in ref_asms]
        if len(ref_hits) == 1:
            return ref_hits[0]
        if len(ref_hits) > 1:
            return ""
        if len(cands) == 1:
            return cands[0]
        return ""
    return resolved


def _iter_vm_inheritance_chain(vm_cls: str, idx: WorkspaceIndex) -> List[str]:
    """Walk `vm_cls` -> base class chain (up to 6 hops) so inherited VM properties resolve transparently."""
    chain: List[str] = []
    seen: Set[str] = set()
    cur = vm_cls
    for _ in range(6):
        if not cur or cur in seen:
            break
        seen.add(cur)
        chain.append(cur)
        tspec = idx.type_table.get(cur)
        if not tspec or not tspec.base_full_name:
            break
        nxt = tspec.base_full_name
        if nxt.startswith("System."):
            break
        if nxt not in idx.vm_props_by_class:
            # Prefer same-namespace qualification for unqualified base class tokens (e.g. `DemoVM : BaseVM`)
            if "." not in nxt and "." in cur:
                same_ns_cand = f"{cur.rsplit('.', 1)[0]}.{nxt}"
                if same_ns_cand in idx.vm_props_by_class or (
                    same_ns_cand in idx.type_table and idx.type_table[same_ns_cand].assembly
                ):
                    nxt = same_ns_cand
            if nxt not in idx.vm_props_by_class:
                short_b = nxt.rsplit(".", 1)[-1]
                cur_asm = _vm_assembly(cur, idx)
                cands = idx.vm_props_by_short_class.get(short_b, [])
                same_asm_cands = [c for c in cands if cur_asm and _vm_assembly(c, idx) == cur_asm]
                if len(same_asm_cands) == 1:
                    nxt = same_asm_cands[0]
                elif len(cands) == 1:
                    nxt = cands[0]
                elif nxt not in idx.type_table or not idx.type_table[nxt].assembly:
                    tcands = [t.full_name for t in idx.short_type_index.get(short_b, []) if t.assembly]
                    if len(tcands) == 1:
                        nxt = tcands[0]
        cur = nxt
    return chain


def _is_validated_vm_class(vm_cls: str, idx: WorkspaceIndex) -> bool:
    """Return True iff `vm_cls` is not a View and its inheritance chain carries VM role/properties."""
    if not vm_cls or vm_cls in idx.defs_by_class:
        return False
    chain = _iter_vm_inheritance_chain(vm_cls, idx)
    has_props = any(c in idx.vm_props_by_class for c in chain)
    if not has_props:
        return False
    return any(c.rsplit(".", 1)[-1].endswith(_VM_ROLE_SUFFIXES) for c in chain)


def _build_mvvm_and_region_indices(idx: WorkspaceIndex) -> None:
    def _link_view_vm(view_def_id: str, vm_cls: str) -> None:
        if not view_def_id or not vm_cls:
            return
        lst = idx.view_to_vms.setdefault(view_def_id, [])
        if vm_cls not in lst:
            lst.append(vm_cls)

    # E1a: View-owned explicit DataTemplate (`VM_VIEW_MAP`), View DataContext (`VIEW_VM_LINK`), and `REGION_REGISTER`
    # E1b: External assembler (`EXT_VIEW_VM_LINK`) deferred until after E1a so View's own DataContext always wins priority.
    vm_short_view_candidates: Dict[str, Set[str]] = {}
    ext_vm_candidates: List[Tuple[str, str, str]] = []  # (src_asm, view_raw, vm_raw)
    ctor_candidates: List[Tuple[str, str, str]] = []  # (src_def_id, src_asm, raw_vm_type)

    for src_def_id, edge_list in sorted(idx.edges_by_src.items()):
        src_asm = src_def_id.split("::", 1)[0]
        for ed in edge_list:
            ek = ed["edge_kind"]
            if ek == "VM_VIEW_MAP":
                vm_raw = str(ed.get("src_elem_id") or "")
                view_raw = str(ed.get("dst_type_or_key") or "")
                view_def = _resolve_target_def_id(view_raw, src_asm, idx)
                vm_full = _resolve_vm_class_name(vm_raw, src_asm, idx)
                if view_def and vm_full:
                    idx.vm_to_view_def[vm_full] = view_def
                    vm_short = vm_full.rsplit(".", 1)[-1]
                    vm_short_view_candidates.setdefault(vm_short, set()).add(view_def)
                    _link_view_vm(view_def, vm_full)
            elif ek == "VIEW_VM_LINK":
                vm_full = _resolve_vm_class_name(str(ed.get("dst_type_or_key") or ""), src_asm, idx)
                if vm_full:
                    _link_view_vm(src_def_id, vm_full)
            elif ek == "EXT_VIEW_VM_LINK":
                ext_vm_candidates.append((
                    src_asm,
                    str(ed.get("src_elem_id") or ""),
                    str(ed.get("dst_type_or_key") or ""),
                ))
            elif ek == "VIEW_VM_CTOR":
                ctor_candidates.append((src_def_id, src_asm, str(ed.get("dst_type_or_key") or "")))
            elif ek == "REGION_REGISTER":
                reg_name = str(ed.get("src_elem_id") or "").strip()
                view_def = _resolve_target_def_id(str(ed.get("dst_type_or_key") or ""), src_asm, idx)
                if reg_name and view_def:
                    rlst = idx.region_views.setdefault(reg_name, [])
                    if view_def not in rlst:
                        rlst.append(view_def)

    for vm_short, vdefs in vm_short_view_candidates.items():
        if len(vdefs) == 1 and vm_short not in idx.vm_to_view_def:
            idx.vm_to_view_def[vm_short] = next(iter(vdefs))

    # E1b: External assembler `view.DataContext = vm;` (appended AFTER View-owned E1a links)
    for src_asm, view_raw, vm_raw in ext_vm_candidates:
        view_def = _resolve_target_def_id(view_raw, src_asm, idx)
        vm_full = _resolve_vm_class_name(vm_raw, src_asm, idx)
        if view_def and vm_full and (vm_full in idx.vm_props_by_class or vm_full in idx.type_table):
            _link_view_vm(view_def, vm_full)

    # E2: Deterministic constructor injection (`VIEW_VM_CTOR`) for Views without an E1 explicit DataContext link
    for view_def_id, src_asm, raw_vm in ctor_candidates:
        if view_def_id in idx.view_to_vms:
            continue
        vm_full = _resolve_vm_class_name(raw_vm, src_asm, idx)
        if _is_validated_vm_class(vm_full, idx):
            _link_view_vm(view_def_id, vm_full)

    # Pre-index all VM classes (both direct property holders and subclasses whose inheritance chain holds VM properties)
    all_vm_classes_by_short: Dict[str, List[str]] = {}
    all_vm_classes_set: Set[str] = set()
    for vc in sorted(idx.vm_props_by_class):
        if vc not in idx.defs_by_class:
            all_vm_classes_set.add(vc)
    for vc in sorted(idx.type_table):
        if vc not in all_vm_classes_set and vc not in idx.defs_by_class:
            if vc.rsplit(".", 1)[-1].endswith(_VM_ROLE_SUFFIXES) and _is_validated_vm_class(vc, idx):
                all_vm_classes_set.add(vc)
    for vc in sorted(all_vm_classes_set):
        all_vm_classes_by_short.setdefault(vc.rsplit(".", 1)[-1], []).append(vc)

    # E3: Type-graph & reference-closure naming fallback for remaining Views without E1/E2 links
    for def_id, d in sorted(idx.definitions.items()):
        if not d.get("is_shipping", True):
            continue
        if d.get("kind") not in ("Window", "UserControl", "Page", "Form"):
            continue
        if def_id in idx.view_to_vms:
            continue
        fcls = d.get("full_class_name") or ""
        short = fcls.rsplit(".", 1)[-1] if fcls else ""
        if not short:
            continue
        asm = d.get("assembly", "")
        ref_asms = idx.assembly_refs.get(asm, set())
        stem = short
        for sfx in ("Window", "Dialog", "UserControl", "Control", "View", "Page"):
            if stem.endswith(sfx) and len(stem) > len(sfx):
                stem = stem[: -len(sfx)]
                break

        candidate_shorts = [f"{short}Model", f"{stem}ViewModel", f"{stem}VM", f"{stem}ViewModelBase"]
        matched_vm = ""
        for cand_s in candidate_shorts:
            hits = all_vm_classes_by_short.get(cand_s, [])
            if not hits:
                continue
            same_asm_hits = [h for h in hits if _vm_assembly(h, idx) == asm or h.startswith(asm + ".")]
            if len(same_asm_hits) == 1:
                matched_vm = same_asm_hits[0]
                break
            ref_asm_hits = [h for h in hits if _vm_assembly(h, idx) in ref_asms]
            if len(ref_asm_hits) == 1:
                matched_vm = ref_asm_hits[0]
                break
            if len(hits) == 1:
                matched_vm = hits[0]
                break

        # Unique same-assembly or referenced-assembly prefix fallback (e.g. `AutoFilmView` -> sole `AutoFilm*ViewModel*`)
        if not matched_vm and len(stem) >= 4 and asm:
            scoped_vms = [
                vc
                for vc in all_vm_classes_set
                if (_vm_assembly(vc, idx) == asm or vc.startswith(asm + "."))
                and vc.rsplit(".", 1)[-1].startswith(stem)
                and vc.rsplit(".", 1)[-1].endswith(("ViewModel", "ViewModelBase", "VM"))
            ]
            if len(scoped_vms) == 1:
                matched_vm = scoped_vms[0]
            elif not scoped_vms and ref_asms:
                ref_vms = [
                    vc
                    for vc in all_vm_classes_set
                    if _vm_assembly(vc, idx) in ref_asms
                    and vc.rsplit(".", 1)[-1].startswith(stem)
                    and vc.rsplit(".", 1)[-1].endswith(("ViewModel", "ViewModelBase", "VM"))
                ]
                if len(ref_vms) == 1:
                    matched_vm = ref_vms[0]

        if matched_vm:
            _link_view_vm(def_id, matched_vm)


def _lookup_in_ref_assemblies(
    key: str,
    ref_asms: Set[str],
    en_table: Dict[Tuple[str, str], str],
    zh_table: Dict[Tuple[str, str], str],
    amb_en: Set[Tuple[str, str]],
    amb_zh: Set[Tuple[str, str]],
) -> Tuple[str, str, bool, bool]:
    """Return `(en_val, zh_val, en_ambiguous, zh_ambiguous)` across `ref_asms` propagating internal assembly ambiguity."""
    ref_en_amb = any((ra, key) in amb_en for ra in ref_asms)
    ref_zh_amb = any((ra, key) in amb_zh for ra in ref_asms)
    en_val = ""
    zh_val = ""
    if not ref_en_amb:
        en_bucket: Dict[str, Dict[str, str]] = {}
        for ra in ref_asms:
            v = en_table.get((ra, key), "")
            if v:
                _record_equiv_value(en_bucket, "_", v, _norm_en_for_eq)
        eq_en = en_bucket.get("_", {})
        if len(eq_en) == 1:
            en_val = next(iter(eq_en.values()))
        elif len(eq_en) > 1:
            ref_en_amb = True
    if not ref_zh_amb and not ref_en_amb:
        zh_bucket: Dict[str, Dict[str, str]] = {}
        for ra in ref_asms:
            v = zh_table.get((ra, key), "")
            if v:
                _record_equiv_value(zh_bucket, "_", v, _norm_zh_for_eq)
        eq_zh = zh_bucket.get("_", {})
        if len(eq_zh) == 1:
            zh_val = next(iter(eq_zh.values()))
        elif len(eq_zh) > 1:
            ref_zh_amb = True
    return en_val, zh_val, ref_en_amb, ref_zh_amb


def _lookup_closed_loc_key(key: str, assembly: str, idx: WorkspaceIndex) -> Optional[Tuple[str, str]]:
    """Carrier-tiered & scoped closed localization lookup with independent EN/ZH disambiguation.

    Tier 1: Same assembly (all localization carriers, respecting same-assembly ambiguity)
    Tier 2: Transitive `.csproj` referenced assemblies — source `.resx`/`.resdict`/`flat_resources`
    Tier 3: Unambiguous global standalone `flat_resources` runtime dictionary (isolated from DLL `scoped_resources`!)
    Tier 3.5: Transitive `.csproj` referenced assemblies — DLL-embedded `scoped_resources`
    Tier 4: Global unambiguous closed localization fallback (EN conflict blocks orphan ZH; ZH conflict preserves unique EN)
    """
    if not key:
        return None
    en_val = ""
    zh_val = ""
    en_blocked = False

    # Tier 1: Same assembly
    if (assembly, key) in idx.loc_by_asm_ambiguous_en:
        en_blocked = True
    else:
        en_val = idx.loc_by_asm_en.get((assembly, key), "")
        if (assembly, key) not in idx.loc_by_asm_ambiguous_zh:
            zh_val = idx.loc_by_asm_zh.get((assembly, key), "")

    # Tier 2: Referenced assemblies — source `.resx`/`.resdict`/`flat_resources`
    ref_asms = idx.assembly_refs.get(assembly) or set()
    if ref_asms and not en_blocked and (not en_val or not zh_val):
        r_en, r_zh, r_en_amb, _ = _lookup_in_ref_assemblies(
            key,
            ref_asms,
            idx.loc_src_by_asm_en,
            idx.loc_src_by_asm_zh,
            idx.loc_src_by_asm_ambiguous_en,
            idx.loc_src_by_asm_ambiguous_zh,
        )
        if r_en_amb:
            en_blocked = True
        else:
            if not en_val and r_en:
                en_val = r_en
            if not zh_val and r_zh:
                zh_val = r_zh

    # Tier 3: Unambiguous standalone `flat_resources` runtime dictionary (beats unrelated referenced platform DLLs!)
    if not en_blocked and key not in idx.loc_flat_ambiguous_en:
        if not en_val:
            en_val = idx.loc_flat_en.get(key, "")
        if not zh_val and key not in idx.loc_flat_ambiguous_zh:
            zh_val = idx.loc_flat_zh.get(key, "")

    # Tier 3.5: Referenced assemblies — DLL-embedded `scoped_resources`
    if ref_asms and not en_blocked and (not en_val or not zh_val):
        d_en, d_zh, d_en_amb, _ = _lookup_in_ref_assemblies(
            key,
            ref_asms,
            idx.loc_dll_by_asm_en,
            idx.loc_dll_by_asm_zh,
            idx.loc_dll_by_asm_ambiguous_en,
            idx.loc_dll_by_asm_ambiguous_zh,
        )
        if d_en_amb:
            en_blocked = True
        else:
            if not en_val and d_en:
                en_val = d_en
            if not zh_val and d_zh:
                zh_val = d_zh

    # Tier 4: Global unambiguous closed localization fallback (EN conflict blocks orphan ZH; ZH conflict preserves unique EN)
    if not en_blocked and key not in idx.loc_ambiguous_en_keys and key not in idx.loc_flat_ambiguous_en:
        if not en_val:
            en_val = idx.loc_global_en.get(key, "")
        if not zh_val and key not in idx.loc_ambiguous_zh_keys and key not in idx.loc_flat_ambiguous_zh:
            zh_val = idx.loc_global_zh.get(key, "")

    if en_val or zh_val:
        return en_val, zh_val
    return None


def _lookup_member_entry(vm_cls: str, prop: str, idx: WorkspaceIndex) -> Optional[Tuple[str, str]]:
    for cls_name in _iter_vm_inheritance_chain(vm_cls, idx):
        pmap = idx.vm_props_by_class.get(cls_name)
        if pmap and prop in pmap:
            return pmap[prop]
    return None


def _finalize_uia_text(en: str, zh: str, ok: bool, uia_type: str) -> Tuple[str, str, bool]:
    """Single top-level UIA projection exit: strip WPF `_` access-key mnemonics ONCE for eligible control types."""
    if not ok:
        return "", "", False
    if uia_type in _ACCESS_KEY_STRIPPING_UIA_TYPES:
        out_en = _strip_access_key(en) if en else en
        out_zh = _strip_access_key(zh) if zh else zh
    else:
        out_en, out_zh = en, zh
    return out_en, out_zh, bool(out_en or out_zh)


def _resolve_member_value_flow_raw(
    owner_classes: Sequence[str],
    start_prop: str,
    assembly: str,
    owner_class_short: str,
    idx: WorkspaceIndex,
    *,
    depth: int = 0,
) -> Tuple[str, str, bool]:
    """Evaluate a C# member value-flow node into RAW un-stripped `(val_en, val_zh, is_static)`."""
    if depth > 4:
        return "", "", False
    for vm_cls in owner_classes:
        cur_prop = start_prop
        visited: Set[str] = set()
        for _ in range(5):
            if cur_prop in visited:
                break
            visited.add(cur_prop)
            entry = _lookup_member_entry(vm_cls, cur_prop, idx)
            if not entry:
                break
            kind, val = entry
            if kind == "dynamic_multi":
                return "", "", False
            if kind == "alias":
                cur_prop = val
                continue
            if kind == "literal":
                return val, "", bool(val)
            if kind in ("key_probe", "res_key"):
                pair = _lookup_closed_loc_key(val, assembly, idx)
                if pair:
                    en_v, zh_v = pair
                    if en_v.startswith("{"):
                        e2, z2, ok2, _ = _eval_markup_expr(
                            parse_markup_expr(en_v),
                            assembly,
                            owner_class_short,
                            idx,
                            depth=depth + 1,
                        )
                        return e2, z2 or zh_v, ok2
                    return en_v, zh_v, bool(en_v or zh_v)
                break
            if kind in ("static_ref", "xstatic"):
                e2, z2, ok2, _ = _eval_markup_expr(
                    parse_markup_expr(f"{{x:Static {val}}}"),
                    assembly,
                    owner_class_short,
                    idx,
                    depth=depth + 1,
                )
                return e2, z2, ok2
            break
    return "", "", False


def _resolve_member_value_flow(
    owner_classes: Sequence[str],
    start_prop: str,
    assembly: str,
    owner_class_short: str,
    uia_type: str,
    idx: WorkspaceIndex,
) -> Tuple[str, str, bool]:
    """Evaluate a C# member value-flow node and apply top-level UIA access-key finalization."""
    en_v, zh_v, ok = _resolve_member_value_flow_raw(owner_classes, start_prop, assembly, owner_class_short, idx)
    return _finalize_uia_text(en_v, zh_v, ok, uia_type)


def _resolve_vm_property_text_raw(
    binding_path: str,
    target_def_id: str,
    assembly: str,
    owner_class_short: str,
    idx: WorkspaceIndex,
    *,
    depth: int = 0,
) -> Tuple[str, str, bool]:
    clean_path = (binding_path or "").strip()
    if not clean_path or "." in clean_path or "[" in clean_path:
        return "", "", False

    vm_candidates: List[str] = list(idx.view_to_vms.get(target_def_id, []))
    owner_def = idx.definitions.get(target_def_id) or {}
    owner_full = owner_def.get("full_class_name") or ""
    if owner_full and owner_full not in vm_candidates:
        vm_candidates.append(owner_full)

    return _resolve_member_value_flow_raw(
        vm_candidates, clean_path, assembly, owner_class_short, idx, depth=depth
    )


def _lookup_scoped_cls_key(
    assembly: str,
    rcls: str,
    key: str,
    idx: WorkspaceIndex,
) -> Optional[Tuple[str, str]]:
    """Lookup `(rcls, key)` preferring same assembly, then unique referenced assembly, then unambiguous global."""
    pair = idx.strings_by_asm_cls_key.get((assembly, rcls, key))
    if pair and (pair[0] or pair[1]):
        return pair
    ref_asms = idx.assembly_refs.get(assembly)
    if ref_asms:
        en_b: Dict[str, Dict[str, str]] = {}
        zh_b: Dict[str, Dict[str, str]] = {}
        for ra in ref_asms:
            rp = idx.strings_by_asm_cls_key.get((ra, rcls, key))
            if rp:
                _record_equiv_value(en_b, "_", rp[0], _norm_en_for_eq)
                _record_equiv_value(zh_b, "_", rp[1], _norm_zh_for_eq)
        eq_en = en_b.get("_", {})
        eq_zh = zh_b.get("_", {})
        if len(eq_en) > 1:
            return None
        if len(eq_en) == 1 or len(eq_zh) == 1:
            return (
                next(iter(eq_en.values())) if len(eq_en) == 1 else "",
                next(iter(eq_zh.values())) if len(eq_zh) == 1 else "",
            )
    return idx.strings_by_cls_key.get((rcls, key))


def _relative_source_targets_window(rel_node: Optional[MarkupExpr]) -> bool:
    if not isinstance(rel_node, MarkupCall):
        return False
    anc = rel_node.named_map_lower().get("ancestortype")
    if isinstance(anc, MarkupCall) and anc.local_name == "Type":
        anc = anc.named_map_lower().get("typename") or (anc.pos_args[0] if anc.pos_args else None)
    if isinstance(anc, MarkupLiteral):
        tok = anc.value.strip().split(":", 1)[-1]
        return tok == "Window" or tok.endswith(("Window", "Dialog", "Form"))
    return False


# ---------------------------------------------------------------------------
# Recursive XAML MarkupExpr Evaluator & Localization Resolution
# ---------------------------------------------------------------------------
def _eval_markup_expr(
    node: MarkupExpr,
    assembly: str,
    owner_class_short: str,
    idx: WorkspaceIndex,
    *,
    depth: int = 0,
    owner_def_id: str = "",
    host_view_def_id: str = "",
    enclosing_window_def_id: str = "",
    in_template: bool = False,
) -> Tuple[str, str, bool, str]:
    """Recursively evaluate a `MarkupExpr` AST into RAW un-stripped `(val_en, val_zh, is_static, text_source)`."""
    if depth > 4:
        return "", "", False, ""

    if isinstance(node, MarkupLiteral):
        val = node.value
        return val, "", bool(val), ("literal" if val else "")

    assert isinstance(node, MarkupCall)
    lname = node.local_name
    pfx = node.prefix
    nmap = node.named_map_lower()

    # WinForms internal `{Resx:Key}` syntax
    if pfx == "Resx":
        rkey = node.type_name[5:]
        pair = (
            _lookup_scoped_cls_key(assembly, owner_class_short, rkey, idx)
            or _lookup_scoped_cls_key(assembly, "Resources", rkey, idx)
        )
        if pair and (pair[0] or pair[1]):
            return pair[0], pair[1], True, "resource"
        return "", "", False, ""

    # `{x:Static [Member=][prefix:]ResClass.Key}` (top-level only; never pierces outer wrapper extensions!)
    if lname == "Static" and pfx in ("x", ""):
        target_node = nmap.get("member") or (node.pos_args[0] if node.pos_args else None)
        if not isinstance(target_node, MarkupLiteral):
            return "", "", False, ""
        token = target_node.value.strip()
        if ":" in token:
            token = token.split(":", 1)[1]
        if "." not in token:
            return "", "", False, ""
        rcls, key = token.rsplit(".", 1)
        pair = _lookup_scoped_cls_key(assembly, rcls, key, idx)
        if not pair and key.endswith("Command"):
            short_k = key[: -len("Command")]
            pair = _lookup_scoped_cls_key(assembly, rcls, short_k, idx)
        if not pair:
            pair = _lookup_closed_loc_key(key, assembly, idx)
        if pair and (pair[0] or pair[1]):
            en_v, zh_v = pair
            if en_v.startswith("{"):
                e2, z2, ok2, src2 = _eval_markup_expr(
                    parse_markup_expr(en_v),
                    assembly,
                    owner_class_short,
                    idx,
                    depth=depth + 1,
                    owner_def_id=owner_def_id,
                    host_view_def_id=host_view_def_id,
                    enclosing_window_def_id=enclosing_window_def_id,
                    in_template=in_template,
                )
                return e2, z2 or zh_v, ok2, (src2 or "xstatic")
            return en_v, zh_v, True, "xstatic"
        return "", "", False, ""

    # `{StaticResource Key}` / `{DynamicResource Key}` (same assembly -> referenced assemblies -> unambiguous global)
    if lname in ("StaticResource", "DynamicResource"):
        target_node = nmap.get("resourcekey") or (node.pos_args[0] if node.pos_args else None)
        if not isinstance(target_node, MarkupLiteral):
            return "", "", False, ""
        rkey = target_node.value.strip()
        pair = _lookup_scoped_cls_key(assembly, "$ResourceDictionary", rkey, idx) or idx.strings_by_key.get(rkey)
        if pair and (pair[0] or pair[1]):
            return pair[0], pair[1], True, "resource"
        return "", "", False, ""

    # `{Binding ...}` (respects RelativeSource AncestorType=Window vs DataGrid/UserControl for `DataContext.<Prop>`)
    if lname == "Binding":
        if "source" in nmap or "elementname" in nmap:
            return "", "", False, ""
        rel_node = nmap.get("relativesource")
        has_rel = rel_node is not None
        path_node = nmap.get("path") or (node.pos_args[0] if node.pos_args else None)
        if not isinstance(path_node, MarkupLiteral):
            return "", "", False, ""
        raw_path = path_node.value.strip()
        if not raw_path:
            return "", "", False, ""

        if raw_path.startswith("DataContext.") and has_rel:
            clean_prop = raw_path[len("DataContext.") :]
            if _relative_source_targets_window(rel_node) and enclosing_window_def_id:
                target_def = enclosing_window_def_id
            else:
                target_def = host_view_def_id or owner_def_id
        elif not in_template and not has_rel:
            clean_prop = raw_path
            target_def = owner_def_id
        else:
            return "", "", False, ""

        if target_def and clean_prop and "." not in clean_prop and "[" not in clean_prop:
            en_v, zh_v, ok = _resolve_vm_property_text_raw(
                clean_prop, target_def, assembly, owner_class_short, idx, depth=depth + 1
            )
            if ok:
                return en_v, zh_v, True, "vm_binding_static"
        return "", "", False, ""

    # Built-in non-static markup extensions
    if lname in ("TemplateBinding", "MultiBinding", "RelativeSource", "Type", "Null", "Array", "StaticExtension"):
        return "", "", False, ""

    # Generic custom MarkupExtension (e.g. `{ext:LocalizableTextResource Key={x:Static ...}, ResourceService=...}`,
    # `{loc:Loc KEY}`, `{lex:Loc Key=KEY}`): recursively evaluate key arguments and strictly verify against
    # the closed localization universe; NEVER leak unresolved raw keys!
    prioritized_args: List[MarkupExpr] = []
    secondary_args: List[MarkupExpr] = []
    for arg_name, arg_expr in node.named_args:
        alow = arg_name.lower()
        if any(tok in alow for tok in _MARKUP_SKIP_ARG_TOKENS):
            continue
        if alow in _MARKUP_KEY_ARG_PRIORITY:
            prioritized_args.append(arg_expr)
        else:
            secondary_args.append(arg_expr)
    candidate_nodes = prioritized_args + list(node.pos_args) + secondary_args

    for cand_node in candidate_nodes:
        cand_en, _, cand_ok, _ = _eval_markup_expr(
            cand_node,
            assembly,
            owner_class_short,
            idx,
            depth=depth + 1,
            owner_def_id=owner_def_id,
            host_view_def_id=host_view_def_id,
            enclosing_window_def_id=enclosing_window_def_id,
            in_template=in_template,
        )
        if cand_ok and cand_en:
            loc_pair = _lookup_closed_loc_key(cand_en.strip(), assembly, idx)
            if loc_pair and (loc_pair[0] or loc_pair[1]):
                return loc_pair[0], loc_pair[1], True, "custom_loc_markup"

    return "", "", False, ""


def resolve_i18n_expr_with_source(
    raw_expr: str,
    assembly: str,
    owner_class_short: str,
    uia_type: str,
    idx: WorkspaceIndex,
    *,
    owner_def_id: str = "",
    host_view_def_id: str = "",
    enclosing_window_def_id: str = "",
    in_template: bool = False,
) -> Tuple[str, str, bool, str]:
    """Evaluate a raw XAML/WinForms text expression and apply top-level UIA access-key finalization ONCE."""
    if not raw_expr or not raw_expr.strip():
        return "", "", False, ""
    ast = parse_markup_expr(raw_expr)
    en_raw, zh_raw, ok, src = _eval_markup_expr(
        ast,
        assembly,
        owner_class_short,
        idx,
        depth=0,
        owner_def_id=owner_def_id,
        host_view_def_id=host_view_def_id,
        enclosing_window_def_id=enclosing_window_def_id,
        in_template=in_template,
    )
    en_fin, zh_fin, ok_fin = _finalize_uia_text(en_raw, zh_raw, ok, uia_type)
    return en_fin, zh_fin, ok_fin, (src if ok_fin else "")


def resolve_i18n_expr(
    raw_expr: str,
    assembly: str,
    owner_class_short: str,
    uia_type: str,
    idx: WorkspaceIndex,
    depth: int = 0,
    owner_def_id: str = "",
    host_view_def_id: str = "",
    enclosing_window_def_id: str = "",
    in_template: bool = False,
) -> Tuple[str, str, bool]:
    """Return `(val_en, val_zh, is_static)` for a raw XAML/WinForms text or tooltip expression."""
    if depth > 4:
        return "", "", False
    en_fin, zh_fin, ok_fin, _ = resolve_i18n_expr_with_source(
        raw_expr,
        assembly,
        owner_class_short,
        uia_type,
        idx,
        owner_def_id=owner_def_id,
        host_view_def_id=host_view_def_id,
        enclosing_window_def_id=enclosing_window_def_id,
        in_template=in_template,
    )
    return en_fin, zh_fin, ok_fin


# ---------------------------------------------------------------------------
# Pass 3 & 4: Governance Audits & Wiring Graph Analysis
# ---------------------------------------------------------------------------
def _collect_uid_shadow_issues(idx: WorkspaceIndex) -> None:
    for def_id, d in sorted(idx.definitions.items()):
        if not d.get("is_shipping", True) or d.get("framework") != "WPF":
            continue
        elems = idx.elements_by_def.get(def_id, [])
        seen_uids: Dict[str, int] = {}
        for el in elems:
            if el.get("context") in ("ControlTemplate", "DataTemplate", "HeaderTemplate"):
                continue
            attrs = el.get("attrs") or {}
            x_uid = attrs.get("x_uid", "")
            x_name = attrs.get("x_name", "")
            explicit_aid = attrs.get("explicit_auto_id", "")
            if x_uid:
                if x_uid in seen_uids:
                    idx.uid_shadow_issues.append(
                        UidShadowIssue(
                            severity="High",
                            issue_kind="DUPLICATE_UID_IN_DEF",
                            repo_id=d["repo_id"],
                            assembly=d["assembly"],
                            file=d["file"],
                            line=el.get("line", 0),
                            def_id=def_id,
                            clr_type=el["clr_type"].rsplit(".", 1)[-1],
                            x_uid=x_uid,
                            x_name=x_name,
                            effective_auto_id=explicit_aid or x_uid,
                            recommendation=f"Duplicate x:Uid='{x_uid}' (first at line {seen_uids[x_uid]}); assign unique x:Uid/AutomationId.",
                        )
                    )
                else:
                    seen_uids[x_uid] = el.get("line", 0)

            if x_uid and x_name and x_uid != x_name and not explicit_aid:
                idx.uid_shadow_issues.append(
                    UidShadowIssue(
                        severity="Medium",
                        issue_kind="UID_SHADOWS_NAME",
                        repo_id=d["repo_id"],
                        assembly=d["assembly"],
                        file=d["file"],
                        line=el.get("line", 0),
                        def_id=def_id,
                        clr_type=el["clr_type"].rsplit(".", 1)[-1],
                        x_uid=x_uid,
                        x_name=x_name,
                        effective_auto_id=x_uid,
                        recommendation=f"Runtime UIA @auto_id exposes x:Uid='{x_uid}' instead of x:Name='{x_name}'; sync x:Uid with x:Name or set AutomationProperties.AutomationId.",
                    )
                )


def _resolve_target_def_id(target_token: str, src_assembly: str, idx: WorkspaceIndex) -> Optional[str]:
    if not target_token:
        return None
    if target_token in idx.definitions:
        return target_token
    resolved_clr = _resolve_raw_clr_token(target_token, idx)
    if resolved_clr.startswith(("System.", "Microsoft.", "x:")):
        return None
    if resolved_clr in idx.defs_by_class:
        return idx.defs_by_class[resolved_clr]
    if "." in resolved_clr or "::" in resolved_clr:
        return None
    cands = idx.defs_by_short_class.get(resolved_clr, [])
    if not cands:
        return None
    same_asm = [cid for cid in cands if cid.startswith(f"{src_assembly}::")]
    if same_asm:
        return same_asm[0]
    ref_asms = idx.assembly_refs.get(src_assembly)
    if ref_asms:
        ref_hits = [cid for cid in cands if cid.split("::", 1)[0] in ref_asms]
        if len(ref_hits) == 1:
            return ref_hits[0]
        if len(ref_hits) > 1:
            return None
    return cands[0]


_NON_CONTAINER_SLOT_UIA_TYPES = frozenset({
    "Button", "CheckBox", "RadioButton", "Edit", "Document",
    "Slider", "Spinner", "Text", "Image", "Hyperlink",
})
_CONTENT_HOST_SHORT_NAMES = frozenset({
    "ContentControl", "UserControl", "ContentPresenter", "HeaderedContentControl",
})


def _def_container_slots(def_id: str, idx: WorkspaceIndex) -> Set[str]:
    """Return all valid container slot `x:Name`/`x:Uid` values in `def_id` (excluding leaf controls)."""
    names: Set[str] = set()
    for el in idx.elements_by_def.get(def_id, []):
        attrs = el.get("attrs") or {}
        if not (attrs.get("x_name") or attrs.get("x_uid")):
            continue
        tspec = resolve_element_type_spec(el["clr_type"], idx)
        if tspec.uia_control_type in _NON_CONTAINER_SLOT_UIA_TYPES:
            continue
        if attrs.get("x_name"):
            names.add(attrs["x_name"])
        if attrs.get("x_uid"):
            names.add(attrs["x_uid"])
    return names


def _resolve_content_binding_mounts(
    def_id: str,
    el: Dict[str, Any],
    tspec: ResolvedTypeSpec,
    idx: WorkspaceIndex,
) -> List[str]:
    """Resolve DataTemplate VM->View mounts for `<ContentControl Content=\"{Binding SubVmProp}\"/>`."""
    if tspec.short_name not in _CONTENT_HOST_SHORT_NAMES:
        return []
    attrs = el.get("attrs") or {}
    tprops = attrs.get("text_props") or {}
    content_expr = (tprops.get("content") or attrs.get("raw_text_expr") or "").strip()
    if not content_expr.startswith("{Binding"):
        return []
    bprop = _parse_xaml_binding_path(content_expr)
    if not bprop or "." in bprop or "[" in bprop:
        return []

    mounts: List[str] = []
    vm_candidates: List[str] = list(idx.view_to_vms.get(def_id, []))
    owner_def = idx.definitions.get(def_id) or {}
    owner_full = owner_def.get("full_class_name") or ""
    if owner_full and owner_full not in vm_candidates:
        vm_candidates.append(owner_full)
    for vm_cls in vm_candidates:
        vp_ent = _lookup_member_entry(vm_cls, bprop, idx)
        if vp_ent and vp_ent[0] == "type_ref":
            sub_vm_type = vp_ent[1]
            mapped_vdef = (
                idx.vm_to_view_def.get(sub_vm_type)
                or idx.vm_to_view_def.get(sub_vm_type.rsplit(".", 1)[-1])
            )
            if mapped_vdef and mapped_vdef != def_id and mapped_vdef not in mounts:
                mounts.append(mapped_vdef)
    return mounts


def _analyze_wiring_and_shared_components(idx: WorkspaceIndex) -> None:
    """Build the single authoritative composition graph (`idx.resolved_mounts`) and derive all audits from it."""
    real_host_kinds = frozenset({"Window", "UserControl", "Page", "Form"})
    inbound_parents: Dict[str, Set[str]] = {}
    referenced_defs: Set[str] = set()
    container_slots_cache: Dict[str, Set[str]] = {}
    resolved_slot_keys_by_def: Dict[str, Set[str]] = {}

    def _is_real_host_def(def_id: str) -> bool:
        d_obj = idx.definitions.get(def_id)
        return d_obj is not None and d_obj.get("kind") in real_host_kinds

    def _record_mount(parent_def_id: str, slot_key: str, target_def_id: str) -> None:
        if not (parent_def_id and target_def_id) or parent_def_id == target_def_id:
            return
        pmap = idx.resolved_mounts.setdefault(parent_def_id, {})
        lst = pmap.setdefault(slot_key, [])
        if target_def_id not in lst:
            lst.append(target_def_id)
        referenced_defs.add(target_def_id)
        if _is_real_host_def(parent_def_id):
            inbound_parents.setdefault(target_def_id, set()).add(parent_def_id)
        resolved_slot_keys_by_def.setdefault(parent_def_id, set()).add(slot_key)

    # 1. Resolve declarative XAML Region mounts and `<ContentControl Content="{Binding SubVm}"/>` + `VM_VIEW_MAP` mounts
    for def_id, d in sorted(idx.definitions.items()):
        if not d.get("is_shipping", True):
            continue
        for el in idx.elements_by_def.get(def_id, []):
            attrs = el.get("attrs") or {}
            eid = el["elem_id"]
            reg_name = attrs.get("region_name", "")
            if reg_name:
                for rv in idx.region_views.get(reg_name, []):
                    _record_mount(def_id, f"region:{reg_name}", rv)
                    resolved_slot_keys_by_def.setdefault(def_id, set()).add(eid)
            tspec = resolve_element_type_spec(el["clr_type"], idx)
            for dt_mount in _resolve_content_binding_mounts(def_id, el, tspec, idx):
                _record_mount(def_id, f"elem:{eid}", dt_mount)
                resolved_slot_keys_by_def.setdefault(def_id, set()).add(eid)

    # 2. Walk explicit wiring edges (`XAML_EMBED`, `CS_ASSIGN`, validated `CS_GENERIC_MOUNT`, `ADDIN_MOUNT`, `REGION_REGISTER`, `VM_VIEW_MAP`, `OPENS_DIALOG`)
    pending_dynamic_slots: List[Tuple[str, str, str, str, str, str]] = []
    for src_def_id, edge_list in sorted(idx.edges_by_src.items()):
        src_def = idx.definitions.get(src_def_id)
        src_asm = src_def["assembly"] if src_def else src_def_id.split("::", 1)[0]
        src_repo = src_def["repo_id"] if src_def else ""
        src_file = src_def["file"] if src_def else ""

        for ed in edge_list:
            ek = ed["edge_kind"]
            dst = ed["dst_type_or_key"]
            if ek == "DYNAMIC_SLOT":
                pending_dynamic_slots.append((
                    src_def_id,
                    src_repo,
                    src_asm,
                    ed.get("extra", {}).get("file") or src_file,
                    str(ed.get("src_elem_id") or ""),
                    dst,
                ))
                continue

            if ek == "CS_GENERIC_MOUNT":
                # Bidirectional verification: slot must be a container element in `src_def_id` AND `T` must be a shipping UserControl/Page
                slot_id = str(ed.get("src_elem_id") or "")
                if src_def_id not in container_slots_cache:
                    container_slots_cache[src_def_id] = _def_container_slots(src_def_id, idx)
                if slot_id and slot_id in container_slots_cache[src_def_id]:
                    target_def = _resolve_target_def_id(dst, src_asm, idx)
                    if target_def and (idx.definitions.get(target_def) or {}).get("kind") in ("UserControl", "Page"):
                        _record_mount(src_def_id, f"slot:{slot_id}", target_def)
                continue

            if ek in ("VM_VIEW_MAP", "REGION_REGISTER"):
                target_def = _resolve_target_def_id(dst, src_asm, idx)
                if target_def:
                    referenced_defs.add(target_def)
                continue

            if ek in ("XAML_EMBED", "CS_ASSIGN", "ADDIN_MOUNT", "OPENS_DIALOG"):
                target_def = _resolve_target_def_id(dst, src_asm, idx)
                if target_def:
                    referenced_defs.add(target_def)
                    if ek == "CS_ASSIGN":
                        slot_id = str(ed.get("src_elem_id") or "")
                        if slot_id:
                            _record_mount(src_def_id, f"slot:{slot_id}", target_def)
                        elif _is_real_host_def(src_def_id):
                            inbound_parents.setdefault(target_def, set()).add(src_def_id)
                    elif ek == "ADDIN_MOUNT":
                        inbound_parents.setdefault(target_def, set()).add(src_def_id)
                    elif ek == "XAML_EMBED" and _is_real_host_def(src_def_id):
                        inbound_parents.setdefault(target_def, set()).add(src_def_id)
                else:
                    resolved_clr = _resolve_raw_clr_token(dst, idx)
                    short_clr = resolved_clr.rsplit(".", 1)[-1]
                    if (
                        resolved_clr not in idx.type_table
                        and "::" not in resolved_clr
                        and not resolved_clr.startswith("System.")
                        and not _is_embedded_web_host_type(resolved_clr, short_clr)
                    ):
                        idx.dynamic_unmounted_issues.append(
                            DynamicUnmountedIssue(
                                category="PENDING_EXTERNAL_TYPE",
                                repo_id=src_repo,
                                assembly=src_asm,
                                file=ed.get("extra", {}).get("file") or src_file,
                                src_def_id=src_def_id,
                                slot_or_target=resolved_clr,
                                detail=f"Referenced UI type '{resolved_clr}' via {ek} is defined in an unscanned external repository/assembly.",
                            )
                        )

    # 3. Emit `DYNAMIC_SLOT` ONLY for slots that were NOT statically resolved into `idx.resolved_mounts`
    for src_def_id, src_repo, src_asm, src_file, slot_id, dst in pending_dynamic_slots:
        resolved_slots = resolved_slot_keys_by_def.get(src_def_id, set())
        if slot_id and (slot_id in resolved_slots or f"elem:{slot_id}" in resolved_slots or f"slot:{slot_id}" in resolved_slots):
            continue
        idx.dynamic_unmounted_issues.append(
            DynamicUnmountedIssue(
                category="DYNAMIC_SLOT",
                repo_id=src_repo,
                assembly=src_asm,
                file=src_file,
                src_def_id=src_def_id,
                slot_or_target=slot_id or dst,
                detail=f"Polymorphic runtime Content/slot bound to '{dst}'",
            )
        )

    for def_id, parents in sorted(inbound_parents.items()):
        d = idx.definitions.get(def_id)
        if d and d.get("kind") == "UserControl" and len(parents) >= 2:
            idx.shared_component_defs[def_id] = len(parents)

    for def_id, d in sorted(idx.definitions.items()):
        if not d.get("is_shipping", True):
            continue
        if d.get("kind") == "UserControl" and def_id not in referenced_defs:
            idx.dynamic_unmounted_issues.append(
                DynamicUnmountedIssue(
                    category="UNREFERENCED_VIEW",
                    repo_id=d["repo_id"],
                    assembly=d["assembly"],
                    file=d["file"],
                    src_def_id=def_id,
                    slot_or_target=d.get("full_class_name") or def_id,
                    detail="Shipping UserControl has 0 static XAML/C#/AddIn references in currently scanned repositories (either mounted dynamically or legacy/unused).",
                )
            )


# ---------------------------------------------------------------------------
# Pass 5: Surface Discovery & UIA Shell Dissolution Projection
# ---------------------------------------------------------------------------
def _project_all_surfaces(idx: WorkspaceIndex, *, default_locale: str = "en-US") -> None:
    # 1. Discover composite multi-mode hosts (e.g., CTViewer -> TwoD / MPR / Volume / Endo)
    composite_hosts: Dict[str, List[str]] = {}  # host_prefix_asm -> [sub_app_class_or_ns]
    addin_mounts_by_asm: Dict[str, List[Dict[str, Any]]] = {}
    dialogs_opened_by_def: Dict[str, List[str]] = {}

    for src_def_id, edges in idx.edges_by_src.items():
        src_asm = src_def_id.split("::", 1)[0]
        for ed in edges:
            ek = ed["edge_kind"]
            if ek == "ADDIN_MOUNT":
                if ed.get("src_elem_id") == "CompositeSubApps":
                    composite_hosts.setdefault(src_asm, []).append(ed["dst_type_or_key"])
                else:
                    addin_mounts_by_asm.setdefault(src_asm, []).append(ed)
            elif ek == "OPENS_DIALOG":
                tdef = _resolve_target_def_id(ed["dst_type_or_key"], src_asm, idx)
                if tdef:
                    dialogs_opened_by_def.setdefault(src_def_id, []).append(tdef)

    mounted_into_surface: Set[str] = set()

    # A. Shipping Top-Level Windows & WinForms Forms (and Modal Dialogs)
    dialog_defs: Set[str] = {d for lst in dialogs_opened_by_def.values() for d in lst}

    for def_id, d in sorted(idx.definitions.items()):
        if not d.get("is_shipping", True):
            continue
        kind = d.get("kind")
        if kind not in ("Window", "Form"):
            continue
        base_spec = resolve_element_type_spec(d.get("base_clr_type") or "Window", idx)
        is_dialog = (
            def_id in dialog_defs
            or "Dialog" in (d.get("full_class_name") or "")
            or "PopupWindow" in (d.get("base_clr_type") or "")
            or "Dialog" in base_spec.base_full_name
        )
        surface_kind = "ModalDialog" if is_dialog else ("StandaloneForm" if kind == "Form" else "HostWindow")
        short_name = (d.get("full_class_name") or def_id).rsplit(".", 1)[-1]
        surf = _build_projected_surface(
            surface_id=def_id,
            surface_name=short_name,
            surface_kind=surface_kind,
            root_defs=[def_id],
            host_window_id=def_id,
            mode_name="",
            idx=idx,
            mounted_tracker=mounted_into_surface,
        )
        surf.triggered_dialogs = sorted(set(dialogs_opened_by_def.get(def_id, [])))
        idx.projected_surfaces[surf.surface_id] = surf
        if surf.root_node is not None:
            idx.host_window_pools[def_id] = surf.root_node

    # B. Multi-Mode Composite Application Hosts (e.g., CTViewer -> 2D, MPR, Volume, Endo)
    sub_asms_claimed_by_composite: Set[str] = set()
    for host_asm, raw_targets in sorted(composite_hosts.items()):
        # Collect host common panels (e.g., CTViewControlPanel, FourDViewWithMovieBarView)
        host_panel_defs: List[str] = []
        for ed in addin_mounts_by_asm.get(host_asm, []):
            tdef = _resolve_target_def_id(ed["dst_type_or_key"], host_asm, idx)
            if tdef and tdef not in host_panel_defs:
                host_panel_defs.append(tdef)

        # Resolve sub-mode assemblies (e.g., PNMS.MIA.TwoD, PNMS.MIA.MPR, PNMS.MIA.Volume, PNMS.MIA.Endo)
        mode_sub_asms: List[str] = []
        for tok in raw_targets:
            if tok in addin_mounts_by_asm and tok not in mode_sub_asms:
                mode_sub_asms.append(tok)
            else:
                # Check if tok is a class inside one of addin_mounts_by_asm
                for cand_asm in sorted(addin_mounts_by_asm):
                    if tok.startswith(cand_asm + ".") and cand_asm not in mode_sub_asms:
                        mode_sub_asms.append(cand_asm)
                # Or fallback: scan shipping UserControls in assembly `tok` (e.g. MPR / Endo without explicit AppBuilder)
                if tok not in mode_sub_asms and any(df["assembly"] == tok for df in idx.definitions.values()):
                    mode_sub_asms.append(tok)

        if not mode_sub_asms:
            continue

        host_win_id = f"HostWindow::{host_asm}"
        fallback_repo_id = next(
            (df["repo_id"] for df in idx.definitions.values() if df["assembly"] in mode_sub_asms or df["assembly"] == host_asm),
            host_asm,
        )
        composite_win_root = ProjectedNode(
            node_id=f"{host_win_id}::root",
            source_elem_id=host_win_id,
            source_def_id=host_win_id,
            repo_id=idx.definitions[host_panel_defs[0]]["repo_id"] if host_panel_defs else fallback_repo_id,
            assembly=host_asm,
            file="",
            line=0,
            clr_type="System.Windows.Window",
            short_clr_type="Window",
            uia_type="Window",
            auto_id=host_asm.rsplit(".", 1)[-1],
            auto_id_source="SyntheticHost",
            name_en=host_asm.rsplit(".", 1)[-1],
            is_static_name=True,
        )

        # Build each mode surface AND attach all modes (with non-active modes resident Collapsed per Iron Law 6!)
        mode_defs_map: Dict[str, List[str]] = {}
        for m_asm in mode_sub_asms:
            sub_asms_claimed_by_composite.add(m_asm)
            m_defs: List[str] = []
            for ed in addin_mounts_by_asm.get(m_asm, []):
                tdef = _resolve_target_def_id(ed["dst_type_or_key"], m_asm, idx)
                if tdef and tdef not in m_defs:
                    m_defs.append(tdef)
            if not m_defs:
                # Include top-level shipping UserControls of this mode assembly (e.g. MPRControlPanel / EndoControlPanel)
                m_defs = [
                    did
                    for did, df in sorted(idx.definitions.items())
                    if df["assembly"] == m_asm
                    and df.get("is_shipping", True)
                    and df.get("kind") == "UserControl"
                    and ("ControlPanel" in did or "MainView" in did or "ToolBar" in did or "BatchPanel" in did)
                ]
            if m_defs:
                mode_short = m_asm.rsplit(".", 1)[-1]
                mode_defs_map[mode_short] = m_defs

        idx.host_window_pools[host_win_id] = composite_win_root

        # Emit one ProjectedSurface per mode (reusing `host_win_id` as its host window pool!)
        for m_short, m_defs in sorted(mode_defs_map.items()):
            surf_id = f"{host_asm}::Mode::{m_short}"
            surf = _build_projected_surface(
                surface_id=surf_id,
                surface_name=f"{host_asm.rsplit('.', 1)[-1]}_{m_short}",
                surface_kind="ModeSurface",
                root_defs=host_panel_defs + m_defs,
                host_window_id=host_win_id,
                mode_name=m_short,
                idx=idx,
                mounted_tracker=mounted_into_surface,
            )
            idx.projected_surfaces[surf_id] = surf

    # C. Standalone AddIn Surfaces (ALA, CFA, CTC, DE, Perfusion, CAA, CCS, Dental, etc.)
    for addin_asm, edges in sorted(addin_mounts_by_asm.items()):
        if addin_asm in composite_hosts or addin_asm in sub_asms_claimed_by_composite:
            continue
        a_defs: List[str] = []
        for ed in edges:
            tdef = _resolve_target_def_id(ed["dst_type_or_key"], addin_asm, idx)
            if tdef and tdef not in a_defs:
                a_defs.append(tdef)
        if not a_defs:
            continue
        surf_id = f"AddIn::{addin_asm}"
        short_app = addin_asm.rsplit(".", 1)[-1]
        surf = _build_projected_surface(
            surface_id=surf_id,
            surface_name=short_app,
            surface_kind="AddInSurface",
            root_defs=a_defs,
            host_window_id=surf_id,
            mode_name=short_app,
            idx=idx,
            mounted_tracker=mounted_into_surface,
        )
        idx.projected_surfaces[surf_id] = surf
        if surf.root_node is not None:
            idx.host_window_pools[surf_id] = surf.root_node

    # D. Shared Components (>= 2 callers) + Remaining Unmounted Shipping Root Panels (e.g. Scan / Service UserControls)
    for def_id in sorted(idx.shared_component_defs):
        d = idx.definitions.get(def_id)
        if not d or not d.get("is_shipping", True):
            continue
        short_name = (d.get("full_class_name") or def_id).rsplit(".", 1)[-1]
        surf = _build_projected_surface(
            surface_id=f"Shared::{def_id}",
            surface_name=short_name,
            surface_kind="SharedComponent",
            root_defs=[def_id],
            host_window_id=f"Shared::{def_id}",
            mode_name="",
            idx=idx,
            mounted_tracker=set(),
        )
        idx.projected_surfaces[surf.surface_id] = surf
        if surf.root_node is not None:
            idx.host_window_pools[surf.host_window_id] = surf.root_node

    # E. Shipping root-level Application Views in repos without shell Window in the same repo (e.g. PPTOthers / MIC)
    for def_id, d in sorted(idx.definitions.items()):
        if (
            not d.get("is_shipping", True)
            or d.get("kind") not in ("UserControl", "Page")
            or def_id in mounted_into_surface
            or f"Shared::{def_id}" in idx.projected_surfaces
        ):
            continue
        elems = idx.elements_by_def.get(def_id, [])
        if len(elems) < 2:
            continue
        short_name = (d.get("full_class_name") or def_id).rsplit(".", 1)[-1]
        surf_id = f"ComponentSurface::{def_id}"
        surf = _build_projected_surface(
            surface_id=surf_id,
            surface_name=short_name,
            surface_kind="AddInSurface",
            root_defs=[def_id],
            host_window_id=surf_id,
            mode_name="",
            idx=idx,
            mounted_tracker=mounted_into_surface,
        )
        if surf.interactive_nodes:
            idx.projected_surfaces[surf_id] = surf
            if surf.root_node is not None:
                idx.host_window_pools[surf_id] = surf.root_node


def _build_projected_surface(
    *,
    surface_id: str,
    surface_name: str,
    surface_kind: str,
    root_defs: List[str],
    host_window_id: str,
    mode_name: str,
    idx: WorkspaceIndex,
    mounted_tracker: Set[str],
) -> ProjectedSurface:
    primary_def = idx.definitions[root_defs[0]]
    repo_id = primary_def["repo_id"]
    assembly = primary_def["assembly"]
    file_rel = primary_def["file"]

    node_seq = [0]
    mounted_components: List[str] = []

    is_real_window_root = len(root_defs) == 1 and primary_def.get("kind") in ("Window", "Form")
    if is_real_window_root:
        mounted_tracker.add(root_defs[0])
        top_nodes = _project_definition_tree(
            def_id=root_defs[0],
            idx=idx,
            visited_defs={root_defs[0]},
            node_seq=node_seq,
            mode_name=mode_name,
            panel_group=surface_name,
            preconditions=[],
            topology="OwnedDialog" if surface_kind == "ModalDialog" else "InWindow",
            initially_visible=True,
            mounted_tracker=mounted_components_collector(mounted_tracker, mounted_components),
            record_wrapper_gaps=True,
            surface_name_for_gaps=surface_name,
            enclosing_window_def_id=root_defs[0],
        )
        root_node = top_nodes[0] if top_nodes else ProjectedNode(
            node_id=f"{surface_id}::root",
            source_elem_id=surface_id,
            source_def_id=root_defs[0],
            repo_id=repo_id,
            assembly=assembly,
            file=file_rel,
            line=1,
            clr_type="System.Windows.Window",
            short_clr_type="Window",
            uia_type="Window",
            auto_id=surface_name,
        )
    else:
        root_node = ProjectedNode(
            node_id=f"{surface_id}::root",
            source_elem_id=surface_id,
            source_def_id=root_defs[0],
            repo_id=repo_id,
            assembly=assembly,
            file=file_rel,
            line=1,
            clr_type="System.Windows.Window",
            short_clr_type="Window",
            uia_type="Window",
            auto_id="",
            auto_id_source="SyntheticHost",
            name_en="",
            is_static_name=False,
        )
        collector = mounted_components_collector(mounted_tracker, mounted_components)
        for rdef in root_defs:
            mounted_tracker.add(rdef)
            mounted_components.append(rdef)
            sub_nodes = _project_definition_tree(
                def_id=rdef,
                idx=idx,
                visited_defs={rdef},
                node_seq=node_seq,
                mode_name=mode_name,
                panel_group=rdef.rsplit(".", 1)[-1],
                preconditions=[],
                topology="InWindow",
                initially_visible=True,
                mounted_tracker=collector,
                record_wrapper_gaps=True,
                surface_name_for_gaps=surface_name,
            )
            for sn in sub_nodes:
                sn.parent = root_node
                sn.sibling_index = len(root_node.children)
                root_node.children.append(sn)

    all_nodes: List[ProjectedNode] = []
    interactive_nodes: List[ProjectedNode] = []

    def collect_flat(n: ProjectedNode) -> None:
        all_nodes.append(n)
        if n is not root_node and n.is_interactive_target:
            interactive_nodes.append(n)
        for i_ch, ch in enumerate(n.children):
            ch.parent = n
            ch.sibling_index = i_ch
            collect_flat(ch)

    collect_flat(root_node)

    # Annotate `adjacent_label` on interactive nodes from immediate preceding/following concise static Text sibling
    for n in interactive_nodes:
        if n.parent is not None and not n.adjacent_label:
            sibs = n.parent.children
            pos = n.sibling_index
            if (
                pos > 0
                and sibs[pos - 1].uia_type == "Text"
                and sibs[pos - 1].is_static_name
                and sibs[pos - 1].name_en
                and "\n" not in sibs[pos - 1].name_en
                and len(sibs[pos - 1].name_en.strip()) <= 64
            ):
                n.adjacent_label = sibs[pos - 1].name_en
            elif (
                pos + 1 < len(sibs)
                and sibs[pos + 1].uia_type == "Text"
                and sibs[pos + 1].is_static_name
                and sibs[pos + 1].name_en
                and "\n" not in sibs[pos + 1].name_en
                and len(sibs[pos + 1].name_en.strip()) <= 64
            ):
                n.adjacent_label = sibs[pos + 1].name_en

    if is_real_window_root:
        host_title = root_node.name_en or surface_name
        host_aid = root_node.auto_id
        host_known = True
    elif surface_kind == "ModeSurface":
        host_title = host_window_id.split("::", 1)[-1].rsplit(".", 1)[-1]
        host_aid = ""
        host_known = False
    else:
        host_title = ""
        host_aid = ""
        host_known = False

    return ProjectedSurface(
        surface_id=surface_id,
        surface_name=surface_name,
        surface_kind=surface_kind,
        repo_id=repo_id,
        assembly=assembly,
        root_def_id=root_defs[0],
        host_window_id=host_window_id,
        host_window_title=host_title,
        host_window_auto_id=host_aid,
        file=file_rel,
        host_window_known=host_known,
        mode_name=mode_name,
        root_node=root_node,
        all_nodes=all_nodes,
        interactive_nodes=interactive_nodes,
        mounted_component_defs=sorted(set(mounted_components)),
    )


class _MountedCollector:
    def __init__(self, global_set: Set[str], local_list: List[str]) -> None:
        self.global_set = global_set
        self.local_list = local_list

    def add(self, def_id: str) -> None:
        self.global_set.add(def_id)
        self.local_list.append(def_id)


def mounted_components_collector(global_set: Set[str], local_list: List[str]) -> Any:
    return _MountedCollector(global_set, local_list)


def _find_template_def_for_element(
    elem: Dict[str, Any],
    type_spec: ResolvedTypeSpec,
    idx: WorkspaceIndex,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return `(template_def_id, style_def_or_none)` if this element applies a known Style/ControlTemplate."""
    style_def: Optional[Dict[str, Any]] = None
    for ed in idx.edges_by_elem.get(elem["elem_id"], []):
        ek = ed["edge_kind"]
        prop = (ed.get("extra") or {}).get("property", "")
        if ek == "APPLIES_TEMPLATE" or (ek == "DATA_TEMPLATE" and prop in ("HeaderTemplate", "ContentTemplate")):
            key = ed["dst_type_or_key"]
            if key in idx.definitions and idx.definitions[key].get("kind") in ("ControlTemplate", "DataTemplate"):
                return key, None
            st = idx.styles_by_key.get(key)
            if st is not None:
                style_def = st
                for sed in idx.edges_by_src.get(st["def_id"], []):
                    if sed["edge_kind"] == "APPLIES_TEMPLATE" and sed["dst_type_or_key"] in idx.definitions:
                        return sed["dst_type_or_key"], st
            tpl_cands = idx.templates_by_res_key.get(key, [])
            if tpl_cands:
                return tpl_cands[0], style_def

    # Default theme template lookup by TargetType (crucial for NumericUpDown / DoubleSpinner BAML templates!)
    for probe in (type_spec.full_name, type_spec.short_name):
        cands = idx.templates_by_target_type.get(probe, [])
        if cands:
            return cands[0], style_def
    return None, style_def


def _uia_allowed_text_slots(uia_type: str) -> Tuple[str, ...]:
    """Return authoritative UIA AutomationPeer `@name` source property precedence for `uia_type`."""
    if uia_type in _INPUT_VALUE_ONLY_UIA_TYPES:
        return ("auto_name", "header")
    if uia_type in _WINDOW_UIA_TYPES:
        return ("auto_name", "title", "text")
    if uia_type in _HEADERED_UIA_TYPES:
        return ("auto_name", "header", "content", "text")
    if uia_type in _TEXT_DISPLAY_UIA_TYPES:
        return ("auto_name", "text", "content", "header")
    return ("auto_name", "content", "header", "text")


def _select_uia_text_expr(attrs: Dict[str, Any], uia_type: str) -> str:
    """Project raw lossless `text_props` into the single UIA `@name` expression per AutomationPeer spec."""
    tprops = attrs.get("text_props")
    if isinstance(tprops, dict):
        for slot in _uia_allowed_text_slots(uia_type):
            val = (tprops.get(slot) or "").strip()
            if val:
                return val
        return ""
    # Backward compatibility for legacy v1.0 snapshots without `text_props`
    if uia_type in _INPUT_VALUE_ONLY_UIA_TYPES:
        return ""
    return (attrs.get("raw_text_expr") or "").strip()


def _project_definition_tree(
    *,
    def_id: str,
    idx: WorkspaceIndex,
    visited_defs: Set[str],
    node_seq: List[int],
    mode_name: str,
    panel_group: str,
    preconditions: List[str],
    topology: str,
    initially_visible: bool,
    mounted_tracker: Any,
    record_wrapper_gaps: bool,
    surface_name_for_gaps: str,
    wrapper_inherit: Optional[Dict[str, str]] = None,
    host_view_def_id: str = "",
    enclosing_window_def_id: str = "",
) -> List[ProjectedNode]:
    d = idx.definitions.get(def_id)
    if not d:
        return []
    elems = idx.elements_by_def.get(def_id, [])
    if not elems:
        return []

    repo_id = d["repo_id"]
    assembly = d["assembly"]
    file_rel = d["file"]
    owner_full_cls = d.get("full_class_name") or ""
    owner_cls_short = (owner_full_cls or def_id).rsplit(".", 1)[-1]
    is_winforms = d.get("framework") == "WinForms"
    def_kind = d.get("kind")
    is_real_view_def = def_kind in ("Window", "UserControl", "Page", "Form")
    effective_host_view_def_id = def_id if is_real_view_def else (host_view_def_id or def_id)
    effective_window_def_id = def_id if def_kind in ("Window", "Form") else enclosing_window_def_id

    children_by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for el in elems:
        children_by_parent.setdefault(el.get("parent_elem_id"), []).append(el)

    # Consume the single authoritative composition graph (`idx.resolved_mounts`) built in Pass 4
    def_resolved_mounts = idx.resolved_mounts.get(def_id, {})

    def project_element(
        el: Dict[str, Any],
        cur_group: str,
        cur_preconds: List[str],
        cur_topology: str,
        cur_visible: bool,
        sibling_tab_ordinal: int,
        inherited_wrapper: Optional[Dict[str, str]],
    ) -> List[ProjectedNode]:
        attrs = el.get("attrs") or {}
        ctx = el.get("context", "VisualTree")
        in_template = ctx in ("ControlTemplate", "DataTemplate", "HeaderTemplate")

        next_topology = cur_topology
        if ctx == "PopupSubtree":
            next_topology = "PopupTopLevel"
        elif ctx == "ContextMenuSubtree":
            next_topology = "ContextMenuTopLevel"

        vis_raw = attrs.get("initial_vis", "")
        next_visible = cur_visible and vis_raw not in ("Collapsed", "Hidden")

        tspec = resolve_element_type_spec(el["clr_type"], idx)
        uia_type = tspec.uia_control_type
        has_peer = tspec.has_uia_peer
        if _is_embedded_web_host_type(tspec.full_name, tspec.short_name):
            next_topology = "EmbeddedWebHost"
            uia_type = "Pane"
            has_peer = True

        tpl_def_id, style_def = _find_template_def_for_element(el, tspec, idx)

        # Iron Law 3: ListView (and any ListView subclass like `EditListView`) -> DataGrid iff has_grid_view
        if tspec.full_name == "System.Windows.Controls.ListView" or (
            uia_type == "List" and (attrs.get("has_grid_view") or (style_def and style_def.get("has_grid_view")))
        ):
            if attrs.get("has_grid_view") or (style_def and style_def.get("has_grid_view")):
                uia_type = "DataGrid"
            else:
                uia_type = "List"

        # Iron Law 1: Dual @auto_id precedence
        explicit_aid = attrs.get("explicit_auto_id", "")
        raw_uid = "" if in_template else attrs.get("x_uid", "")
        raw_name = attrs.get("x_name", "")
        if explicit_aid:
            eff_auto_id, aid_src = explicit_aid, "AutomationId"
        elif raw_uid:
            eff_auto_id, aid_src = raw_uid, "x:Uid"
        elif raw_name:
            eff_auto_id, aid_src = raw_name, "x:Name"
        else:
            eff_auto_id, aid_src = "", ""

        i18n_uia_hint = "Button" if tspec.short_name == "Label" else (uia_type or "")
        raw_text_expr = _select_uia_text_expr(attrs, uia_type or "")
        name_en, name_zh, is_stat_name, text_src = resolve_i18n_expr_with_source(
            raw_text_expr,
            assembly,
            owner_cls_short,
            i18n_uia_hint,
            idx,
            owner_def_id=def_id,
            host_view_def_id=effective_host_view_def_id,
            enclosing_window_def_id=effective_window_def_id,
            in_template=in_template,
        )

        # Code-Behind static text fallback (strictly observing UIA property spec & dynamic_multi)
        if not is_stat_name and not in_template and owner_full_cls and owner_full_cls in idx.vm_props_by_class:
            cb_slots: List[str] = []
            if el.get("parent_elem_id") is None and uia_type == "Window":
                cb_slots.append("$root.title")
            if raw_name:
                for slot_p in _uia_allowed_text_slots(uia_type or ""):
                    if slot_p in ("content", "header", "text", "title"):
                        cb_slots.append(f"$ctrl:{raw_name}.{slot_p}")
            for cb_key in cb_slots:
                cb_en, cb_zh, cb_ok = _resolve_member_value_flow(
                    [owner_full_cls], cb_key, assembly, owner_cls_short, i18n_uia_hint, idx
                )
                if cb_ok and (cb_en or cb_zh):
                    name_en, name_zh, is_stat_name = cb_en, cb_zh, True
                    text_src = "code_behind_static"
                    break

        # Closed-universe attached-property localization probe (for label-bearing UIA types only; never input controls)
        if not is_stat_name and (uia_type in _LABEL_BEARING_ATTACHED_PROBE_UIA_TYPES):
            for probe_key in attrs.get("attached_probes") or ():
                loc_pair = _lookup_closed_loc_key(probe_key, assembly, idx)
                if loc_pair and (loc_pair[0] or loc_pair[1]):
                    name_en, name_zh, is_stat_name = _finalize_uia_text(
                        loc_pair[0], loc_pair[1], True, i18n_uia_hint
                    )
                    if is_stat_name:
                        text_src = "attached_loc_probe"
                        break

        help_en, help_zh, is_stat_help = resolve_i18n_expr(
            attrs.get("raw_tooltip_expr", ""),
            assembly,
            owner_cls_short,
            "",
            idx,
            owner_def_id=def_id,
            host_view_def_id=effective_host_view_def_id,
            enclosing_window_def_id=effective_window_def_id,
            in_template=in_template,
        )
        if not is_stat_help and not in_template and raw_name and owner_full_cls and owner_full_cls in idx.vm_props_by_class:
            cb_hen, cb_hzh, cb_hok = _resolve_member_value_flow(
                [owner_full_cls], f"$ctrl:{raw_name}.tooltip", assembly, owner_cls_short, "", idx
            )
            if cb_hok and (cb_hen or cb_hzh):
                help_en, help_zh, is_stat_help = cb_hen, cb_hzh, True

        # Check embedded UserControl (`XAML_EMBED`) and authoritative Pass-4 mounts (`idx.resolved_mounts`)
        embedded_def_id: Optional[str] = None
        if len(visited_defs) < 12 and el.get("parent_elem_id") is not None:
            cand_def = _resolve_target_def_id(tspec.full_name, assembly, idx)
            if cand_def and cand_def not in visited_defs and idx.definitions[cand_def].get("kind") in ("UserControl", "Page"):
                embedded_def_id = cand_def

        c_mounts: List[str] = []
        if len(visited_defs) < 12:
            mount_lookup_keys: List[str] = [f"elem:{el['elem_id']}"]
            if raw_name:
                mount_lookup_keys.append(f"slot:{raw_name}")
            if raw_uid:
                mount_lookup_keys.append(f"slot:{raw_uid}")
            reg_name = attrs.get("region_name", "")
            if reg_name:
                mount_lookup_keys.append(f"region:{reg_name}")
            for mk in mount_lookup_keys:
                for cm in def_resolved_mounts.get(mk, []):
                    if cm not in visited_defs and cm not in c_mounts:
                        c_mounts.append(cm)

        # Update group & lazy container preconditions (Iron Law 8)
        next_group = cur_group
        next_preconds = list(cur_preconds)
        if uia_type == "Group" and name_en:
            next_group = name_en
            if tspec.short_name in ("Expander", "CollapsedPanelItem"):
                next_preconds.append(f"expand('{name_en}')")
        elif uia_type == "TabItem":
            tab_label = name_en or eff_auto_id or f"tab[{sibling_tab_ordinal}]"
            next_group = tab_label
            if sibling_tab_ordinal > 0:
                next_preconds.append(f"select_tab('{tab_label}')")
        elif embedded_def_id:
            next_group = tspec.short_name

        # Collect projected children recursively (in exact xml_order)
        projected_kids: List[ProjectedNode] = []
        raw_kids = children_by_parent.get(el["elem_id"], [])
        tab_counter = 0
        for rk in raw_kids:
            rk_spec = resolve_element_type_spec(rk["clr_type"], idx)
            ord_tab = tab_counter if rk_spec.uia_control_type == "TabItem" else 0
            if rk_spec.uia_control_type == "TabItem":
                tab_counter += 1
            projected_kids.extend(
                project_element(rk, next_group, next_preconds, next_topology, next_visible, ord_tab, None)
            )

        # Expand embedded UserControl subtree (`XAML_EMBED`)
        # In WPF, the host tag (`<local:BatchPanel x:Name="batchControl"/>`) and the embedded file's
        # root `<UserControl x:Uid="UserControl_1">` are ONE single CLR object / ONE UIA Custom node.
        # Merge the embedded single Custom root into the caller node instead of emitting ghost double-Custom pairs.
        if embedded_def_id:
            mounted_tracker.add(embedded_def_id)
            emb_nodes = _project_definition_tree(
                def_id=embedded_def_id,
                idx=idx,
                visited_defs=visited_defs | {embedded_def_id},
                node_seq=node_seq,
                mode_name=mode_name,
                panel_group=next_group,
                preconditions=next_preconds,
                topology=next_topology,
                initially_visible=next_visible,
                mounted_tracker=mounted_tracker,
                record_wrapper_gaps=record_wrapper_gaps,
                surface_name_for_gaps=surface_name_for_gaps,
                enclosing_window_def_id=effective_window_def_id,
            )
            if len(emb_nodes) == 1 and emb_nodes[0].uia_type == "Custom" and uia_type == "Custom":
                emb_root = emb_nodes[0]
                if not eff_auto_id and emb_root.auto_id:
                    eff_auto_id = emb_root.auto_id
                    aid_src = emb_root.auto_id_source
                if not name_en and emb_root.name_en:
                    name_en = emb_root.name_en
                    name_zh = emb_root.name_zh
                    is_stat_name = emb_root.is_static_name
                if not help_en and emb_root.help_text_en:
                    help_en = emb_root.help_text_en
                    help_zh = emb_root.help_text_zh
                    is_stat_help = emb_root.is_static_help_text
                next_visible = next_visible and emb_root.is_initially_visible
                projected_kids.extend(emb_root.children)
            else:
                projected_kids.extend(emb_nodes)

        # Expand C# assigned views (`slot.Content = new SubView()`)
        for cm_def in c_mounts:
            if cm_def not in visited_defs:
                mounted_tracker.add(cm_def)
                projected_kids.extend(
                    _project_definition_tree(
                        def_id=cm_def,
                        idx=idx,
                        visited_defs=visited_defs | {cm_def},
                        node_seq=node_seq,
                        mode_name=mode_name,
                        panel_group=cm_def.rsplit(".", 1)[-1],
                        preconditions=next_preconds,
                        topology=next_topology,
                        initially_visible=next_visible,
                        mounted_tracker=mounted_tracker,
                        record_wrapper_gaps=record_wrapper_gaps,
                        surface_name_for_gaps=surface_name_for_gaps,
                        enclosing_window_def_id=effective_window_def_id,
                    )
                )

        # Expand ControlTemplate ONLY for Peer-less wrappers (NumericUpDown / DoubleSpinner / ContentControl)
        # or composite container/header peers (`Custom`, `HeaderItem`)—never expand internal chrome of leaf Button/ComboBox/etc.
        needs_template_expansion = (not has_peer) or (uia_type in ("Custom", "HeaderItem"))
        if tpl_def_id and tpl_def_id not in visited_defs and needs_template_expansion and not embedded_def_id:
            wrap_meta = {
                "wrapper_name": eff_auto_id,
                "wrapper_type": tspec.short_name,
                "binding_path": attrs.get("binding_path", ""),
                "help_en": help_en,
                "help_zh": help_zh,
                "is_stat_help": "1" if is_stat_help else "",
                "file": file_rel,
                "line": str(el.get("line", 0)),
            }
            tpl_kids = _project_definition_tree(
                def_id=tpl_def_id,
                idx=idx,
                visited_defs=visited_defs | {tpl_def_id},
                node_seq=node_seq,
                mode_name=mode_name,
                panel_group=next_group,
                preconditions=next_preconds,
                topology=next_topology,
                initially_visible=next_visible,
                mounted_tracker=mounted_tracker,
                record_wrapper_gaps=False,
                surface_name_for_gaps=surface_name_for_gaps,
                wrapper_inherit=wrap_meta if not has_peer else None,
                host_view_def_id=effective_host_view_def_id,
                enclosing_window_def_id=effective_window_def_id,
            )
            # Propagate wrapper metadata onto ALL expanded template children (Edit + companion RepeatButtons, etc.)
            if not has_peer and tpl_kids:
                w_inst_key = f"{def_id}::{el['elem_id']}"
                primary_inputs = [tk for tk in tpl_kids if tk.uia_type in ("Edit", "ComboBox", "Spinner")]
                target_inp = primary_inputs[0] if primary_inputs else tpl_kids[0]

                def _stamp_wrapper_node(node_item: ProjectedNode) -> None:
                    if not node_item.wrapper_instance_key:
                        node_item.wrapper_instance_key = w_inst_key
                    if wrap_meta["wrapper_name"] and not node_item.dissolved_wrapper_name:
                        node_item.dissolved_wrapper_name = wrap_meta["wrapper_name"]
                        node_item.dissolved_wrapper_type = wrap_meta["wrapper_type"]
                    elif wrap_meta["wrapper_type"] and not node_item.dissolved_wrapper_type:
                        node_item.dissolved_wrapper_type = wrap_meta["wrapper_type"]
                    if file_rel:
                        node_item.file = file_rel
                        node_item.line = el.get("line", 0)
                    if wrap_meta["binding_path"] and (
                        not node_item.binding_path or node_item.binding_path in ("Value", "Text")
                    ):
                        node_item.binding_path = wrap_meta["binding_path"]
                    for ch_n in node_item.children:
                        _stamp_wrapper_node(ch_n)

                for tk in tpl_kids:
                    _stamp_wrapper_node(tk)

                if wrap_meta["wrapper_name"] and record_wrapper_gaps:
                    idx.identity_gap_issues.append(
                        IdentityGapIssue(
                            severity="Medium",
                            gap_kind="WRAPPER_NAME_DISSOLVED",
                            repo_id=repo_id,
                            surface_name=surface_name_for_gaps,
                            file=file_rel,
                            line=el.get("line", 0),
                            uia_type=target_inp.uia_type,
                            clr_type=tspec.short_name,
                            effective_auto_id=target_inp.auto_id,
                            dissolved_wrapper_name=wrap_meta["wrapper_name"],
                            adjacent_label="",
                            current_xpath="",
                            recommendation=(
                                f"Wrapper '{tspec.short_name}' (x:Name/x:Uid='{wrap_meta['wrapper_name']}') has no AutomationPeer "
                                f"and dissolves into runtime UIA {target_inp.uia_type} (auto_id={target_inp.auto_id!r})."
                            ),
                        )
                    )
                if wrap_meta["help_en"] and not target_inp.help_text_en:
                    target_inp.help_text_en = wrap_meta["help_en"]
                    target_inp.help_text_zh = wrap_meta["help_zh"]
                    target_inp.is_static_help_text = bool(wrap_meta["is_stat_help"])
            projected_kids.extend(tpl_kids)

        # Iron Law 2: Dissolve WPF Peer-less shells (`Grid`, `StackPanel`, `Border`, `NumericUpDown`, etc.)!
        if not is_winforms and (not has_peer or not uia_type):
            return projected_kids

        # Otherwise emit real UIA Peer node!
        is_edit_raw = attrs.get("is_editable") or (style_def.get("setter_props", {}).get("IsEditable") if style_def else "")
        is_ro_raw = attrs.get("is_readonly") or (style_def.get("setter_props", {}).get("IsReadOnly") if style_def else "")
        is_editable_val: Optional[bool] = None
        if uia_type == "ComboBox":
            is_editable_val = str(is_edit_raw).lower() == "true"
        is_readonly_val: Optional[bool] = (str(is_ro_raw).lower() == "true") if is_ro_raw else None

        # Iron Law 4: WPF string Content/Header on Button/CheckBox/RadioButton/TabItem also renders an inner Text peer via ContentPresenter
        if (
            not is_winforms
            and uia_type in ("Button", "CheckBox", "RadioButton", "TabItem")
            and is_stat_name
            and name_en
            and not any(ch.uia_type == "Text" for ch in projected_kids)
        ):
            node_seq[0] += 1
            projected_kids.append(
                ProjectedNode(
                    node_id=f"{def_id}::p{node_seq[0]}",
                    source_elem_id=f"{el['elem_id']}::$content_text",
                    source_def_id=def_id,
                    repo_id=repo_id,
                    assembly=assembly,
                    file=file_rel,
                    line=el.get("line", 0),
                    clr_type="System.Windows.Controls.TextBlock",
                    short_clr_type="TextBlock",
                    uia_type="Text",
                    name_en=name_en,
                    name_zh=name_zh,
                    is_static_name=True,
                    is_initially_visible=next_visible,
                    mode_name=mode_name,
                    panel_group=next_group,
                    preconditions=next_preconds,
                    topology=next_topology,
                )
            )

        node_seq[0] += 1
        pnode = ProjectedNode(
            node_id=f"{def_id}::p{node_seq[0]}",
            source_elem_id=el["elem_id"],
            source_def_id=def_id,
            repo_id=repo_id,
            assembly=assembly,
            file=file_rel,
            line=el.get("line", 0),
            clr_type=tspec.full_name,
            short_clr_type=tspec.short_name,
            uia_type=uia_type or "Custom",
            auto_id=eff_auto_id,
            auto_id_source=aid_src,
            raw_x_uid=raw_uid,
            raw_x_name=raw_name,
            name_en=name_en,
            name_zh=name_zh,
            is_static_name=is_stat_name,
            help_text_en=help_en,
            help_text_zh=help_zh,
            is_static_help_text=is_stat_help,
            is_editable=is_editable_val,
            is_readonly=is_readonly_val,
            is_initially_visible=next_visible,
            mode_name=mode_name,
            panel_group=next_group,
            preconditions=next_preconds,
            topology=next_topology,
            type_source=tspec.type_source,
            text_source=text_src,
            binding_path=attrs.get("binding_path", ""),
            command_expr=attrs.get("command_expr", ""),
            children=projected_kids,
        )
        for i_c, ch in enumerate(projected_kids):
            ch.parent = pnode
            ch.sibling_index = i_c
        return [pnode]

    out_roots: List[ProjectedNode] = []
    root_elems = children_by_parent.get(None, [])
    for relem in root_elems:
        out_roots.extend(
            project_element(relem, panel_group, preconditions, topology, initially_visible, 0, wrapper_inherit)
        )
    return out_roots
