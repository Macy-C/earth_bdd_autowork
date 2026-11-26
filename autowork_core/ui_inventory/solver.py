"""Zero-Index Unique XPath Selector Solver & Static UIA XPath Verifier.

Implements the empirically grounded 4-Tier cost-progressive solver (Chapters 6.1–6.4 of
`messy/ui控件知识库.md`) and validates every emitted selector against both
`autowork_core.common.winauto_xpath.parse_xpath` and a faithful UIA tree replay evaluator.
"""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from autowork_core.common.winauto_xpath import parse_xpath
from autowork_core.ui_inventory.models import (
    IdentityGapIssue,
    ProjectedNode,
    ProjectedSurface,
    SolvedLocator,
    WorkspaceIndex,
)


GENERIC_AUTO_ID_RE = re.compile(
    r"^(?:[A-Za-z_][\w.]*:)?(?:Window|Button|RepeatButton|ToggleButton|RadioButton|CheckBox|"
    r"TextBox|ComboBox|ComboBoxItem|TextBlock|Label|TabControl|TabItem|ListBox|ListBoxItem|"
    r"ListView|ListViewItem|DataGrid|TreeView|TreeViewItem|Slider|ScrollBar|Thumb|ProgressBar|"
    r"SplitButtonItem|Image|Border|Grid|StackPanel|DockPanel|UserControl|Separator|MenuItem|"
    r"ToolBar|GroupBox|Expander|ScrollViewer|ContentControl|Canvas|WrapPanel)_\d+$"
    r"|^(?:button|textBox|comboBox|checkBox|radioButton|listBox|listView|treeView|label|"
    r"panel|groupBox|tabControl|tabPage|numericUpDown|slider|dataGridView|menuItem)\d+$"
    r"|^[A-Za-z_][\w.]*:[A-Za-z_]\w*_\d+$"
    r"|^PART_[A-Za-z0-9_]+$",
    re.IGNORECASE,
)

CONTAINER_UIA_TYPES = frozenset({
    "Custom",
    "Group",
    "TabItem",
    "Tab",
    "HeaderItem",
    "DataGrid",
    "List",
    "Tree",
    "Pane",
    "ToolBar",
})


def is_weak_generic_auto_id(auto_id: str) -> bool:
    if not auto_id:
        return True
    return bool(GENERIC_AUTO_ID_RE.match(auto_id))


def is_usable_xpath_literal(val: str, is_static: bool = True) -> bool:
    """Check whether a property string can be safely embedded into `winauto_xpath`."""
    if not is_static or not val:
        return False
    if len(val) > 96 or "\n" in val or "\r" in val or "\t" in val:
        return False
    if "'" in val and '"' in val:
        return False
    if not val.strip():
        return False
    return True


def quote_xpath_literal(val: str) -> str:
    if "'" in val:
        return f'"{val}"'
    return f"'{val}'"


def format_action_contract(node: ProjectedNode) -> str:
    """Generate concise BDD Autowork runtime API & property guidance per Iron Laws 4, 7, 8."""
    if node.topology in ("PopupTopLevel", "ContextMenuTopLevel"):
        return "Popup/ContextMenu top-level HWND: outside host Window UIA subtree; use host `region` + `by: 'ocr'` or top-level Popup window."
    if node.topology == "EmbeddedWebHost":
        return "Embedded Web Host (CefSharp/WebView2): inner web DOM controls are NOT in static WPF XAML; inspect web UI or use host `region` + `ocr`/WebView locator."

    ut = node.uia_type
    pre = f" [Precondition: {' -> '.join(node.preconditions)}]" if node.preconditions else ""
    if ut == "CheckBox":
        return f"set_checked(target, True/False); verify via is_checked{pre}"
    if ut == "RadioButton":
        return f"select_radio(target); verify via is_selected{pre}"
    if ut == "TabItem":
        return f"select_tab(target); verify via is_selected{pre}"
    if ut == "ComboBox":
        if node.short_clr_type.startswith("Split"):
            return f"SplitButton (UIA ComboBox): click(target) or expand + Popup OCR{pre}"
        if node.is_editable:
            return f"ComboBox(IsEditable=True): select_dropdown_option / input_text; supports `value.value` and `selection`{pre}"
        return f"ComboBox(IsEditable=False): select_dropdown_option; read via `selection` ONLY (`value.value` unsupported){pre}"
    if ut in ("Edit", "Document"):
        if node.is_readonly:
            return f"ReadOnly {ut}: read via `value.value` ONLY (do NOT input_text){pre}"
        return f"input_text(target, text); read via `value.value`{pre}"
    if ut == "Slider":
        return f"drag/set slider; read via `value.value`{pre}"
    if ut in ("DataGrid", "List", "Tree"):
        return f"Container `{ut}`: row/item children attach dynamically at runtime{pre}"
    return f"click(target); wait_enabled / is_visible{pre}"


