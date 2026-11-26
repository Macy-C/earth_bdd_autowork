"""In-memory data models for Cross-Repo UI Linking, UIA Projection, Selector Solving, and Auditing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class ResolvedTypeSpec:
    full_name: str
    short_name: str
    assembly: str
    base_full_name: str
    uia_control_type: Optional[str]
    has_uia_peer: bool
    has_custom_peer: bool = False
    addin_name: str = ""
    type_source: str = "verified"  # verified | inferred_external


@dataclass
class SolvedLocator:
    primary_xpath: str
    alt_xpath: str = ""
    tier: str = "NonTarget"
    uses_index: bool = False
    weak_auto_id: bool = False
    notes: str = ""


@dataclass
class ProjectedNode:
    node_id: str
    source_elem_id: str
    source_def_id: str
    repo_id: str
    assembly: str
    file: str
    line: int
    clr_type: str
    short_clr_type: str
    uia_type: str
    auto_id: str = ""
    auto_id_source: str = ""  # AutomationId | x:Uid | x:Name | ""
    raw_x_uid: str = ""
    raw_x_name: str = ""
    dissolved_wrapper_name: str = ""
    dissolved_wrapper_type: str = ""
    wrapper_instance_key: str = ""
    name_en: str = ""
    name_zh: str = ""
    is_static_name: bool = False
    help_text_en: str = ""
    help_text_zh: str = ""
    is_static_help_text: bool = False
    is_editable: Optional[bool] = None
    is_readonly: Optional[bool] = None
    is_initially_visible: bool = True
    mode_name: str = ""
    panel_group: str = ""
    preconditions: List[str] = field(default_factory=list)
    topology: str = "InWindow"  # InWindow | OwnedDialog | PopupTopLevel | ContextMenuTopLevel | StandaloneWindow | EmbeddedWebHost
    type_source: str = "verified"  # verified | inferred_external
    text_source: str = ""  # literal | xstatic | resource | vm_binding_static | ""
    binding_path: str = ""
    command_expr: str = ""
    adjacent_label: str = ""
    action_contract: str = ""
    parent: Optional["ProjectedNode"] = field(default=None, repr=False)
    children: List["ProjectedNode"] = field(default_factory=list, repr=False)
    sibling_index: int = 0
    solved: Optional[SolvedLocator] = None

    @property
    def is_interactive_target(self) -> bool:
        return self.uia_type in INTERACTIVE_UIA_TYPES or self.topology == "EmbeddedWebHost"


INTERACTIVE_UIA_TYPES = frozenset({
    "Button",
    "CheckBox",
    "RadioButton",
    "Edit",
    "Document",
    "ComboBox",
    "List",
    "ListItem",
    "DataGrid",
    "DataItem",
    "HeaderItem",
    "Tab",
    "TabItem",
    "Tree",
    "TreeItem",
    "Slider",
    "MenuItem",
    "Hyperlink",
    "Spinner",
})


@dataclass
class ProjectedSurface:
    surface_id: str
    surface_name: str
    surface_kind: str  # HostWindow | ModeSurface | AddInSurface | ModalDialog | SharedComponent | StandaloneForm
    repo_id: str
    assembly: str
    root_def_id: str
    host_window_id: str
    host_window_title: str
    host_window_auto_id: str
    file: str
    host_window_known: bool = True
    mode_name: str = ""
    root_node: Optional[ProjectedNode] = None
    all_nodes: List[ProjectedNode] = field(default_factory=list)
    interactive_nodes: List[ProjectedNode] = field(default_factory=list)
    mounted_component_defs: List[str] = field(default_factory=list)
    triggered_dialogs: List[str] = field(default_factory=list)


@dataclass
class UidShadowIssue:
    severity: str  # High | Medium
    issue_kind: str  # UID_SHADOWS_NAME | DUPLICATE_UID_IN_DEF
    repo_id: str
    assembly: str
    file: str
    line: int
    def_id: str
    clr_type: str
    x_uid: str
    x_name: str
    effective_auto_id: str
    recommendation: str


@dataclass
class IdentityGapIssue:
    severity: str  # High | Medium | Low
    gap_kind: str  # POSITIONAL_INDEX_FALLBACK | WRAPPER_NAME_DISSOLVED | WEAK_GENERIC_AUTO_ID
    repo_id: str
    surface_name: str
    file: str
    line: int
    uia_type: str
    clr_type: str
    effective_auto_id: str
    dissolved_wrapper_name: str
    adjacent_label: str
    current_xpath: str
    recommendation: str


@dataclass
class DynamicUnmountedIssue:
    category: str  # DYNAMIC_SLOT | PENDING_EXTERNAL_TYPE | UNREFERENCED_VIEW
    repo_id: str
    assembly: str
    file: str
    src_def_id: str
    slot_or_target: str
    detail: str


@dataclass
class LocatorAuditItem:
    yaml_file: str
    locator_key: str
    root_name: str
    by: str
    raw_value: str
    status: str  # OK | UPGRADEABLE_INDEX | CHILD_TEXT_TARGET | SHADOWED_BY_UID | UNVERIFIED_EXTERNAL | INVALID_XPATH
    matched_count: int = 0
    matched_surface: str = ""
    suggested_xpath: str = ""
    detail: str = ""


@dataclass
class DiffImpactReport:
    changed_repos: List[str] = field(default_factory=list)
    added_controls: List[Dict[str, Any]] = field(default_factory=list)
    removed_controls: List[Dict[str, Any]] = field(default_factory=list)
    modified_controls: List[Dict[str, Any]] = field(default_factory=list)
    impacted_surfaces: List[str] = field(default_factory=list)
    impacted_yaml_locators: List[Dict[str, str]] = field(default_factory=list)
    impacted_step_files: List[str] = field(default_factory=list)


@dataclass
class WorkspaceIndex:
    repo_manifests: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    type_table: Dict[str, ResolvedTypeSpec] = field(default_factory=dict)
    short_type_index: Dict[str, List[ResolvedTypeSpec]] = field(default_factory=dict)
    xmlns_map: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # xml_uri -> [(assembly, clr_ns)]
    # Assembly dependency graph from `.csproj` (`assembly -> transitive referenced assembly names`)
    assembly_refs: Dict[str, set[str]] = field(default_factory=dict)
    assembly_repo: Dict[str, str] = field(default_factory=dict)
    strings_by_asm_cls_key: Dict[Tuple[str, str, str], Tuple[str, str]] = field(default_factory=dict)
    strings_by_cls_key: Dict[Tuple[str, str], Tuple[str, str]] = field(default_factory=dict)
    strings_by_key: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    # Scoped & carrier-tiered closed localization universe (excludes C# constants; EN & ZH disambiguated independently)
    loc_by_asm_en: Dict[Tuple[str, str], str] = field(default_factory=dict)
    loc_by_asm_zh: Dict[Tuple[str, str], str] = field(default_factory=dict)
    loc_by_asm_ambiguous_en: set[Tuple[str, str]] = field(default_factory=set)
    loc_by_asm_ambiguous_zh: set[Tuple[str, str]] = field(default_factory=set)
    loc_src_by_asm_en: Dict[Tuple[str, str], str] = field(default_factory=dict)
    loc_src_by_asm_zh: Dict[Tuple[str, str], str] = field(default_factory=dict)
    loc_src_by_asm_ambiguous_en: set[Tuple[str, str]] = field(default_factory=set)
    loc_src_by_asm_ambiguous_zh: set[Tuple[str, str]] = field(default_factory=set)
    loc_dll_by_asm_en: Dict[Tuple[str, str], str] = field(default_factory=dict)
    loc_dll_by_asm_zh: Dict[Tuple[str, str], str] = field(default_factory=dict)
    loc_dll_by_asm_ambiguous_en: set[Tuple[str, str]] = field(default_factory=set)
    loc_dll_by_asm_ambiguous_zh: set[Tuple[str, str]] = field(default_factory=set)
    loc_flat_en: Dict[str, str] = field(default_factory=dict)
    loc_flat_zh: Dict[str, str] = field(default_factory=dict)
    loc_flat_ambiguous_en: set[str] = field(default_factory=set)
    loc_flat_ambiguous_zh: set[str] = field(default_factory=set)
    loc_global_en: Dict[str, str] = field(default_factory=dict)
    loc_global_zh: Dict[str, str] = field(default_factory=dict)
    loc_ambiguous_en_keys: set[str] = field(default_factory=set)
    loc_ambiguous_zh_keys: set[str] = field(default_factory=set)
    # Backward-compatible aggregate views populated alongside tiered tables
    loc_strings_by_asm_key: Dict[Tuple[str, str], Tuple[str, str]] = field(default_factory=dict)
    loc_strings_unique_by_key: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    loc_ambiguous_keys: set[str] = field(default_factory=set)
    # Unified C# member value flow (ViewModel properties + Code-Behind control text assignments)
    vm_props_by_class: Dict[str, Dict[str, Tuple[str, str]]] = field(default_factory=dict)
    vm_assembly_by_class: Dict[str, str] = field(default_factory=dict)
    vm_props_by_short_class: Dict[str, List[str]] = field(default_factory=dict)
    view_to_vms: Dict[str, List[str]] = field(default_factory=dict)
    vm_to_view_def: Dict[str, str] = field(default_factory=dict)
    region_views: Dict[str, List[str]] = field(default_factory=dict)
    # Single authoritative composition graph: parent_def_id -> slot_key -> [child_def_id]
    resolved_mounts: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    configs: List[Dict[str, str]] = field(default_factory=list)
    definitions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    defs_by_class: Dict[str, str] = field(default_factory=dict)
    defs_by_short_class: Dict[str, List[str]] = field(default_factory=dict)
    templates_by_target_type: Dict[str, List[str]] = field(default_factory=dict)
    templates_by_res_key: Dict[str, List[str]] = field(default_factory=dict)
    styles_by_key: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    elements_by_def: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    edges_by_src: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    edges_by_elem: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    shared_component_defs: Dict[str, int] = field(default_factory=dict)  # def_id -> ref_count (>=2)
    projected_surfaces: Dict[str, ProjectedSurface] = field(default_factory=dict)
    host_window_pools: Dict[str, ProjectedNode] = field(default_factory=dict)  # host_window_id -> composite Window root
    uid_shadow_issues: List[UidShadowIssue] = field(default_factory=list)
    identity_gap_issues: List[IdentityGapIssue] = field(default_factory=list)
    dynamic_unmounted_issues: List[DynamicUnmountedIssue] = field(default_factory=list)


@dataclass
class IdentityCountStats:
    """Two-dimensional objective instance counts (within-surface & global) for both `@auto_id` and `@name`/ChildText."""

    global_auto_id_instances: Dict[Tuple[str, str], int] = field(default_factory=dict)
    surface_auto_id_instances: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    global_name_instances: Dict[Tuple[str, str], int] = field(default_factory=dict)
    surface_name_instances: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    has_any_zh_text: bool = False


def get_effective_display_text(node: ProjectedNode) -> Tuple[str, bool]:
    """Return `(text, is_child_text)`: direct `@name` (EN or ZH-only) if present, else first static child `Text`."""
    if node.name_en:
        return node.name_en, False
    if node.name_zh:
        return node.name_zh, False
    for ch in node.children:
        if ch.uia_type == "Text" and (ch.name_en or ch.name_zh):
            return (ch.name_en or ch.name_zh), True
    return "", False


def compute_identity_counts(idx: WorkspaceIndex) -> IdentityCountStats:
    """Compute objective within-surface and workspace-wide instance counts for `(uia_type, auto_id)` and `(uia_type, display_text)`."""
    stats = IdentityCountStats()
    for sid, surf in idx.projected_surfaces.items():
        is_shared = surf.surface_kind == "SharedComponent"
        for node in surf.interactive_nodes:
            if node.name_zh:
                stats.has_any_zh_text = True
            if node.auto_id:
                skey = (sid, node.uia_type, node.auto_id)
                stats.surface_auto_id_instances[skey] = stats.surface_auto_id_instances.get(skey, 0) + 1
                if not is_shared:
                    gkey = (node.uia_type, node.auto_id)
                    stats.global_auto_id_instances[gkey] = stats.global_auto_id_instances.get(gkey, 0) + 1
            eff_text, _ = get_effective_display_text(node)
            if eff_text:
                nskey = (sid, node.uia_type, eff_text)
                stats.surface_name_instances[nskey] = stats.surface_name_instances.get(nskey, 0) + 1
                if not is_shared:
                    ngkey = (node.uia_type, eff_text)
                    stats.global_name_instances[ngkey] = stats.global_name_instances.get(ngkey, 0) + 1
    return stats


def compute_global_auto_id_counts(idx: WorkspaceIndex) -> Dict[Tuple[str, str], int]:
    """Return workspace-wide non-shared instance counts for each `(uia_type, auto_id)` pair."""
    return compute_identity_counts(idx).global_auto_id_instances


def _is_short_label_text(text: str, *, max_len: int = 64) -> bool:
    """Return True only for concise single-line static labels suitable as sibling anchors (not help paragraphs)."""
    if not text:
        return False
    if "\n" in text or "\r" in text:
        return False
    return len(text.strip()) <= max_len


def describe_uia_sibling_order(node: ProjectedNode, *, max_gap: int = 2) -> str:
    """Return objective nearby true UIA sibling ordering around `node` (bounded to <= `max_gap` intervening controls)."""
    if node.parent is None:
        return ""
    sibs = node.parent.children
    pos = node.sibling_index
    ut = node.uia_type

    low = max(0, pos - max_gap - 1)
    for i in range(pos - 1, low - 1, -1):
        s = sibs[i]
        if s.uia_type == ut:
            break
        if s.uia_type == "Text" and s.is_static_name and _is_short_label_text(s.name_en):
            gap = pos - i - 1
            if gap == 0:
                return f"Text('{s.name_en}') -> {ut}"
            return f"Text('{s.name_en}') ..(+{gap})..> {ut}"

    high = min(len(sibs) - 1, pos + max_gap + 1)
    for i in range(pos + 1, high + 1):
        s = sibs[i]
        if s.uia_type == ut:
            break
        if s.uia_type == "Text" and s.is_static_name and _is_short_label_text(s.name_en):
            gap = i - pos - 1
            if gap == 0:
                return f"{ut} -> Text('{s.name_en}')"
            return f"{ut} ..(+{gap})..> Text('{s.name_en}')"

    return ""


def describe_semantic_ancestors(
    node: ProjectedNode,
    is_weak_fn: Callable[[str], bool],
    *,
    max_depth: int = 3,
) -> str:
    """Return up to `max_depth` nearest real UIA ancestor containers carrying a semantic `@auto_id` or static `@name`."""
    chain: List[str] = []
    cur = node.parent
    while cur is not None and cur.parent is not None:
        if cur.auto_id_source == "SyntheticHost" or cur.uia_type == "Window":
            cur = cur.parent
            continue
        has_sem_id = bool(cur.auto_id and not is_weak_fn(cur.auto_id))
        has_name = bool(cur.is_static_name and cur.name_en)
        if has_sem_id and has_name:
            chain.append(f"{cur.uia_type}(@auto_id='{cur.auto_id}', @name='{cur.name_en}')")
        elif has_sem_id:
            chain.append(f"{cur.uia_type}(@auto_id='{cur.auto_id}')")
        elif has_name:
            chain.append(f"{cur.uia_type}(@name='{cur.name_en}')")
        if len(chain) >= max_depth:
            break
        cur = cur.parent
    if not chain:
        return ""
    chain.reverse()
    return " > ".join(chain)


def format_surface_host_fact(surface: ProjectedSurface) -> str:
    """Return an honest static host-window description without fabricating Window names for unmounted panels."""
    if surface.surface_kind == "SharedComponent":
        return "共享组件(随宿主复用)"
    if surface.surface_kind == "ModeSurface":
        host_lbl = surface.host_window_title or "CompositeHost"
        return f"多模式宿主:{host_lbl}(模式:{surface.mode_name},同宿主其他模式可能Collapsed驻留)"
    if surface.host_window_known and surface.host_window_title:
        if surface.host_window_auto_id:
            return f"{surface.host_window_title} (@auto_id='{surface.host_window_auto_id}')"
        return surface.host_window_title
    return "动态挂载(未静态绑定顶层窗口)"


def classify_surface_scope(surface: ProjectedSurface) -> str:
    """Describe the static mounting scope of a surface objectively."""
    if surface.surface_kind == "SharedComponent":
        return "共享组件(跨界面复用)"
    if surface.surface_kind == "ModeSurface":
        return f"多模式宿主({surface.mode_name},同宿主其他模式可能Collapsed驻留)"
    if surface.surface_kind == "ModalDialog":
        return "模态/子窗口"
    if surface.host_window_known:
        return "宿主顶层树"
    return "动态挂载面板(未静态绑定顶层窗口)"


def classify_auto_id_quality(
    auto_id: str,
    is_weak: bool,
    global_count: int,
    surface_count: int = 1,
) -> str:
    """Classify objective `@auto_id` quality using both within-surface and workspace-wide instance counts."""
    if not auto_id:
        return "Missing"
    if is_weak:
        return "Weak_Generated"
    if surface_count <= 1 and global_count <= 1:
        return "Semantic_Unique"
    return "Semantic_Repeated"


def is_special_action_contract(node: ProjectedNode) -> bool:
    """Return True only when the node has a non-obvious or error-prone UIA interaction contract worth printing in Markdown."""
    if node.topology in ("PopupTopLevel", "ContextMenuTopLevel", "EmbeddedWebHost"):
        return True
    if node.is_readonly:
        return True
    if node.uia_type in ("ComboBox", "DataGrid", "List", "Tree", "Document", "Spinner"):
        return True
    return False
