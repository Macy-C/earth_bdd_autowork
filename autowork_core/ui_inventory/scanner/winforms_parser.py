"""WinForms `.Designer.cs` static AST parser (`InitializeComponent`).

100% Python standard library. Preserves HWND-backed Panel/SplitContainer/TabPage hierarchy
and exact `Controls.Add` / `Controls.AddRange` sibling order (`xml_order`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional

from autowork_core.ui_inventory.scanner.resx_config_parser import unescape_csharp_string


NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)", re.MULTILINE)
CLASS_DECL_RE = re.compile(
    r"\bpartial\s+class\s+([A-Za-z_]\w*)(?:\s*:\s*([A-Za-z_][\w.]*))?"
)
FIELD_DECL_RE = re.compile(
    r"^\s*(?:private|protected|internal|public)\s+([A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*;",
    re.MULTILINE,
)
NEW_ASSIGN_RE = re.compile(
    r"^\s*(?:this\.)?([A-Za-z_]\w*)\s*=\s*new\s+([A-Za-z_][\w.]*)\s*\(\s*\)\s*;",
    re.MULTILINE,
)
PROP_STR_RE = re.compile(
    r'^\s*(?:this\.)?([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*;',
    re.MULTILINE,
)
PROP_RES_RE = re.compile(
    r"^\s*(?:this\.)?([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*(?:resources\.GetString\(\s*\"([^\"]+)\"\s*\)|([A-Za-z_][\w.]*\.[\w]+))\s*;",
    re.MULTILINE,
)
PROP_BOOL_INT_RE = re.compile(
    r"^\s*(?:this\.)?([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*(true|false|\d+)\s*;",
    re.MULTILINE,
)
ROOT_PROP_STR_RE = re.compile(
    r'^\s*this\.([A-Za-z_]\w*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*;',
    re.MULTILINE,
)
CONTROLS_ADD_RE = re.compile(
    r"^\s*(?:this\.)?(?:([A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*\.)?Controls\.Add\s*\(\s*(?:this\.)?([A-Za-z_]\w*)(?:\s*,[^;)]*)?\)\s*;",
)
CONTROLS_ADDRANGE_RE = re.compile(
    r"(?:this\.)?(?:([A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*\.)?Controls\.AddRange\s*\(\s*new\s+[^\{]+\{([^\}]*)\}\s*\)\s*;",
    re.DOTALL,
)


@dataclass
class WinFormsParseResult:
    definitions: List[Dict[str, Any]] = field(default_factory=list)
    elements: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)


_WINFORMS_SHORT_TO_FULL = {
    "Form": "System.Windows.Forms.Form",
    "UserControl": "System.Windows.Forms.UserControl",
    "Panel": "System.Windows.Forms.Panel",
    "FlowLayoutPanel": "System.Windows.Forms.FlowLayoutPanel",
    "TableLayoutPanel": "System.Windows.Forms.TableLayoutPanel",
    "SplitContainer": "System.Windows.Forms.SplitContainer",
    "GroupBox": "System.Windows.Forms.GroupBox",
    "TabControl": "System.Windows.Forms.TabControl",
    "TabPage": "System.Windows.Forms.TabPage",
    "Button": "System.Windows.Forms.Button",
    "CheckBox": "System.Windows.Forms.CheckBox",
    "RadioButton": "System.Windows.Forms.RadioButton",
    "Label": "System.Windows.Forms.Label",
    "LinkLabel": "System.Windows.Forms.LinkLabel",
    "TextBox": "System.Windows.Forms.TextBox",
    "MaskedTextBox": "System.Windows.Forms.MaskedTextBox",
    "RichTextBox": "System.Windows.Forms.RichTextBox",
    "ComboBox": "System.Windows.Forms.ComboBox",
    "ListBox": "System.Windows.Forms.ListBox",
    "CheckedListBox": "System.Windows.Forms.CheckedListBox",
    "ListView": "System.Windows.Forms.ListView",
    "DataGridView": "System.Windows.Forms.DataGridView",
    "TreeView": "System.Windows.Forms.TreeView",
    "NumericUpDown": "System.Windows.Forms.NumericUpDown",
    "DateTimePicker": "System.Windows.Forms.DateTimePicker",
    "TrackBar": "System.Windows.Forms.TrackBar",
    "ProgressBar": "System.Windows.Forms.ProgressBar",
    "PictureBox": "System.Windows.Forms.PictureBox",
    "ToolStrip": "System.Windows.Forms.ToolStrip",
    "MenuStrip": "System.Windows.Forms.MenuStrip",
    "StatusStrip": "System.Windows.Forms.StatusStrip",
    "ContextMenuStrip": "System.Windows.Forms.ContextMenuStrip",
    "WebBrowser": "System.Windows.Forms.WebBrowser",
}

_NON_VISUAL_DESIGNER_TYPES = frozenset({
    "Timer", "ToolTip", "ImageList", "ErrorProvider", "BindingSource",
    "BackgroundWorker", "OpenFileDialog", "SaveFileDialog", "FolderBrowserDialog",
    "DataGridViewTextBoxColumn", "DataGridViewCheckBoxColumn", "ColumnHeader",
})


def _normalize_wf_type(raw_type: str, ns: str) -> str:
    if raw_type.startswith("System.Windows.Forms."):
        return raw_type
    short = raw_type.rsplit(".", 1)[-1]
    if short in _WINFORMS_SHORT_TO_FULL and "." not in raw_type:
        return _WINFORMS_SHORT_TO_FULL[short]
    if "." in raw_type:
        return raw_type
    return f"{ns}.{raw_type}" if ns else raw_type


def parse_winforms_designer(
    cs_text: str,
    rel_path: str,
    assembly_name: str,
    default_ns: str,
    is_shipping: bool,
) -> Optional[WinFormsParseResult]:
    if "InitializeComponent" not in cs_text:
        return None
    # Skip auto-generated .Resources.Designer.cs / .Settings.Designer.cs files!
    if "ResourceManager" in cs_text and "Controls.Add" not in cs_text and "AutoScaleMode" not in cs_text:
        return None

    ns_match = NAMESPACE_RE.search(cs_text)
    ns = ns_match.group(1) if ns_match else default_ns
    cls_match = CLASS_DECL_RE.search(cs_text)
    if not cls_match:
        return None
    cls_name = cls_match.group(1)
    base_raw = cls_match.group(2) or ""
    full_cls = f"{ns}.{cls_name}" if ns else cls_name

    # Collect field & instantiation types + line numbers
    ctrl_types: Dict[str, str] = {}
    ctrl_lines: Dict[str, int] = {}
    for m in FIELD_DECL_RE.finditer(cs_text):
        t_raw, v_name = m.group(1), m.group(2)
        if t_raw.rsplit(".", 1)[-1] not in _NON_VISUAL_DESIGNER_TYPES:
            ctrl_types[v_name] = _normalize_wf_type(t_raw, ns)

    for idx, line in enumerate(cs_text.splitlines(), 1):
        m = NEW_ASSIGN_RE.match(line)
        if m:
            v_name, t_raw = m.group(1), m.group(2)
            if t_raw.rsplit(".", 1)[-1] in _NON_VISUAL_DESIGNER_TYPES:
                ctrl_types.pop(v_name, None)
                continue
            ctrl_types[v_name] = _normalize_wf_type(t_raw, ns)
            ctrl_lines[v_name] = idx

    if not ctrl_types and "AutoScaleDimensions" not in cs_text and "ClientSize" not in cs_text:
        return None

    ctrl_props: Dict[str, Dict[str, str]] = {k: {} for k in ctrl_types}
    root_props: Dict[str, str] = {}

    for m in PROP_STR_RE.finditer(cs_text):
        target, prop, val = m.group(1), m.group(2), unescape_csharp_string(m.group(3))
        if target in ctrl_props:
            ctrl_props[target][prop] = val
    for m in PROP_RES_RE.finditer(cs_text):
        target, prop, res_key, static_ref = m.group(1), m.group(2), m.group(3), m.group(4)
        if target in ctrl_props:
            ctrl_props[target][prop] = f"{{Resx:{res_key}}}" if res_key else f"{{x:Static {static_ref}}}"
    for m in PROP_BOOL_INT_RE.finditer(cs_text):
        target, prop, val = m.group(1), m.group(2), m.group(3)
        if target in ctrl_props:
            ctrl_props[target][prop] = val
    for m in ROOT_PROP_STR_RE.finditer(cs_text):
        root_props[m.group(1)] = unescape_csharp_string(m.group(2))

    children_map: Dict[str, List[str]] = {"$root": []}
    has_parent: set[str] = set()

    # Expand AddRange blocks first in source order, then single Controls.Add
    events_in_order: List[tuple[int, str, str]] = []
    for m in CONTROLS_ADDRANGE_RE.finditer(cs_text):
        parent_var = m.group(1) or "$root"
        items_blob = m.group(2)
        pos = m.start()
        for tok in items_blob.split(","):
            tok_clean = tok.strip()
            if tok_clean.startswith("this."):
                tok_clean = tok_clean[5:].strip()
            if tok_clean in ctrl_types:
                events_in_order.append((pos, parent_var, tok_clean))
                pos += 1

    char_offset = 0
    for raw_line in cs_text.splitlines(keepends=True):
        m = CONTROLS_ADD_RE.match(raw_line)
        if m:
            parent_var = m.group(1) or "$root"
            child_var = m.group(2)
            if child_var in ctrl_types:
                events_in_order.append((char_offset, parent_var, child_var))
        char_offset += len(raw_line)

    events_in_order.sort(key=lambda x: x[0])
    for _, p_var, c_var in events_in_order:
        key = p_var if p_var in ctrl_types else "$root"
        if c_var == key:
            continue
        lst = children_map.setdefault(key, [])
        if c_var not in lst:
            lst.append(c_var)
            has_parent.add(c_var)

    for v_name in sorted(ctrl_types):
        if v_name not in has_parent:
            children_map["$root"].append(v_name)

    def_kind = "Form" if ("Form" in base_raw or "ClientSize" in cs_text) else "UserControl"
    root_clr = "System.Windows.Forms.Form" if def_kind == "Form" else "System.Windows.Forms.UserControl"
    def_id = f"{assembly_name}::{full_cls}"

    res = WinFormsParseResult()
    res.definitions.append({
        "def_id": def_id,
        "kind": def_kind,
        "framework": "WinForms",
        "file": rel_path,
        "full_class_name": full_cls,
        "base_clr_type": _normalize_wf_type(base_raw, ns) if base_raw else root_clr,
        "target_type": "",
        "res_key": "",
        "is_shipping": is_shipping,
    })

    root_elem_id = f"{def_id}::root"
    root_text_props: Dict[str, str] = {}
    if root_props.get("AccessibleName"):
        root_text_props["auto_name"] = root_props["AccessibleName"]
    if root_props.get("Text"):
        root_text_props["title"] = root_props["Text"]
    root_attrs: Dict[str, Any] = {"x_name": root_props.get("Name") or cls_name}
    if root_text_props:
        root_attrs["text_props"] = root_text_props
        root_attrs["raw_text_expr"] = root_text_props.get("auto_name") or root_text_props.get("title", "")
    res.elements.append({
        "elem_id": root_elem_id,
        "def_id": def_id,
        "parent_elem_id": None,
        "xml_order": 0,
        "context": "VisualTree",
        "clr_type": root_clr,
        "attrs": root_attrs,
        "grid_rc": [None, None],
        "line": 1,
    })

    emitted_vars: set[str] = set()

    def emit_children(parent_key: str, parent_elem_id: str) -> None:
        kids = children_map.get(parent_key, [])
        for order_idx, var_name in enumerate(kids):
            if var_name in emitted_vars:
                continue
            emitted_vars.add(var_name)
            c_type = ctrl_types[var_name]
            props = ctrl_props.get(var_name, {})
            eid = f"{def_id}::{var_name}"
            attrs: Dict[str, Any] = {
                "x_name": props.get("Name") or var_name,
            }
            text_props: Dict[str, str] = {}
            if props.get("AccessibleName"):
                text_props["auto_name"] = props["AccessibleName"]
            if props.get("Text"):
                text_props["text"] = props["Text"]
            if text_props:
                attrs["text_props"] = text_props
                attrs["raw_text_expr"] = text_props.get("auto_name") or text_props.get("text", "")
            if props.get("AccessibleDescription"):
                attrs["raw_tooltip_expr"] = props["AccessibleDescription"]
            if props.get("ReadOnly"):
                attrs["is_readonly"] = props["ReadOnly"]
            if props.get("Visible") == "false":
                attrs["initial_vis"] = "Hidden"
            if props.get("TabIndex"):
                attrs["tab_index"] = int(props["TabIndex"])

            res.elements.append({
                "elem_id": eid,
                "def_id": def_id,
                "parent_elem_id": parent_elem_id,
                "xml_order": order_idx,
                "context": "VisualTree",
                "clr_type": c_type,
                "attrs": attrs,
                "grid_rc": [None, None],
                "line": ctrl_lines.get(var_name, 1),
            })

            if not c_type.startswith("System.Windows.Forms."):
                res.edges.append({
                    "src_def_id": def_id,
                    "src_elem_id": eid,
                    "edge_kind": "XAML_EMBED",
                    "dst_type_or_key": c_type,
                    "extra": {"line": ctrl_lines.get(var_name, 1)},
                })

            emit_children(var_name, eid)

    emit_children("$root", root_elem_id)
    return res