# ---------------------------------------------------------------------------
# Faithful Static UIA Tree XPath Evaluator (mirrors `winauto_xpath.py` 100%)
# ---------------------------------------------------------------------------
def _node_attr(node: ProjectedNode, raw_key: str, default_locale: str = "en-US") -> Optional[str]:
    key = raw_key.strip().lower()
    if key in ("auto_id", "automation_id", "automationid", "id"):
        return node.auto_id
    if key in ("name", "text", "title", "caption", "label"):
        if default_locale.lower().startswith("zh") and node.name_zh:
            return node.name_zh
        return node.name_en
    if key in ("help_text", "helptext"):
        if default_locale.lower().startswith("zh") and node.help_text_zh:
            return node.help_text_zh
        return node.help_text_en
    if key in ("class_name", "class", "classname"):
        return node.short_clr_type
    if key == "visible":
        return "true" if node.is_initially_visible else "false"
    if key == "enabled":
        return "true"
    return None


def _match_node_step(node: ProjectedNode, step: Dict[str, Any], default_locale: str) -> bool:
    tag = step["tag"]
    if tag != "*" and node.uia_type.lower() != tag.lower():
        return False
    for k, expected in step["equals"]:
        actual = _node_attr(node, k, default_locale)
        if actual is None or str(actual) != str(expected):
            return False
    for k, expected in step["contains"]:
        actual = _node_attr(node, k, default_locale)
        if actual is None or str(expected).lower() not in str(actual).lower():
            return False
    return True


def _iter_descendants(node: ProjectedNode) -> List[ProjectedNode]:
    out: List[ProjectedNode] = []
    for ch in node.children:
        out.append(ch)
        out.extend(_iter_descendants(ch))
    return out


def eval_xpath_on_tree(
    root: ProjectedNode,
    xpath: str,
    *,
    first_only: bool = False,
    default_locale: str = "en-US",
) -> List[ProjectedNode]:
    """Evaluate `winauto_xpath` expression against a `ProjectedNode` tree."""
    steps = parse_xpath(xpath)
    current: List[ProjectedNode] = [root]

    for step_idx, step in enumerate(steps):
        axis = step["axis"]
        is_last = step_idx == len(steps) - 1
        next_nodes: List[ProjectedNode] = []

        for pnode in current:
            if axis == "child":
                cands = list(pnode.children)
            elif axis == "desc":
                cands = _iter_descendants(pnode)
                if step_idx == 0:
                    cands = [pnode] + cands
            elif axis == "next":
                if pnode.parent is None:
                    cands = []
                else:
                    sibs = pnode.parent.children
                    cands = sibs[pnode.sibling_index + 1 :]
            elif axis == "prev":
                if pnode.parent is None:
                    cands = []
                else:
                    sibs = pnode.parent.children
                    cands = list(reversed(sibs[: pnode.sibling_index]))
            elif axis == "parent":
                cands = [pnode.parent] if pnode.parent is not None else []
            elif axis == "ancestor":
                cands = []
                anc = pnode.parent
                while anc is not None:
                    cands.append(anc)
                    anc = anc.parent
            else:
                raise ValueError(f"Unsupported axis: {axis}")

            if first_only and is_last and step["index"] is None:
                for c in cands:
                    if _match_node_step(c, step, default_locale):
                        return [c]
                continue

            matched = [c for c in cands if _match_node_step(c, step, default_locale)]
            if step["index"] is not None:
                idx_val = step["index"]
                try:
                    matched = [matched[idx_val]]
                except IndexError:
                    matched = []
            next_nodes.extend(matched)

        current = next_nodes
        if not current:
            return []

    if first_only:
        return [current[0]]
    return current


