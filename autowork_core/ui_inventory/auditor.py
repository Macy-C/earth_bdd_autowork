"""Read-only Locator Health Auditor (`audit-locators`) & Commit Diff Impact Analyzer (`diff-impact`).

Strictly read-only against `Bdd/locators/` and `Bdd/steps/` (Sec 7.4).
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from autowork_core.common.winauto_xpath import parse_xpath
from autowork_core.ui_inventory.linker import load_and_build_store
from autowork_core.ui_inventory.models import (
    DiffImpactReport,
    LocatorAuditItem,
    ProjectedNode,
    ProjectedSurface,
    WorkspaceIndex,
)
from autowork_core.ui_inventory.solver import eval_xpath_on_tree
from autowork_core.ui_inventory.store import SnapshotStore


INDEX_BRACKET_RE = re.compile(r"\[\s*-?\d+\s*\]")


def _load_locator_yaml_entries(yaml_path: Path) -> List[Tuple[str, Dict[str, Any]]]:
    """Read locator dictionary entries from a YAML file without modifying anything."""
    text = yaml_path.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore
        raw = yaml.safe_load(text)
        if isinstance(raw, dict):
            return [
                (str(k), v)
                for k, v in raw.items()
                if isinstance(v, dict) and ("by" in v or "value" in v)
            ]
    except Exception:
        pass

    # Line-by-line fallback for unquoted `*` keys like `2D_1*1_Layout_tool: {root: ..., by: 'xpath', value: "..."}`
    entries: List[Tuple[str, Dict[str, Any]]] = []
    line_re = re.compile(r"^\s*([^#\r\n:]+?)\s*:\s*\{(.+)\}\s*$")
    kv_re = re.compile(r"([A-Za-z_]\w*)\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|([^,\s\}]+))")
    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        loc_key = m.group(1).strip()
        body = m.group(2)
        item: Dict[str, Any] = {}
        for km in kv_re.finditer(body):
            k = km.group(1)
            v = km.group(2) if km.group(2) is not None else (km.group(3) if km.group(3) is not None else km.group(4))
            item[k] = v
        if "by" in item or "value" in item:
            entries.append((loc_key, item))
    return entries


def audit_bdd_locators(
    idx: WorkspaceIndex,
    locators_dir: Path,
    *,
    default_locale: str = "en-US",
) -> List[LocatorAuditItem]:
    """Read-only health check of existing `Bdd/locators/**/*.yaml` against the static UI inventory."""
    results: List[LocatorAuditItem] = []
    if not locators_dir.is_dir():
        return results

    # Index x:Name -> x:Uid shadowing for instant explanation when a locator uses shadowed x:Name!
    shadow_by_xname: Dict[str, List[str]] = {}
    for sh in idx.uid_shadow_issues:
        if sh.issue_kind == "UID_SHADOWS_NAME":
            shadow_by_xname.setdefault(sh.x_name, []).append(
                f"{sh.file}:{sh.line} exposes @auto_id='{sh.x_uid}' (x:Uid shadows x:Name='{sh.x_name}')"
            )

    surfaces_ordered: List[ProjectedSurface] = [
        idx.projected_surfaces[sid]
        for sid in sorted(idx.projected_surfaces)
        if idx.projected_surfaces[sid].surface_kind != "SharedComponent"
    ]
    surf_signatures: Dict[str, Tuple[Set[str], Set[str], Set[str]]] = {}
    for s in surfaces_ordered:
        tags_set = {n.uia_type.lower() for n in s.all_nodes}
        aids_set = {n.auto_id for n in s.all_nodes if n.auto_id}
        names_set = {
            (n.name_zh if default_locale.lower().startswith("zh") and n.name_zh else n.name_en)
            for n in s.all_nodes
            if n.name_en or n.name_zh
        }
        surf_signatures[s.surface_id] = (tags_set, aids_set, names_set)

    for yfile in sorted(locators_dir.rglob("*.yaml")):
        rel_yaml = yfile.relative_to(locators_dir).as_posix()
        for loc_key, spec in _load_locator_yaml_entries(yfile):
            by = str(spec.get("by", "")).strip().lower()
            val = str(spec.get("value", "")).strip()
            root_name = str(spec.get("root") or spec.get("region") or "")

            if by != "xpath":
                continue

            # 1. Syntax check against `winauto_xpath.parse_xpath`
            try:
                steps = parse_xpath(val)
            except Exception as exc:
                results.append(
                    LocatorAuditItem(
                        yaml_file=rel_yaml,
                        locator_key=loc_key,
                        root_name=root_name,
                        by=by,
                        raw_value=val,
                        status="INVALID_XPATH",
                        detail=str(exc),
                    )
                )
                continue

            uses_idx = bool(INDEX_BRACKET_RE.search(val))
            preferred_surfaces = _select_and_rank_surfaces(rel_yaml, surfaces_ordered, root_name=root_name)

            req_tags = {st["tag"].lower() for st in steps if st["tag"] != "*"}
            req_aids = {
                str(exp)
                for st in steps
                for k, exp in st.get("equals", [])
                if k.strip().lower() in ("auto_id", "automation_id", "automationid", "id")
            }
            req_names = {
                str(exp)
                for st in steps
                for k, exp in st.get("equals", [])
                if k.strip().lower() in ("name", "text", "title", "caption", "label")
            }

            matched_surf: Optional[ProjectedSurface] = None
            matched_nodes: List[ProjectedNode] = []
            for surf in preferred_surfaces:
                if surf.root_node is None:
                    continue
                s_tags, s_aids, s_names = surf_signatures[surf.surface_id]
                if not req_tags.issubset(s_tags) or not req_aids.issubset(s_aids) or not req_names.issubset(s_names):
                    continue
                hits = eval_xpath_on_tree(surf.root_node, val, first_only=False, default_locale=default_locale)
                if hits:
                    matched_surf = surf
                    matched_nodes = hits
                    break

            if matched_surf is not None and matched_nodes:
                hit0 = matched_nodes[0]
                # Check if locator targeted child `Text` of a Button/CheckBox/TabItem instead of the interactive parent
                if (
                    hit0.uia_type == "Text"
                    and hit0.parent is not None
                    and hit0.parent.is_interactive_target
                    and hit0.parent.solved is not None
                ):
                    results.append(
                        LocatorAuditItem(
                            yaml_file=rel_yaml,
                            locator_key=loc_key,
                            root_name=root_name,
                            by=by,
                            raw_value=val,
                            status="CHILD_TEXT_TARGET",
                            matched_count=len(matched_nodes),
                            matched_surface=matched_surf.surface_name,
                            suggested_xpath=hit0.parent.solved.primary_xpath,
                            detail=(
                                f"Targets inner `Text` instead of parent `{hit0.parent.uia_type}`; "
                                f"`set_checked`/`select_tab`/`wait_enabled` require `{hit0.parent.uia_type}`."
                            ),
                        )
                    )
                elif uses_idx and hit0.solved and not hit0.solved.uses_index:
                    results.append(
                        LocatorAuditItem(
                            yaml_file=rel_yaml,
                            locator_key=loc_key,
                            root_name=root_name,
                            by=by,
                            raw_value=val,
                            status="UPGRADEABLE_INDEX",
                            matched_count=len(matched_nodes),
                            matched_surface=matched_surf.surface_name,
                            suggested_xpath=hit0.solved.primary_xpath,
                            detail=f"Positional index can be upgraded to {hit0.solved.tier} zero-index selector.",
                        )
                    )
                else:
                    results.append(
                        LocatorAuditItem(
                            yaml_file=rel_yaml,
                            locator_key=loc_key,
                            root_name=root_name,
                            by=by,
                            raw_value=val,
                            status="OK",
                            matched_count=len(matched_nodes),
                            matched_surface=matched_surf.surface_name,
                            suggested_xpath=hit0.solved.primary_xpath if hit0.solved else "",
                        )
                    )
                continue

            # Not matched in currently scanned static trees: check if it used a shadowed `x:Name`!
            queried_aids = [
                exp for st in steps for k, exp in st.get("equals", []) if k.lower() in ("auto_id", "id", "automation_id")
            ]
            shadow_notes = [note for aid in queried_aids for note in shadow_by_xname.get(aid, [])]
            if shadow_notes:
                results.append(
                    LocatorAuditItem(
                        yaml_file=rel_yaml,
                        locator_key=loc_key,
                        root_name=root_name,
                        by=by,
                        raw_value=val,
                        status="SHADOWED_BY_UID",
                        detail="; ".join(shadow_notes[:2]),
                    )
                )
            else:
                results.append(
                    LocatorAuditItem(
                        yaml_file=rel_yaml,
                        locator_key=loc_key,
                        root_name=root_name,
                        by=by,
                        raw_value=val,
                        status="UNVERIFIED_EXTERNAL",
                        detail="Target control is dynamic at runtime (e.g. DataItem/TreeItem) or belongs to a repository not yet in snapshots/.",
                    )
                )

    return results


def _select_and_rank_surfaces(
    rel_yaml: str,
    surfaces: List[ProjectedSurface],
    *,
    root_name: str = "",
) -> List[ProjectedSurface]:
    """Scope to exact-matching `root_name` surfaces when `root_name` names a real surface; else rank by token overlap."""
    rn = (root_name or "").strip().lower()
    if rn:
        exact_matches = [
            s
            for s in surfaces
            if rn in (
                s.surface_name.lower(),
                s.surface_id.lower(),
                (s.host_window_title or "").lower(),
                (s.host_window_auto_id or "").lower(),
            )
        ]
        if exact_matches:
            return exact_matches

    raw_tokens = {
        tok
        for tok in re.split(r"[^a-z0-9]+", f"{rel_yaml} {root_name}".lower())
        if len(tok) >= 2 and tok not in ("yaml", "yml", "window", "locators", "common", "view", "page", "screen")
    }
    if "2d" in raw_tokens:
        raw_tokens.add("twod")
    if not raw_tokens:
        return surfaces

    def _score(s: ProjectedSurface) -> Tuple[int, str]:
        hay = f"{s.surface_name} {s.host_window_title} {s.assembly} {s.repo_id}".lower()
        hits = sum(1 for tok in raw_tokens if tok in hay)
        return (-hits, s.surface_id)

    return sorted(surfaces, key=_score)


def analyze_diff_impact(
    old_store: SnapshotStore,
    new_store: SnapshotStore,
    *,
    bdd_dir: Optional[Path] = None,
    default_locale: str = "en-US",
    reference_dll_dirs: Optional[List[Path]] = None,
) -> DiffImpactReport:
    """Compare two UI Inventory stores and identify impacted Surfaces, YAML locators, and BDD Step files."""
    old_idx = load_and_build_store(old_store, default_locale=default_locale, reference_dll_dirs=reference_dll_dirs)
    new_idx = load_and_build_store(new_store, default_locale=default_locale, reference_dll_dirs=reference_dll_dirs)

    report = DiffImpactReport()
    all_repos = sorted(set(old_idx.repo_manifests) | set(new_idx.repo_manifests))
    for rid in all_repos:
        if old_idx.repo_manifests.get(rid, {}).get("commit") != new_idx.repo_manifests.get(rid, {}).get("commit"):
            report.changed_repos.append(rid)

    def index_controls(widx: WorkspaceIndex) -> Dict[Tuple[str, str, str, int], Dict[str, Any]]:
        out: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
        occ_counts: Dict[Tuple[str, str, str], int] = {}
        for sid, surf in sorted(widx.projected_surfaces.items()):
            if surf.surface_kind == "SharedComponent":
                continue
            for n in surf.interactive_nodes:
                identity = n.auto_id or n.dissolved_wrapper_name or n.adjacent_label or f"{n.file}:{n.line}"
                base_k = (surf.surface_id, n.uia_type, identity)
                occ = occ_counts.get(base_k, 0)
                occ_counts[base_k] = occ + 1
                key = (surf.surface_id, n.uia_type, identity, occ)
                out[key] = {
                    "surface_id": surf.surface_id,
                    "surface_name": surf.surface_name,
                    "uia_type": n.uia_type,
                    "auto_id": n.auto_id,
                    "name_en": n.name_en,
                    "name_zh": n.name_zh,
                    "help_text_en": n.help_text_en,
                    "xpath": n.solved.primary_xpath if n.solved else "",
                    "file": n.file,
                    "line": n.line,
                }
        return out

    old_ctrls = index_controls(old_idx)
    new_ctrls = index_controls(new_idx)
    impacted_surfs: Set[str] = set()
    changed_tokens: Set[str] = set()

    for k in sorted(set(old_ctrls) | set(new_ctrls)):
        oc = old_ctrls.get(k)
        nc = new_ctrls.get(k)
        if oc is None and nc is not None:
            report.added_controls.append(nc)
            impacted_surfs.add(nc["surface_name"])
        elif nc is None and oc is not None:
            report.removed_controls.append(oc)
            impacted_surfs.add(oc["surface_name"])
            for tok in (oc["auto_id"], oc["name_en"], oc["name_zh"]):
                if tok:
                    changed_tokens.add(tok)
        elif oc is not None and nc is not None:
            if (
                oc["auto_id"] != nc["auto_id"]
                or oc["name_en"] != nc["name_en"]
                or oc["name_zh"] != nc["name_zh"]
                or oc["help_text_en"] != nc["help_text_en"]
                or oc["xpath"] != nc["xpath"]
            ):
                report.modified_controls.append({"before": oc, "after": nc})
                impacted_surfs.add(nc["surface_name"])
                for tok in (
                    oc["auto_id"], oc["name_en"], oc["name_zh"],
                    nc["auto_id"], nc["name_en"], nc["name_zh"],
                ):
                    if tok:
                        changed_tokens.add(tok)

    report.impacted_surfaces = sorted(impacted_surfs)

    if bdd_dir and bdd_dir.is_dir() and changed_tokens:
        loc_dir = bdd_dir / "locators"
        steps_dir = bdd_dir / "steps"
        impacted_keys: Set[str] = set()

        if loc_dir.is_dir():
            for yf in sorted(loc_dir.rglob("*.yaml")):
                rel_y = yf.relative_to(loc_dir).as_posix()
                for lkey, spec in _load_locator_yaml_entries(yf):
                    raw_v = str(spec.get("value", ""))
                    if any(tok in raw_v for tok in changed_tokens):
                        impacted_keys.add(lkey)
                        report.impacted_yaml_locators.append({
                            "yaml_file": rel_y,
                            "locator_key": lkey,
                            "value": raw_v,
                        })

        if steps_dir.is_dir() and impacted_keys:
            for pf in sorted(steps_dir.rglob("*.py")):
                txt = pf.read_text(encoding="utf-8", errors="replace")
                if any(k in txt for k in impacted_keys):
                    report.impacted_step_files.append(pf.relative_to(bdd_dir).as_posix())

    return report
