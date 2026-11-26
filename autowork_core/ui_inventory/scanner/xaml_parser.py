"""Line-accurate WPF XAML AST, ResourceDictionary, Style, and Template parser.

100% Python standard library (`xml.parsers.expat`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import xml.parsers.expat

from autowork_core.ui_inventory.scanner.markup_expr import (
    extract_binding_path as _extract_binding_path_ast,
    extract_resource_key_ref as _extract_resource_key_ref_ast,
)
from autowork_core.ui_inventory.scanner.resx_config_parser import (
    classify_locale_bucket,
    split_resx_stem_and_locale,
)
from autowork_core.ui_inventory.scanner.wpf_builtin_specs import (
    DEFAULT_XAML_XMLNS_MAP,
    PURE_RESOURCE_NON_UI_TAGS,
    WPF_BUILTIN_TYPE_SPECS,
    WPF_SHORT_NAME_TO_FULL,
    XAML_DIRECTIVE_URIS,
)


CLR_NS_RE = re.compile(r"^clr-namespace:([^;]+)(?:;assembly=([^;]+))?$")
X_TYPE_RE = re.compile(r"\{\s*x:Type\s+(?:TypeName\s*=\s*)?([^\s,}]+)\s*\}")
DESIGN_INSTANCE_RE = re.compile(r"\{\s*[A-Za-z_][\w]*:DesignInstance\s+(?:Type\s*=\s*)?([^\s,}]+)")

_BUILTIN_ATTACHED_OWNERS_LOWER = frozenset({
    "grid", "canvas", "dockpanel", "panel", "scrollviewer", "automationproperties",
    "keyboardnavigation", "focusmanager", "tooltipservice", "textoptions", "renderoptions",
    "virtualizingstackpanel", "virtualizingpanel", "selector", "validation", "typography",
    "dragdrop", "stylus", "touch", "inputmethod", "spellcheck", "contextmenuservice",
    "numberSubstitution".lower(), "textsearch", "frameworkelement", "uielement",
})
_ATTACHED_PROBE_INCLUDE_TOKENS = (
    "text", "title", "header", "caption", "label", "content", "loc", "lang", "i18n", "res",
)
_ATTACHED_PROBE_REJECT_SUFFIXES = (
    "style", "template", "alignment", "direction", "mode", "size", "width", "height",
    "color", "brush", "icon", "image", "source", "visibility", "enabled", "command",
    "persistancekey", "persistencekey", "regionname", "groupname", "elementname", "converter",
)


@dataclass
class _XmlNode:
    raw_tag: str
    prefix: str
    local_name: str
    ns_uri: str
    ns_scope: Dict[str, str]
    attrs: Dict[str, str]
    line: int
    text_parts: List[str] = field(default_factory=list)
    children: List["_XmlNode"] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.text_parts).strip()


@dataclass
class XamlParseResult:
    is_pure_icon_or_brush_dict: bool = False
    definitions: List[Dict[str, Any]] = field(default_factory=list)
    elements: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    strings: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)


def _parse_xml_tree(xml_text: str) -> _XmlNode:
    ns_stack: List[Dict[str, str]] = [{}]
    node_stack: List[_XmlNode] = []
    root_holder: List[_XmlNode] = []

    parser = xml.parsers.expat.ParserCreate(namespace_separator=None)
    parser.buffer_text = True

    def on_start(name: str, attrs: Dict[str, str]) -> None:
        cur_ns = dict(ns_stack[-1])
        clean_attrs: Dict[str, str] = {}
        for k, v in attrs.items():
            if k == "xmlns":
                cur_ns[""] = v
            elif k.startswith("xmlns:"):
                cur_ns[k[6:]] = v
            else:
                clean_attrs[k] = v
        ns_stack.append(cur_ns)

        if ":" in name:
            pfx, lname = name.split(":", 1)
        else:
            pfx, lname = "", name
        uri = cur_ns.get(pfx, "")
        node = _XmlNode(
            raw_tag=name,
            prefix=pfx,
            local_name=lname,
            ns_uri=uri,
            ns_scope=cur_ns,
            attrs=clean_attrs,
            line=parser.CurrentLineNumber,
        )
        if node_stack:
            node_stack[-1].children.append(node)
        else:
            root_holder.append(node)
        node_stack.append(node)

    def on_end(_name: str) -> None:
        if node_stack:
            node_stack.pop()
        if len(ns_stack) > 1:
            ns_stack.pop()

    def on_char(data: str) -> None:
        if node_stack and data:
            node_stack[-1].text_parts.append(data)

    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    parser.CharacterDataHandler = on_char
    parser.Parse(xml_text, True)

    if not root_holder:
        raise ValueError("Empty XAML document")
    return root_holder[0]


def _resolve_clr_type(prefix: str, local_name: str, ns_scope: Dict[str, str], default_clr_ns: str) -> str:
    uri = ns_scope.get(prefix, "")
    if not uri:
        return f"{default_clr_ns}.{local_name}" if default_clr_ns else local_name
    m = CLR_NS_RE.match(uri)
    if m:
        clr_ns = m.group(1).strip()
        return f"{clr_ns}.{local_name}" if clr_ns else local_name
    if uri in DEFAULT_XAML_XMLNS_MAP:
        return WPF_SHORT_NAME_TO_FULL.get(local_name, f"System.Windows.Controls.{local_name}")
    if uri in XAML_DIRECTIVE_URIS:
        return f"x:{local_name}"
    return f"{uri}::{local_name}"


def _resolve_x_type_expr(raw_val: str, ns_scope: Dict[str, str], default_clr_ns: str) -> str:
    val = raw_val.strip()
    m = X_TYPE_RE.search(val)
    token = m.group(1) if m else val
    if ":" in token:
        pfx, lname = token.split(":", 1)
    else:
        pfx, lname = "", token
    return _resolve_clr_type(pfx, lname, ns_scope, default_clr_ns)


def _extract_resource_key_ref(expr: str) -> Optional[str]:
    if not expr:
        return None
    key_raw = _extract_resource_key_ref_ast(expr)
    if key_raw:
        return key_raw
    return None


def _extract_binding_path(expr: str) -> str:
    """Extract only the actual data-binding Path from `{Binding ...}` (ignoring `Source=`/`ElementName=`/`Mode=` etc.)."""
    return _extract_binding_path_ast(expr)


def _extract_attached_probes(attrs: Dict[str, str]) -> List[str]:
    """Losslessly collect non-builtin attached attribute values with text/localization semantics for closed-table verification."""
    probes: List[str] = []
    for ak, av in attrs.items():
        if "." not in ak:
            continue
        val = (av or "").strip()
        if not val or "{" in val or len(val) > 96 or "\n" in val or "\r" in val or "://" in val or "\\\\" in val:
            continue
        val_low = val.lower()
        if val_low in ("true", "false", "null", "auto", "visible", "collapsed", "hidden", "none"):
            continue
        if val.replace(".", "", 1).replace("-", "", 1).isdigit():
            continue
        if " " in val and len(val) > 32:
            continue
        # Strip XML namespace prefix on owner (`vm:XamlResourceHub.LanguageTextForLabel` -> `XamlResourceHub`, `LanguageTextForLabel`)
        owner_raw, prop_short = ak.rsplit(".", 1)
        owner_short = owner_raw.rsplit(":", 1)[-1]
        owner_low = owner_short.lower()
        prop_low = prop_short.lower()
        if owner_low in _BUILTIN_ATTACHED_OWNERS_LOWER:
            continue
        if prop_low.endswith(_ATTACHED_PROBE_REJECT_SUFFIXES):
            continue
        has_semantic = any(tok in prop_low for tok in _ATTACHED_PROBE_INCLUDE_TOKENS) or any(
            tok in owner_low for tok in ("loc", "lang", "i18n", "res", "text", "string")
        )
        if not has_semantic:
            continue
        if val not in probes:
            probes.append(val)
            if len(probes) >= 4:
                break
    return probes


def _get_attr_by_suffix(attrs: Dict[str, str], suffixes: Tuple[str, ...]) -> str:
    for sfx in suffixes:
        if sfx in attrs:
            return attrs[sfx]
    for k, v in attrs.items():
        short_k = k.rsplit(":", 1)[-1]
        if short_k in suffixes:
            return v
    return ""


def _is_pure_icon_or_brush_dictionary(root: _XmlNode) -> bool:
    if root.local_name != "ResourceDictionary" or not root.children:
        return False

    def check(node: _XmlNode) -> bool:
        lname = node.local_name
        if lname.endswith(".MergedDictionaries"):
            return False
        if lname == "ResourceDictionary":
            if node.attrs.get("Source"):
                return False
            return all(check(ch) for ch in node.children)
        if "." in lname:
            return all(check(ch) for ch in node.children)
        if lname in ("Style", "ControlTemplate", "DataTemplate", "HierarchicalDataTemplate", "ItemsPanelTemplate"):
            return False
        if lname not in PURE_RESOURCE_NON_UI_TAGS:
            return False
        return all(check(ch) for ch in node.children)

    return all(check(ch) for ch in root.children)


_NON_VISUAL_HELPER_TAGS = frozenset({
    "RowDefinition",
    "ColumnDefinition",
    "Setter",
    "Trigger",
    "DataTrigger",
    "MultiTrigger",
    "MultiDataTrigger",
    "EventTrigger",
    "Condition",
    "Storyboard",
    "BeginStoryboard",
    "DoubleAnimation",
    "ColorAnimation",
    "ObjectDataProvider",
    "Binding",
    "MultiBinding",
    "LogicCommandTargetCollection",
    "BehaviorBindingCollection",
    "BehaviorBinding",
    "BroadcastBehavior",
    "LogicBehavior",
})


def parse_xaml_file(
    xml_text: str,
    rel_path: str,
    assembly_name: str,
    default_clr_ns: str,
    is_shipping: bool,
) -> XamlParseResult:
    res = XamlParseResult()
    try:
        root = _parse_xml_tree(xml_text)
    except Exception as exc:
        res.diagnostics.append({
            "code": "XAML_PARSE_ERROR",
            "severity": "Warn",
            "file": rel_path,
            "line": 1,
            "message": f"{type(exc).__name__}: {exc}",
        })
        return res

    if _is_pure_icon_or_brush_dictionary(root):
        res.is_pure_icon_or_brush_dict = True
        return res

    elem_seq = 0
    sub_def_seq = 0

    def next_elem_id(def_id: str) -> str:
        nonlocal elem_seq
        elem_seq += 1
        return f"{def_id}::e{elem_seq}"

    def next_sub_def_id(prefix_tag: str) -> str:
        nonlocal sub_def_seq
        sub_def_seq += 1
        return f"{assembly_name}::{rel_path}::{prefix_tag}_{sub_def_seq}"

    def process_resources_container(res_node: _XmlNode, owner_def_id: str) -> None:
        for ch in res_node.children:
            lname = ch.local_name
            if lname.endswith(".MergedDictionaries"):
                for md in ch.children:
                    if md.local_name == "ResourceDictionary":
                        src = md.attrs.get("Source", "").strip()
                        if src:
                            res.edges.append({
                                "src_def_id": owner_def_id,
                                "src_elem_id": None,
                                "edge_kind": "MERGED_DICT",
                                "dst_type_or_key": src,
                                "extra": {"line": md.line},
                            })
                continue
            if lname == "ResourceDictionary":
                src = ch.attrs.get("Source", "").strip()
                if src:
                    res.edges.append({
                        "src_def_id": owner_def_id,
                        "src_elem_id": None,
                        "edge_kind": "MERGED_DICT",
                        "dst_type_or_key": src,
                        "extra": {"line": ch.line},
                    })
                process_resources_container(ch, owner_def_id)
                continue

            if lname == "String":
                s_key = _get_attr_by_suffix(ch.attrs, ("Key",))
                if s_key and ch.text:
                    _, x_locale = split_resx_stem_and_locale(Path(rel_path).stem)
                    loc_bucket = classify_locale_bucket(x_locale, rel_path)
                    if loc_bucket in ("en", "zh"):
                        res.strings.append({
                            "assembly": assembly_name,
                            "res_class": "$ResourceDictionary",
                            "key": s_key,
                            "val_en": ch.text if loc_bucket == "en" else "",
                            "val_zh": ch.text if loc_bucket == "zh" else "",
                        })
                continue

            if lname == "Style":
                _process_style_node(ch)
                continue

            if lname in ("ControlTemplate", "DataTemplate", "HierarchicalDataTemplate"):
                _process_template_def_node(ch, explicit_target="", fallback_key="")
                continue

    def _process_style_node(style_node: _XmlNode) -> Optional[str]:
        raw_target = style_node.attrs.get("TargetType", "")
        target_clr = _resolve_x_type_expr(raw_target, style_node.ns_scope, default_clr_ns) if raw_target else ""
        style_key = _get_attr_by_suffix(style_node.attrs, ("Key",)) or target_clr
        based_on_raw = style_node.attrs.get("BasedOn", "")
        based_on_key = _extract_resource_key_ref(based_on_raw) or ""

        style_def_id = next_sub_def_id("Style")
        has_grid_view_in_style = False
        setter_props: Dict[str, str] = {}

        for ch in style_node.children:
            if ch.local_name.endswith(".Resources"):
                process_resources_container(ch, style_def_id)
            elif ch.local_name.endswith(".Setters"):
                for st in ch.children:
                    if st.local_name == "Setter":
                        _inspect_setter(st, style_def_id, target_clr, style_key, setter_props)
                        if _contains_tag(st, "GridView"):
                            has_grid_view_in_style = True
            elif ch.local_name == "Setter":
                _inspect_setter(ch, style_def_id, target_clr, style_key, setter_props)
                if _contains_tag(ch, "GridView"):
                    has_grid_view_in_style = True

        res.definitions.append({
            "def_id": style_def_id,
            "kind": "Style",
            "framework": "WPF",
            "file": rel_path,
            "full_class_name": "",
            "target_type": target_clr,
            "res_key": style_key,
            "based_on": based_on_key,
            "has_grid_view": has_grid_view_in_style,
            "setter_props": setter_props,
            "is_shipping": is_shipping,
        })
        return style_def_id

    def _inspect_setter(
        setter_node: _XmlNode,
        style_def_id: str,
        target_clr: str,
        style_key: str,
        setter_props: Dict[str, str],
    ) -> None:
        prop_name = setter_node.attrs.get("Property", "").rsplit(".", 1)[-1]
        val_attr = setter_node.attrs.get("Value", "")
        if prop_name and val_attr:
            setter_props[prop_name] = val_attr
        for sub in setter_node.children:
            candidates = sub.children if sub.local_name.endswith(".Value") else [sub]
            for cand in candidates:
                if cand.local_name in ("ControlTemplate", "DataTemplate", "HierarchicalDataTemplate"):
                    tpl_def_id = _process_template_def_node(cand, explicit_target=target_clr, fallback_key=style_key)
                    res.edges.append({
                        "src_def_id": style_def_id,
                        "src_elem_id": None,
                        "edge_kind": "APPLIES_TEMPLATE",
                        "dst_type_or_key": tpl_def_id,
                        "extra": {"property": prop_name or "Template", "line": cand.line},
                    })

    def _process_template_def_node(
        tpl_node: _XmlNode,
        explicit_target: str,
        fallback_key: str,
    ) -> str:
        lname = tpl_node.local_name
        kind = "ControlTemplate" if lname == "ControlTemplate" else "DataTemplate"
        raw_t = tpl_node.attrs.get("TargetType") or tpl_node.attrs.get("DataType") or ""
        target_clr = _resolve_x_type_expr(raw_t, tpl_node.ns_scope, default_clr_ns) if raw_t else explicit_target
        res_key = _get_attr_by_suffix(tpl_node.attrs, ("Key",)) or fallback_key or target_clr
        tpl_def_id = next_sub_def_id(kind)
        res.definitions.append({
            "def_id": tpl_def_id,
            "kind": kind,
            "framework": "WPF",
            "file": rel_path,
            "full_class_name": "",
            "target_type": target_clr,
            "res_key": res_key,
            "is_shipping": is_shipping,
        })
        order = 0
        for ch in tpl_node.children:
            if ch.local_name.endswith(".Resources"):
                process_resources_container(ch, tpl_def_id)
                continue
            if ch.local_name.endswith(".Triggers"):
                continue
            # Record `<DataTemplate DataType="{x:Type vm:FooViewModel}"><view:FooView/></DataTemplate>` mapping
            if kind == "DataTemplate" and target_clr and "." not in ch.local_name and ch.local_name not in _NON_VISUAL_HELPER_TAGS:
                ch_clr = _resolve_clr_type(ch.prefix, ch.local_name, ch.ns_scope, default_clr_ns)
                if ch_clr and ch_clr not in WPF_BUILTIN_TYPE_SPECS and not ch_clr.startswith("x:"):
                    res.edges.append({
                        "src_def_id": tpl_def_id,
                        "src_elem_id": target_clr,
                        "edge_kind": "VM_VIEW_MAP",
                        "dst_type_or_key": ch_clr,
                        "extra": {"line": ch.line},
                    })
            emitted = visit_visual_node(ch, tpl_def_id, None, order, kind)
            order += emitted
        return tpl_def_id

    def visit_visual_node(
        node: _XmlNode,
        def_id: str,
        parent_elem_id: Optional[str],
        sibling_order: int,
        tree_context: str,
    ) -> int:
        lname = node.local_name
        if "." in lname or lname in _NON_VISUAL_HELPER_TAGS or node.ns_uri in XAML_DIRECTIVE_URIS:
            return 0

        clr_type = _resolve_clr_type(node.prefix, lname, node.ns_scope, default_clr_ns)
        in_template = tree_context in ("ControlTemplate", "DataTemplate", "HeaderTemplate")

        cur_context = tree_context
        if lname == "Popup" or clr_type == "System.Windows.Controls.Primitives.Popup":
            cur_context = "PopupSubtree"
        elif lname == "ContextMenu" or clr_type == "System.Windows.Controls.ContextMenu":
            cur_context = "ContextMenuSubtree"

        elem_id = next_elem_id(def_id)
        attrs = node.attrs

        explicit_auto_id = _get_attr_by_suffix(attrs, ("AutomationProperties.AutomationId", "AutomationId"))
        raw_x_uid = "" if in_template else _get_attr_by_suffix(attrs, ("Uid",))
        raw_x_name = _get_attr_by_suffix(attrs, ("Name",))
        auto_name = _get_attr_by_suffix(attrs, ("AutomationProperties.Name",))

        # Lossless per-property text facts (UIA AutomationPeer projection rules decide `@name` in linker!)
        text_props: Dict[str, str] = {}
        if auto_name:
            text_props["auto_name"] = auto_name
        if attrs.get("Content"):
            text_props["content"] = attrs["Content"]
        hdr_val = attrs.get("Header") or _find_attached_header(attrs)
        if hdr_val:
            text_props["header"] = hdr_val
        if attrs.get("Text"):
            text_props["text"] = attrs["Text"]
        if attrs.get("Title"):
            text_props["title"] = attrs["Title"]
        if node.text and "content" not in text_props and "text" not in text_props:
            if lname in ("TextBlock", "AccessText", "Run", "Hyperlink", "Span", "Bold", "Italic"):
                text_props["text"] = node.text
            else:
                text_props["content"] = node.text

        tooltip_expr = (
            _get_attr_by_suffix(attrs, ("AutomationProperties.HelpText", "ToolTip"))
        )
        command_expr = attrs.get("Command", "")
        vis_expr = attrs.get("Visibility", "")
        is_editable = attrs.get("IsEditable", "")
        is_readonly = attrs.get("IsReadOnly", "")

        # Extract primary data/text binding path (prefer value/selection/checked bindings, fall back to Content/Header/Title)
        binding_path = ""
        for b_attr in ("Value", "Text", "SelectedItem", "SelectedValue", "IsChecked", "ItemsSource", "Content", "Header", "Title"):
            b_val = attrs.get(b_attr, "")
            if b_val and "Binding" in b_val:
                binding_path = _extract_binding_path(b_val)
                if binding_path:
                    break

        # Detect Prism/MEF RegionName attached property on container slots
        region_name = ""
        for ak, av in attrs.items():
            if (ak == "RegionName" or ak.endswith(".RegionName")) and av and not av.strip().startswith("{"):
                region_name = av.strip()
                break

        # Detect root-level design-time DataContext (`d:DataContext="{d:DesignInstance Type=vm:FooVM}"`)
        if parent_elem_id is None and not in_template:
            dc_attr = _get_attr_by_suffix(attrs, ("DataContext",))
            if dc_attr:
                m_di = DESIGN_INSTANCE_RE.search(dc_attr)
                if m_di:
                    vm_clr = _resolve_x_type_expr(m_di.group(1), node.ns_scope, default_clr_ns)
                    if vm_clr and vm_clr not in WPF_BUILTIN_TYPE_SPECS:
                        res.edges.append({
                            "src_def_id": def_id,
                            "src_elem_id": None,
                            "edge_kind": "VIEW_VM_LINK",
                            "dst_type_or_key": vm_clr,
                            "extra": {"source": "d:DataContext", "line": node.line},
                        })

        grid_r_str = _get_attr_by_suffix(attrs, ("Grid.Row",))
        grid_c_str = _get_attr_by_suffix(attrs, ("Grid.Column",))
        grid_r = int(grid_r_str) if (grid_r_str.isascii() and grid_r_str.isdigit()) else None
        grid_c = int(grid_c_str) if (grid_c_str.isascii() and grid_c_str.isdigit()) else None

        has_grid_view = False
        child_visual_nodes: List[Tuple[_XmlNode, str]] = []
        synthetic_children: List[_XmlNode] = []

        # Inspect property elements & direct children
        for ch in node.children:
            ch_lname = ch.local_name
            if ch_lname.endswith(".Resources"):
                process_resources_container(ch, def_id)
                continue
            if "." in ch_lname:
                prop_part = ch_lname.rsplit(".", 1)[-1]
                if prop_part == "View" and _contains_tag(ch, "GridView"):
                    has_grid_view = True
                elif prop_part in ("Header", "Content"):
                    pkey = prop_part.lower()
                    if pkey not in text_props and ch.text:
                        text_props[pkey] = ch.text
                    # Single direct TextBlock inside Header/Content populates GetPlainText() in WPF!
                    elif (
                        pkey not in text_props
                        and len(ch.children) == 1
                        and ch.children[0].local_name in ("TextBlock", "AccessText", "Run")
                    ):
                        tb = ch.children[0]
                        tb_val = tb.attrs.get("Text") or tb.text
                        if tb_val:
                            text_props[pkey] = tb_val
                    for sub in ch.children:
                        child_visual_nodes.append((sub, cur_context))
                elif prop_part == "ContextMenu":
                    for sub in ch.children:
                        child_visual_nodes.append((sub, "ContextMenuSubtree"))
                elif prop_part in ("ToolTip", "HelpContent"):
                    if prop_part == "ToolTip" and not tooltip_expr:
                        tooltip_expr = _extract_first_text(ch)
                    for sub in ch.children:
                        child_visual_nodes.append((sub, "PopupSubtree"))
                elif prop_part == "Style":
                    for sub in ch.children:
                        if sub.local_name == "Style":
                            st_id = _process_style_node(sub)
                            if st_id:
                                res.edges.append({
                                    "src_def_id": def_id,
                                    "src_elem_id": elem_id,
                                    "edge_kind": "APPLIES_TEMPLATE",
                                    "dst_type_or_key": st_id,
                                    "extra": {"line": sub.line},
                                })
                elif prop_part == "DataContext" and parent_elem_id is None and not in_template:
                    for sub in ch.children:
                        if "." not in sub.local_name and sub.local_name not in _NON_VISUAL_HELPER_TAGS:
                            vm_clr = _resolve_clr_type(sub.prefix, sub.local_name, sub.ns_scope, default_clr_ns)
                            if vm_clr and vm_clr not in WPF_BUILTIN_TYPE_SPECS:
                                res.edges.append({
                                    "src_def_id": def_id,
                                    "src_elem_id": None,
                                    "edge_kind": "VIEW_VM_LINK",
                                    "dst_type_or_key": vm_clr,
                                    "extra": {"source": "DataContextElement", "line": sub.line},
                                })
                elif prop_part in ("Template", "ItemTemplate", "ContentTemplate", "HeaderTemplate"):
                    edge_kind = "APPLIES_TEMPLATE" if prop_part == "Template" else "DATA_TEMPLATE"
                    sub_ctx = "HeaderTemplate" if prop_part == "HeaderTemplate" else (
                        "ControlTemplate" if prop_part == "Template" else "DataTemplate"
                    )
                    for sub in ch.children:
                        if sub.local_name in ("ControlTemplate", "DataTemplate", "HierarchicalDataTemplate"):
                            tpl_id = _process_template_def_node(sub, explicit_target=clr_type, fallback_key="")
                            res.edges.append({
                                "src_def_id": def_id,
                                "src_elem_id": elem_id,
                                "edge_kind": edge_kind,
                                "dst_type_or_key": tpl_id,
                                "extra": {"property": prop_part, "context": sub_ctx, "line": sub.line},
                            })
                elif prop_part == "Columns":
                    # DataGrid.Columns -> synthesize HeaderItem nodes in exact column order
                    for col_node in ch.children:
                        if "Column" in col_node.local_name:
                            synthetic_children.append(_make_datagrid_column_header_node(col_node))
                continue

            # Iron Law 4: CT BroadcastCommand child element injects CommandContent (ToolTip / HelpText)
            if ch_lname == "BroadcastCommand":
                cmd_content = ch.attrs.get("CommandContent", "")
                if cmd_content and not command_expr:
                    command_expr = cmd_content
                if cmd_content and not tooltip_expr:
                    tooltip_expr = cmd_content
                continue

            child_visual_nodes.append((ch, cur_context))

        # Iron Law 4: SplitButton inherits default first SplitButtonItem's ToolTip if unset
        if not tooltip_expr and lname in ("SplitButton", "SplitRadioButton", "SplitToggleButton"):
            for ch_node, _ in child_visual_nodes:
                if ch_node.local_name in ("SplitButtonItem", "SplitButtonMenuItem"):
                    first_tip = ch_node.attrs.get("ToolTip", "")
                    if first_tip:
                        tooltip_expr = first_tip
                    break

        # Single direct TextBlock child of HeaderedContentControl/Button?
        # Note: <Button><TextBlock Text="X"/></Button> DOES set Button.GetPlainText() = "X",
        # whereas <Button><Grid><TextBlock Text="X"/></Grid></Button> leaves Button.@name = ""!
        if (
            "content" not in text_props
            and "header" not in text_props
            and len(child_visual_nodes) == 1
            and child_visual_nodes[0][0].local_name in ("TextBlock", "AccessText")
            and lname in ("Button", "ToggleButton", "RepeatButton", "CheckBox", "RadioButton", "Label", "TabItem")
        ):
            tb0 = child_visual_nodes[0][0]
            tb0_val = tb0.attrs.get("Text") or tb0.text
            if tb0_val:
                text_props["header" if lname == "TabItem" else "content"] = tb0_val

        # Compatibility summary field (linker prefers lossless `text_props` with UIA-spec projection)
        text_expr = (
            text_props.get("auto_name")
            or text_props.get("header")
            or text_props.get("content")
            or text_props.get("text")
            or text_props.get("title")
            or ""
        )

        # Style / Template / ItemTemplate attribute references
        for attr_prop, edge_k in (
            ("Style", "APPLIES_TEMPLATE"),
            ("Template", "APPLIES_TEMPLATE"),
            ("ItemTemplate", "DATA_TEMPLATE"),
            ("ContentTemplate", "DATA_TEMPLATE"),
        ):
            ref_key = _extract_resource_key_ref(attrs.get(attr_prop, ""))
            if ref_key:
                res.edges.append({
                    "src_def_id": def_id,
                    "src_elem_id": elem_id,
                    "edge_kind": edge_k,
                    "dst_type_or_key": ref_key,
                    "extra": {"property": attr_prop, "line": node.line},
                })

        # Dynamic Content="{Binding ...}" on container controls (ContentControl / UserControl / Frame)
        raw_content_attr = attrs.get("Content", "")
        if "Binding" in raw_content_attr and lname in ("ContentControl", "UserControl", "Frame", "HeaderedContentControl"):
            res.edges.append({
                "src_def_id": def_id,
                "src_elem_id": elem_id,
                "edge_kind": "DYNAMIC_SLOT",
                "dst_type_or_key": _extract_binding_path(raw_content_attr),
                "extra": {"line": node.line},
            })

        # Custom control or embedded UserControl edge
        if clr_type not in WPF_BUILTIN_TYPE_SPECS and not clr_type.startswith("x:"):
            res.edges.append({
                "src_def_id": def_id,
                "src_elem_id": elem_id,
                "edge_kind": "XAML_EMBED",
                "dst_type_or_key": clr_type,
                "extra": {"line": node.line},
            })

        elem_attrs: Dict[str, Any] = {}
        if explicit_auto_id:
            elem_attrs["explicit_auto_id"] = explicit_auto_id
        if raw_x_uid:
            elem_attrs["x_uid"] = raw_x_uid
        if raw_x_name:
            elem_attrs["x_name"] = raw_x_name
        if text_props:
            elem_attrs["text_props"] = text_props
        if text_expr:
            elem_attrs["raw_text_expr"] = text_expr
        attached_probes = _extract_attached_probes(attrs)
        if attached_probes:
            elem_attrs["attached_probes"] = attached_probes
        if region_name:
            elem_attrs["region_name"] = region_name
        if tooltip_expr:
            elem_attrs["raw_tooltip_expr"] = tooltip_expr
        if is_editable:
            elem_attrs["is_editable"] = is_editable
        if is_readonly:
            elem_attrs["is_readonly"] = is_readonly
        if vis_expr:
            elem_attrs["initial_vis"] = vis_expr
        if binding_path:
            elem_attrs["binding_path"] = binding_path
        if command_expr:
            elem_attrs["command_expr"] = command_expr
        if has_grid_view:
            elem_attrs["has_grid_view"] = True

        res.elements.append({
            "elem_id": elem_id,
            "def_id": def_id,
            "parent_elem_id": parent_elem_id,
            "xml_order": sibling_order,
            "context": cur_context,
            "clr_type": clr_type,
            "attrs": elem_attrs,
            "grid_rc": [grid_r, grid_c],
            "line": node.line,
        })

        next_order = 0
        for syn in synthetic_children:
            next_order += visit_visual_node(syn, def_id, elem_id, next_order, cur_context)
        for ch_node, ch_ctx in child_visual_nodes:
            next_order += visit_visual_node(ch_node, def_id, elem_id, next_order, ch_ctx)

        return 1

    # Top-level dispatch
    if root.local_name == "ResourceDictionary":
        rd_def_id = f"{assembly_name}::resdict::{rel_path}"
        res.definitions.append({
            "def_id": rd_def_id,
            "kind": "ResourceDictionary",
            "framework": "WPF",
            "file": rel_path,
            "full_class_name": "",
            "target_type": "",
            "res_key": rel_path,
            "is_shipping": is_shipping,
        })
        process_resources_container(root, rd_def_id)
        return res

    if root.local_name == "Application":
        app_class = _get_attr_by_suffix(root.attrs, ("Class",)) or f"{default_clr_ns}.App"
        app_def_id = f"{assembly_name}::{app_class}"
        res.definitions.append({
            "def_id": app_def_id,
            "kind": "Application",
            "framework": "WPF",
            "file": rel_path,
            "full_class_name": app_class,
            "target_type": "",
            "res_key": root.attrs.get("StartupUri", ""),
            "is_shipping": is_shipping,
        })
        for ch in root.children:
            if ch.local_name.endswith(".Resources"):
                process_resources_container(ch, app_def_id)
        return res

    # UI Root: Window / Dialog / PopupWindow / UserControl / Page / Custom Root
    x_class = _get_attr_by_suffix(root.attrs, ("Class",))
    root_clr_type = _resolve_clr_type(root.prefix, root.local_name, root.ns_scope, default_clr_ns)
    def_key = x_class or f"{default_clr_ns}.{rel_path}"
    main_def_id = f"{assembly_name}::{def_key}"

    root_kind = "UserControl"
    if root.local_name in ("Window", "NavigationWindow") or "Dialog" in root.local_name or "Window" in root.local_name:
        root_kind = "Window"
    elif root.local_name == "Page":
        root_kind = "Page"

    res.definitions.append({
        "def_id": main_def_id,
        "kind": root_kind,
        "framework": "WPF",
        "file": rel_path,
        "full_class_name": x_class,
        "base_clr_type": root_clr_type,
        "target_type": "",
        "res_key": "",
        "is_shipping": is_shipping,
    })

    visit_visual_node(root, main_def_id, None, 0, "VisualTree")
    return res


def _contains_tag(node: _XmlNode, local_name: str) -> bool:
    if node.local_name == local_name:
        return True
    return any(_contains_tag(ch, local_name) for ch in node.children)


def _find_attached_header(attrs: Dict[str, str]) -> str:
    for k, v in attrs.items():
        if k.endswith(".Header") and v:
            return v
    return ""


def _extract_first_text(node: _XmlNode) -> str:
    if node.text:
        return node.text
    for k in ("Text", "Content", "Header"):
        if node.attrs.get(k):
            return node.attrs[k]
    for ch in node.children:
        found = _extract_first_text(ch)
        if found:
            return found
    return ""


def _make_datagrid_column_header_node(col_node: _XmlNode) -> _XmlNode:
    hdr_attrs: Dict[str, str] = {}
    if "Header" in col_node.attrs:
        hdr_attrs["Header"] = col_node.attrs["Header"]
    hdr_children: List[_XmlNode] = []
    for ch in col_node.children:
        if ch.local_name.endswith(".Header"):
            prop_wrapper = _XmlNode(
                raw_tag="DataGridColumnHeader.Header",
                prefix="",
                local_name="DataGridColumnHeader.Header",
                ns_uri=col_node.ns_uri,
                ns_scope=col_node.ns_scope,
                attrs={},
                line=ch.line,
                text_parts=list(ch.text_parts),
                children=list(ch.children),
            )
            hdr_children.append(prop_wrapper)
        elif ch.local_name.endswith(".HeaderTemplate"):
            prop_wrapper = _XmlNode(
                raw_tag="DataGridColumnHeader.HeaderTemplate",
                prefix="",
                local_name="DataGridColumnHeader.HeaderTemplate",
                ns_uri=col_node.ns_uri,
                ns_scope=col_node.ns_scope,
                attrs={},
                line=ch.line,
                children=list(ch.children),
            )
            hdr_children.append(prop_wrapper)
    return _XmlNode(
        raw_tag="DataGridColumnHeader",
        prefix="",
        local_name="DataGridColumnHeader",
        ns_uri="http://schemas.microsoft.com/winfx/2006/xaml/presentation",
        ns_scope=col_node.ns_scope,
        attrs=hdr_attrs,
        line=col_node.line,
        children=hdr_children,
    )