# ---------------------------------------------------------------------------
# Fast Window-Scoped Index + 4-Tier Zero-Index Solver
# ---------------------------------------------------------------------------
class _WindowPoolIndex:
    """O(1) hash + Euler-tour interval index over a host window's projected node pool $W$."""

    def __init__(self, win_root: ProjectedNode, default_locale: str = "en-US") -> None:
        self.win_root = win_root
        self.locale = default_locale
        self.all_nodes: List[ProjectedNode] = []
        self.tin: Dict[str, int] = {}
        self.tout: Dict[str, int] = {}
        self.by_tag: Dict[str, List[ProjectedNode]] = defaultdict(list)
        self.by_tag_aid: Dict[Tuple[str, str], List[ProjectedNode]] = defaultdict(list)
        self.by_tag_name: Dict[Tuple[str, str], List[ProjectedNode]] = defaultdict(list)
        self.by_tag_help: Dict[Tuple[str, str], List[ProjectedNode]] = defaultdict(list)
        self.container_sel_cache: Dict[str, Optional[str]] = {}

        clock = [0]

        def dfs(n: ProjectedNode) -> None:
            clock[0] += 1
            self.tin[n.node_id] = clock[0]
            self.all_nodes.append(n)
            for ch in n.children:
                dfs(ch)
            self.tout[n.node_id] = clock[0]

        dfs(win_root)

        for n in self.all_nodes:
            ut = n.uia_type
            self.by_tag[ut].append(n)
            if n.auto_id:
                self.by_tag_aid[(ut, n.auto_id)].append(n)
            nm = self.node_name(n)
            if nm and n.is_static_name:
                self.by_tag_name[(ut, nm)].append(n)
            ht = self.node_help(n)
            if ht and n.is_static_help_text:
                self.by_tag_help[(ut, ht)].append(n)

    def is_descendant(self, anc: ProjectedNode, cand: ProjectedNode) -> bool:
        t_cand = self.tin[cand.node_id]
        return self.tin[anc.node_id] < t_cand <= self.tout[anc.node_id]

    def node_name(self, n: ProjectedNode) -> str:
        if self.locale.lower().startswith("zh") and n.name_zh:
            return n.name_zh
        return n.name_en

    def node_help(self, n: ProjectedNode) -> str:
        if self.locale.lower().startswith("zh") and n.help_text_zh:
            return n.help_text_zh
        return n.help_text_en

    def unique_container_selector(self, anc: ProjectedNode, depth: int = 0) -> Optional[str]:
        """Compute a 0-index selector uniquely matching container `anc` in $W$ (if one exists)."""
        if anc is self.win_root or anc.uia_type == "Window":
            return None
        if anc.node_id in self.container_sel_cache:
            return self.container_sel_cache[anc.node_id]
        if depth > 2:
            return None

        ut = anc.uia_type
        aid = anc.auto_id
        nm = self.node_name(anc)

        # 1. Unique @auto_id (prefer semantic over generic)
        if is_usable_xpath_literal(aid, True) and not is_weak_generic_auto_id(aid):
            hits = self.by_tag_aid[(ut, aid)]
            if len(hits) == 1 and hits[0] is anc:
                sel = f"//{ut}[@auto_id={quote_xpath_literal(aid)}]"
                self.container_sel_cache[anc.node_id] = sel
                return sel

        # 2. Unique @name (e.g. Group[@name='Geometry'], TabItem[@name='Series'], HeaderItem[@name='Patient ID'])
        if is_usable_xpath_literal(nm, anc.is_static_name):
            hits = self.by_tag_name[(ut, nm)]
            if len(hits) == 1 and hits[0] is anc:
                sel = f"//{ut}[@name={quote_xpath_literal(nm)}]"
                self.container_sel_cache[anc.node_id] = sel
                return sel

        # 3. Weak @auto_id if globally unique in window
        if is_usable_xpath_literal(aid, True):
            hits = self.by_tag_aid[(ut, aid)]
            if len(hits) == 1 and hits[0] is anc:
                sel = f"//{ut}[@auto_id={quote_xpath_literal(aid)}]"
                self.container_sel_cache[anc.node_id] = sel
                return sel

        # 4. Direct child header Text -> /parent::Container
        for ch in anc.children:
            if ch.uia_type == "Text":
                chnm = self.node_name(ch)
                if is_usable_xpath_literal(chnm, ch.is_static_name):
                    thits = self.by_tag_name[("Text", chnm)]
                    if len(thits) == 1 and thits[0] is ch:
                        sel = f"//Text[@name={quote_xpath_literal(chnm)}]/parent::{ut}"
                        self.container_sel_cache[anc.node_id] = sel
                        return sel

        # 5. Scoped under an outer unique ancestor container (1 extra hop, still 0 index)
        p = anc.parent
        while p is not None and p is not self.win_root:
            outer_sel = self.unique_container_selector(p, depth + 1)
            if outer_sel:
                if is_usable_xpath_literal(aid, True):
                    sub_hits = [h for h in self.by_tag_aid[(ut, aid)] if self.is_descendant(p, h)]
                    if len(sub_hits) == 1 and sub_hits[0] is anc:
                        cand = f"{outer_sel}//{ut}[@auto_id={quote_xpath_literal(aid)}]"
                        self.container_sel_cache[anc.node_id] = cand
                        return cand
                if is_usable_xpath_literal(nm, anc.is_static_name):
                    sub_hits = [h for h in self.by_tag_name[(ut, nm)] if self.is_descendant(p, h)]
                    if len(sub_hits) == 1 and sub_hits[0] is anc:
                        cand = f"{outer_sel}//{ut}[@name={quote_xpath_literal(nm)}]"
                        self.container_sel_cache[anc.node_id] = cand
                        return cand
            p = p.parent

        self.container_sel_cache[anc.node_id] = None
        return None


def _clone_tree_collapsed(node: ProjectedNode) -> ProjectedNode:
    """Shallow-copy a sibling mode's subtree with `is_initially_visible=False` (Iron Law 6)."""
    copied_kids = [_clone_tree_collapsed(ch) for ch in node.children]
    clone = ProjectedNode(
        node_id=f"resident_collapsed::{node.node_id}",
        source_elem_id=node.source_elem_id,
        source_def_id=node.source_def_id,
        repo_id=node.repo_id,
        assembly=node.assembly,
        file=node.file,
        line=node.line,
        clr_type=node.clr_type,
        short_clr_type=node.short_clr_type,
        uia_type=node.uia_type,
        auto_id=node.auto_id,
        auto_id_source=node.auto_id_source,
        raw_x_uid=node.raw_x_uid,
        raw_x_name=node.raw_x_name,
        name_en=node.name_en,
        name_zh=node.name_zh,
        is_static_name=node.is_static_name,
        help_text_en=node.help_text_en,
        help_text_zh=node.help_text_zh,
        is_static_help_text=node.is_static_help_text,
        is_editable=node.is_editable,
        is_readonly=node.is_readonly,
        is_initially_visible=False,
        mode_name=node.mode_name,
        panel_group=node.panel_group,
        topology=node.topology,
        children=copied_kids,
    )
    for idx_c, ck in enumerate(copied_kids):
        ck.parent = clone
        ck.sibling_index = idx_c
    return clone


def _build_window_root_for_surface(
    surf: ProjectedSurface,
    sibling_mode_surfaces: List[ProjectedSurface],
) -> Tuple[ProjectedNode, List[Tuple[ProjectedNode, Optional[ProjectedNode], int]]]:
    """Attach resident-collapsed sibling mode panels temporarily onto `surf.root_node` so `E` stays object-identical."""
    assert surf.root_node is not None
    win_root = surf.root_node
    if not sibling_mode_surfaces:
        return win_root, []

    active_def_ids = {ch.source_def_id for ch in win_root.children}
    added_clones: List[ProjectedNode] = []
    for sib_surf in sibling_mode_surfaces:
        if sib_surf.surface_id == surf.surface_id or sib_surf.root_node is None:
            continue
        for ch in sib_surf.root_node.children:
            if ch.source_def_id in active_def_ids:
                continue  # Common host panel (e.g. CTViewControlPanel) is shared, not duplicated
            cl = _clone_tree_collapsed(ch)
            cl.parent = win_root
            cl.sibling_index = len(win_root.children)
            win_root.children.append(cl)
            added_clones.append(cl)

    return win_root, [(cl, None, 0) for cl in added_clones]


def solve_all_surfaces(idx: WorkspaceIndex, *, default_locale: str = "en-US") -> None:
    """Solve zero-index unique selectors for all interactive controls across all surfaces."""
    modes_by_host: Dict[str, List[ProjectedSurface]] = defaultdict(list)
    for surf in idx.projected_surfaces.values():
        if surf.surface_kind == "ModeSurface":
            modes_by_host[surf.host_window_id].append(surf)

    record_gaps_kinds = {"HostWindow", "ModeSurface", "AddInSurface", "ModalDialog", "StandaloneForm"}

    for surf_id in sorted(idx.projected_surfaces):
        surf = idx.projected_surfaces[surf_id]
        if surf.root_node is None:
            continue

        sib_modes = modes_by_host.get(surf.host_window_id, []) if surf.surface_kind == "ModeSurface" else []
        orig_child_count = len(surf.root_node.children)
        win_root, _ = _build_window_root_for_surface(surf, sib_modes)
        try:
            pool = _WindowPoolIndex(win_root, default_locale=default_locale)
            active_pool = _WindowPoolIndex(surf.root_node, default_locale=default_locale) if not sib_modes else None
            if sib_modes:
                # Active surface nodes are the prefix `win_root.children[:orig_child_count]`
                active_node_ids = {surf.root_node.node_id}
                for ch in surf.root_node.children[:orig_child_count]:
                    active_node_ids.add(ch.node_id)
                    for d in _iter_descendants(ch):
                        active_node_ids.add(d.node_id)
            else:
                active_node_ids = {n.node_id for n in pool.all_nodes}

            for node in surf.interactive_nodes:
                node.action_contract = format_action_contract(node)
                solved = _solve_single_node(node, pool, active_node_ids, default_locale=default_locale)
                node.solved = solved

                if surf.surface_kind in record_gaps_kinds:
                    if solved.uses_index:
                        idx.identity_gap_issues.append(
                            IdentityGapIssue(
                                severity="High",
                                gap_kind="POSITIONAL_INDEX_FALLBACK",
                                repo_id=node.repo_id,
                                surface_name=surf.surface_name,
                                file=node.file,
                                line=node.line,
                                uia_type=node.uia_type,
                                clr_type=node.short_clr_type,
                                effective_auto_id=node.auto_id,
                                dissolved_wrapper_name=node.dissolved_wrapper_name,
                                adjacent_label=node.adjacent_label,
                                current_xpath=solved.primary_xpath,
                                recommendation=(
                                    "Control cannot be uniquely distinguished without positional indexing inside its static container; "
                                    "assign a semantic x:Name/x:Uid or AutomationProperties.AutomationId in source XAML."
                                ),
                            )
                        )
                    elif solved.weak_auto_id and solved.tier == "Tier 1C":
                        idx.identity_gap_issues.append(
                            IdentityGapIssue(
                                severity="Low",
                                gap_kind="WEAK_GENERIC_AUTO_ID",
                                repo_id=node.repo_id,
                                surface_name=surf.surface_name,
                                file=node.file,
                                line=node.line,
                                uia_type=node.uia_type,
                                clr_type=node.short_clr_type,
                                effective_auto_id=node.auto_id,
                                dissolved_wrapper_name=node.dissolved_wrapper_name,
                                adjacent_label=node.adjacent_label,
                                current_xpath=solved.primary_xpath,
                                recommendation=(
                                    f"Auto-generated weak @auto_id='{node.auto_id}' is currently unique in window, "
                                    f"but may shift if controls are inserted; replace with semantic x:Name."
                                ),
                            )
                        )
        finally:
            if len(surf.root_node.children) > orig_child_count:
                del surf.root_node.children[orig_child_count:]


def _verify_candidate(
    win_root: ProjectedNode,
    target: ProjectedNode,
    xpath: str,
    *,
    allow_sibling_first_match: bool,
    default_locale: str,
) -> Tuple[bool, bool]:
    """Double self-check gate (Sec 6.4):
    Returns `(valid_unique_for_runtime, globally_single_in_find_all)`.
    """
    try:
        parse_xpath(xpath)
        all_hits = eval_xpath_on_tree(win_root, xpath, first_only=False, default_locale=default_locale)
    except Exception:
        return False, False

    if len(all_hits) == 1 and all_hits[0] is target:
        return True, True

    if allow_sibling_first_match and all_hits and all_hits[0] is target:
        first_hit = eval_xpath_on_tree(win_root, xpath, first_only=True, default_locale=default_locale)
        if len(first_hit) == 1 and first_hit[0] is target:
            return True, False

    return False, False


def _solve_single_node(
    node: ProjectedNode,
    pool: _WindowPoolIndex,
    active_node_ids: set[str],
    *,
    default_locale: str = "en-US",
) -> SolvedLocator:
    # Iron Law 7: Top-level Popup / ContextMenu escapes host Window UIA subtree
    if node.topology in ("PopupTopLevel", "ContextMenuTopLevel"):
        nm = pool.node_name(node)
        if is_usable_xpath_literal(nm, node.is_static_name):
            ocr_primary = f"OCR(region=host_window, value={quote_xpath_literal(nm)})"
            popup_alt = f"//Window[@class_name='Popup']//{node.uia_type}[@name={quote_xpath_literal(nm)}]"
        elif is_usable_xpath_literal(node.auto_id, True):
            ocr_primary = f"//Window[@class_name='Popup']//{node.uia_type}[@auto_id={quote_xpath_literal(node.auto_id)}]"
            popup_alt = ""
        else:
            ocr_primary = f"OCR(region=host_window) /* {node.topology}: {node.short_clr_type} */"
            popup_alt = ""
        return SolvedLocator(
            primary_xpath=ocr_primary,
            alt_xpath=popup_alt,
            tier="Popup-OCR",
            uses_index=False,
            notes="WPF Popup/ContextMenu renders in separate top-level HwndWrapper outside host Window subtree.",
        )

    # First-class boundary host: Embedded WebView (CefSharp / WebView2 / WebBrowser)
    if node.topology == "EmbeddedWebHost":
        aid = node.auto_id
        if is_usable_xpath_literal(aid, True):
            web_primary = f"//{node.uia_type}[@auto_id={quote_xpath_literal(aid)}]"
        else:
            web_primary = f"WebHost(class={node.short_clr_type})"
        return SolvedLocator(
            primary_xpath=web_primary,
            alt_xpath="",
            tier="WebHost-External",
            uses_index=False,
            notes="Embedded web browser host; internal web DOM controls are outside static WPF XAML.",
        )

    ut = node.uia_type
    aid = node.auto_id
    nm = pool.node_name(node)
    ht = pool.node_help(node)
    has_aid = is_usable_xpath_literal(aid, True)
    weak_aid = is_weak_generic_auto_id(aid)
    has_nm = is_usable_xpath_literal(nm, node.is_static_name)
    has_ht = is_usable_xpath_literal(ht, node.is_static_help_text)

    def collect_alt_zero_index(exclude_xp: str) -> str:
        cands: List[Tuple[str, List[ProjectedNode]]] = []
        if has_aid:
            cands.append((f"//{ut}[@auto_id={quote_xpath_literal(aid)}]", pool.by_tag_aid[(ut, aid)]))
        if has_nm:
            cands.append((f"//{ut}[@name={quote_xpath_literal(nm)}]", pool.by_tag_name[(ut, nm)]))
        if has_ht:
            cands.append((f"//{ut}[@help_text={quote_xpath_literal(ht)}]", pool.by_tag_help[(ut, ht)]))
        for ch in node.children:
            if ch.uia_type == "Text" and is_usable_xpath_literal(pool.node_name(ch), ch.is_static_name):
                chnm = pool.node_name(ch)
                cands.append((
                    f"//Text[@name={quote_xpath_literal(chnm)}]/parent::{ut}",
                    [t.parent for t in pool.by_tag_name[("Text", chnm)] if t.parent is not None and t.parent.uia_type == ut],
                ))
        for c, hits in cands:
            if c != exclude_xp and len(hits) == 1 and hits[0] is node:
                ok, single_all = _verify_candidate(
                    pool.win_root, node, c, allow_sibling_first_match=False, default_locale=default_locale
                )
                if ok and single_all:
                    return c
        return ""

    # ------------------------------------------------------------------
    # Tier 1: Single self-attribute globally unique in host Window $W$
    # ------------------------------------------------------------------
    # Tier 1A: Semantic @auto_id
    if has_aid and not weak_aid and len(pool.by_tag_aid[(ut, aid)]) == 1:
        xp = f"//{ut}[@auto_id={quote_xpath_literal(aid)}]"
        ok, _ = _verify_candidate(pool.win_root, node, xp, allow_sibling_first_match=False, default_locale=default_locale)
        if ok:
            return SolvedLocator(primary_xpath=xp, alt_xpath=collect_alt_zero_index(xp), tier="Tier 1A")

    # Tier 1B: Unique static @name or @help_text
    if has_nm and len(pool.by_tag_name[(ut, nm)]) == 1:
        xp = f"//{ut}[@name={quote_xpath_literal(nm)}]"
        ok, _ = _verify_candidate(pool.win_root, node, xp, allow_sibling_first_match=False, default_locale=default_locale)
        if ok:
            return SolvedLocator(primary_xpath=xp, alt_xpath=collect_alt_zero_index(xp), tier="Tier 1B")

    if has_ht and len(pool.by_tag_help[(ut, ht)]) == 1:
        xp = f"//{ut}[@help_text={quote_xpath_literal(ht)}]"
        ok, _ = _verify_candidate(pool.win_root, node, xp, allow_sibling_first_match=False, default_locale=default_locale)
        if ok:
            return SolvedLocator(primary_xpath=xp, alt_xpath=collect_alt_zero_index(xp), tier="Tier 1B")

    # Tier 1C: Weak generic @auto_id that happens to be globally unique in $W$
    if has_aid and weak_aid and len(pool.by_tag_aid[(ut, aid)]) == 1:
        xp = f"//{ut}[@auto_id={quote_xpath_literal(aid)}]"
        ok, _ = _verify_candidate(pool.win_root, node, xp, allow_sibling_first_match=False, default_locale=default_locale)
        if ok:
            return SolvedLocator(
                primary_xpath=xp,
                alt_xpath=collect_alt_zero_index(xp),
                tier="Tier 1C",
                weak_auto_id=True,
                notes="Weak auto-generated @auto_id (unique in current window).",
            )

    # ------------------------------------------------------------------
    # Tier 2: Dual self-attributes OR unique child Text -> /parent::Tag
    # ------------------------------------------------------------------
    dual_specs: List[Tuple[str, List[ProjectedNode]]] = []
    if has_aid and has_nm:
        dual_specs.append((
            f"//{ut}[@auto_id={quote_xpath_literal(aid)} and @name={quote_xpath_literal(nm)}]",
            [h for h in pool.by_tag_aid[(ut, aid)] if h.is_static_name and pool.node_name(h) == nm],
        ))
    if has_aid and has_ht:
        dual_specs.append((
            f"//{ut}[@auto_id={quote_xpath_literal(aid)} and @help_text={quote_xpath_literal(ht)}]",
            [h for h in pool.by_tag_aid[(ut, aid)] if h.is_static_help_text and pool.node_help(h) == ht],
        ))
    if has_nm and has_ht and nm != ht:
        dual_specs.append((
            f"//{ut}[@name={quote_xpath_literal(nm)} and @help_text={quote_xpath_literal(ht)}]",
            [h for h in pool.by_tag_name[(ut, nm)] if h.is_static_help_text and pool.node_help(h) == ht],
        ))

    for xp, dhits in dual_specs:
        if len(dhits) == 1 and dhits[0] is node:
            ok, _ = _verify_candidate(pool.win_root, node, xp, allow_sibling_first_match=False, default_locale=default_locale)
            if ok:
                return SolvedLocator(primary_xpath=xp, tier="Tier 2A")

    # Tier 2B: Unique inner Text child -> /parent::Tag (preserves Button/CheckBox/TabItem UIA patterns!)
    child_text_rel_specs: List[Tuple[str, List[ProjectedNode]]] = []
    for ch in node.children:
        if ch.uia_type == "Text":
            chnm = pool.node_name(ch)
            if is_usable_xpath_literal(chnm, ch.is_static_name):
                rel = f"Text[@name={quote_xpath_literal(chnm)}]/parent::{ut}"
                thits = pool.by_tag_name[("Text", chnm)]
                phits = [t.parent for t in thits if t.parent is not None and t.parent.uia_type == ut]
                child_text_rel_specs.append((rel, phits))
                if len(thits) == 1 and len(phits) == 1 and phits[0] is node:
                    xp = f"//{rel}"
                    ok, _ = _verify_candidate(
                        pool.win_root, node, xp, allow_sibling_first_match=False, default_locale=default_locale
                    )
                    if ok:
                        return SolvedLocator(primary_xpath=xp, tier="Tier 2B")

    # Build ordered relative expressions + pre-indexed global match lists for O(1) Tier 3 scoping!
    rel_specs: List[Tuple[str, List[ProjectedNode]]] = []
    if has_aid and not weak_aid:
        rel_specs.append((f"{ut}[@auto_id={quote_xpath_literal(aid)}]", pool.by_tag_aid[(ut, aid)]))
    if has_nm:
        rel_specs.append((f"{ut}[@name={quote_xpath_literal(nm)}]", pool.by_tag_name[(ut, nm)]))
    if has_ht:
        rel_specs.append((f"{ut}[@help_text={quote_xpath_literal(ht)}]", pool.by_tag_help[(ut, ht)]))
    if has_aid and has_nm:
        rel_specs.append((
            f"{ut}[@auto_id={quote_xpath_literal(aid)} and @name={quote_xpath_literal(nm)}]",
            [h for h in pool.by_tag_aid[(ut, aid)] if h.is_static_name and pool.node_name(h) == nm],
        ))
    rel_specs.extend(child_text_rel_specs)
    if has_aid and weak_aid:
        rel_specs.append((f"{ut}[@auto_id={quote_xpath_literal(aid)}]", pool.by_tag_aid[(ut, aid)]))
    rel_specs.append((ut, pool.by_tag[ut]))

    # Collect unique ancestor container selectors (nearest first)
    unique_ancestors: List[Tuple[ProjectedNode, str]] = []
    anc = node.parent
    while anc is not None and anc is not pool.win_root:
        asel = pool.unique_container_selector(anc)
        if asel:
            unique_ancestors.append((anc, asel))
        anc = anc.parent

    # ------------------------------------------------------------------
    # Tier 3A: Cross-Mode Resident Collapsed Panel Disambiguation
    # ------------------------------------------------------------------
    if node.is_initially_visible:
        for rel, all_hits in rel_specs[:-1]:  # exclude bare `Tag`
            active_hits = [h for h in all_hits if h.node_id in active_node_ids]
            if len(active_hits) == 1 and active_hits[0] is node and len(all_hits) > 1:
                base_xp = f"//{rel}"
                # Prefer scoping to mode's unique Custom/Group container first
                for anc_node, asel in unique_ancestors:
                    sub_hits = [h for h in all_hits if pool.is_descendant(anc_node, h)]
                    if len(sub_hits) == 1 and sub_hits[0] is node:
                        scoped_xp = f"{asel}//{rel}"
                        ok, _ = _verify_candidate(
                            pool.win_root, node, scoped_xp, allow_sibling_first_match=False, default_locale=default_locale
                        )
                        if ok:
                            return SolvedLocator(
                                primary_xpath=scoped_xp,
                                alt_xpath=_append_visible_true(base_xp),
                                tier="Tier 3A",
                                notes="Scoped to active mode panel to avoid resident Collapsed sibling mode collision.",
                            )
                vis_hits = [h for h in all_hits if h.is_initially_visible]
                vis_xp = _append_visible_true(base_xp)
                if vis_xp and len(vis_hits) == 1 and vis_hits[0] is node:
                    ok, _ = _verify_candidate(
                        pool.win_root, node, vis_xp, allow_sibling_first_match=False, default_locale=default_locale
                    )
                    if ok:
                        return SolvedLocator(
                            primary_xpath=vis_xp,
                            tier="Tier 3A",
                            notes="Filtered by @visible='true' to exclude resident Collapsed mode panels.",
                        )

    # ------------------------------------------------------------------
    # Tier 3B: Sibling Static `Text` Anchor (`/next::Tag` or `/prev::Tag`)
    # ------------------------------------------------------------------
    sib_cand = _try_sibling_text_anchor(node, pool, unique_ancestors, default_locale=default_locale)
    if sib_cand is not None:
        return sib_cand

    # ------------------------------------------------------------------
    # Tier 3C: Nearest Unique Ancestor Container Scoping (`<Sel(A)>//<Rel>`)
    # ------------------------------------------------------------------
    for anc_node, asel in unique_ancestors:
        for rel, all_hits in rel_specs:
            # Direct child `/` check when `anc_node` is `node.parent` (and `rel` is not a `/parent::` step)
            if anc_node is node.parent and "/parent::" not in rel:
                child_hits = [h for h in all_hits if h.parent is anc_node]
                if len(child_hits) == 1 and child_hits[0] is node:
                    cand_xp = f"{asel}/{rel}"
                    ok, _ = _verify_candidate(
                        pool.win_root, node, cand_xp, allow_sibling_first_match=False, default_locale=default_locale
                    )
                    if ok:
                        return SolvedLocator(primary_xpath=cand_xp, tier="Tier 3C")

            desc_hits = [h for h in all_hits if pool.is_descendant(anc_node, h)]
            if len(desc_hits) == 1 and desc_hits[0] is node:
                cand_xp = f"{asel}//{rel}"
                ok, _ = _verify_candidate(
                    pool.win_root, node, cand_xp, allow_sibling_first_match=False, default_locale=default_locale
                )
                if ok:
                    return SolvedLocator(primary_xpath=cand_xp, tier="Tier 3C")

    # ------------------------------------------------------------------
    # Tier 4: Bounded Container Scoped 0-Based Index Fallback (+ Audit Gap)
    # ------------------------------------------------------------------
    best_rel, best_hits = rel_specs[0]  # Most specific self predicate (e.g. `Edit[@auto_id='PART_TextBox']`)
    if unique_ancestors:
        nearest_anc, nearest_sel = unique_ancestors[0]
        base_scoped = f"{nearest_sel}//{best_rel}"
        matches = [h for h in best_hits if pool.is_descendant(nearest_anc, h)]
    else:
        base_scoped = f"//{best_rel}"
        matches = best_hits

    for k, mnode in enumerate(matches):
        if mnode is node:
            indexed_xp = f"{base_scoped}[{k}]"
            ok, _ = _verify_candidate(
                pool.win_root, node, indexed_xp, allow_sibling_first_match=False, default_locale=default_locale
            )
            if ok:
                return SolvedLocator(
                    primary_xpath=indexed_xp,
                    tier="Tier 4",
                    uses_index=True,
                    weak_auto_id=weak_aid,
                    notes="Bounded positional index fallback; add semantic x:Name in source XAML.",
                )

    # Ultimate structural fallback (guaranteed valid)
    all_tag_hits = pool.by_tag[ut]
    idx_global = next((i for i, n in enumerate(all_tag_hits) if n is node), 0)
    fallback_xp = f"//{ut}[{idx_global}]"
    return SolvedLocator(
        primary_xpath=fallback_xp,
        tier="Tier 4",
        uses_index=True,
        weak_auto_id=True,
        notes="Global positional fallback.",
    )


def _append_visible_true(single_step_xpath: str) -> str:
    if single_step_xpath.endswith("]") and "/parent::" not in single_step_xpath:
        return single_step_xpath[:-1] + " and @visible='true']"
    return ""


def _try_sibling_text_anchor(
    node: ProjectedNode,
    pool: _WindowPoolIndex,
    unique_ancestors: List[Tuple[ProjectedNode, str]],
    *,
    default_locale: str,
) -> Optional[SolvedLocator]:
    if node.parent is None:
        return None

    ut = node.uia_type
    aid = node.auto_id
    has_aid = is_usable_xpath_literal(aid, True)
    target_step_variants = [ut]
    if has_aid:
        target_step_variants.append(f"{ut}[@auto_id={quote_xpath_literal(aid)}]")

    sibs = node.parent.children
    pos = node.sibling_index

    # Gather nearest preceding Text (`next::`) and immediate following Text (`prev::`)
    anchor_specs: List[Tuple[ProjectedNode, str, str]] = []  # (text_node, axis_name, target_step)

    for t_step in target_step_variants:
        # Search backward for nearest static Text with zero intervening matching siblings
        for i in range(pos - 1, -1, -1):
            s = sibs[i]
            if s.uia_type == ut:
                if t_step == ut or s.auto_id == aid:
                    break  # Intervening sibling of same target step blocks zero-index `next::`!
            if s.uia_type == "Text" and is_usable_xpath_literal(pool.node_name(s), s.is_static_name):
                anchor_specs.append((s, "next", t_step))
                break

        # Search forward for nearest static Text with zero intervening matching siblings
        for i in range(pos + 1, len(sibs)):
            s = sibs[i]
            if s.uia_type == ut:
                if t_step == ut or s.auto_id == aid:
                    break
            if s.uia_type == "Text" and is_usable_xpath_literal(pool.node_name(s), s.is_static_name):
                anchor_specs.append((s, "prev", t_step))
                break

    for txt_node, axis_dir, t_step in anchor_specs:
        lbl = pool.node_name(txt_node)
        qlbl = quote_xpath_literal(lbl)

        # Case 1: Anchor Text is globally unique in the entire host window $W$
        if len(pool.by_tag_name[("Text", lbl)]) == 1:
            xp = f"//Text[@name={qlbl}]/{axis_dir}::{t_step}"
            ok, single_all = _verify_candidate(
                pool.win_root, node, xp, allow_sibling_first_match=True, default_locale=default_locale
            )
            if ok:
                alt = "" if single_all else f"{xp}[0]"
                return SolvedLocator(primary_xpath=xp, alt_xpath=alt, tier="Tier 3B")

        # Case 2: Scope anchor Text under nearest unique ancestor container $A$
        txt_all = pool.by_tag_name[("Text", lbl)]
        for anc_node, asel in unique_ancestors:
            anchor_hits = [t for t in txt_all if pool.is_descendant(anc_node, t)]
            if len(anchor_hits) == 1 and anchor_hits[0] is txt_node:
                xp = f"{asel}//Text[@name={qlbl}]/{axis_dir}::{t_step}"
                ok, single_all = _verify_candidate(
                    pool.win_root, node, xp, allow_sibling_first_match=True, default_locale=default_locale
                )
                if ok:
                    alt = "" if single_all else f"{xp}[0]"
                    return SolvedLocator(primary_xpath=xp, alt_xpath=alt, tier="Tier 3B")

    return None
