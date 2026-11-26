"""C# class inheritance, AddIn/ApplicationSystem registration, and UI composition wiring extractor.

100% Python standard library. Distinguishes deterministic concrete UI mounting (`CS_ASSIGN`,
`ADDIN_MOUNT`, `OPENS_DIALOG`) from polymorphic runtime slots (`DYNAMIC_SLOT`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from autowork_core.ui_inventory.scanner.resx_config_parser import (
    strip_csharp_comments,
    unescape_csharp_string,
)


USING_NS_RE = re.compile(r"^\s*using\s+([A-Za-z_][\w.]*)\s*;", re.MULTILINE)
NAMESPACE_RE = re.compile(r"\bnamespace\s+([A-Za-z_][\w.]*)")
CLASS_HEADER_RE = re.compile(
    r"(?:\[\s*AddIn\s*\(\s*\"([^\"]+)\"\s*\)\s*\]\s*)?"
    r"(?:public|internal|protected|private|abstract|sealed|partial|static|\s)+"
    r"class\s+([A-Za-z_]\w*)\s*(?:<[^>]+>)?\s*(?::\s*([^{\r\n]+))?",
)
TYPED_VAR_NEW_RE = re.compile(
    r"\b(?:var|([A-Za-z_][\w.]*))\s+([A-Za-z_]\w*)\s*=\s*new(?:\s+([A-Za-z_][\w.]*))?\s*\("
)
ASSIGN_NEW_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_]\w*)\s*=\s*new\s+([A-Za-z_][\w.]*)\s*\("
)
FIELD_TYPE_RE = re.compile(
    r"\b(?:private|protected|internal|public)\s+(?:readonly\s+)?([A-Za-z_][\w.]*)\??\s+([A-Za-z_]\w*)\s*(?:;|=)"
)
DI_VAR_RESOLVE_RE = re.compile(
    r"\b(?:var|([A-Za-z_][\w.]*))\s+([A-Za-z_]\w*)\s*=\s*[^;=\r\n]+\."
    r"(?:GetInstance|Resolve|GetService|GetRequiredService|GetExportedValue|CreateInstance|Create|LoadView)\s*<\s*([A-Za-z_][\w.]*)\s*>\s*\("
)
DI_ASSIGN_RESOLVE_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_]\w*)\s*=\s*[^;=\r\n]+\."
    r"(?:GetInstance|Resolve|GetService|GetRequiredService|GetExportedValue|CreateInstance|Create|LoadView)\s*<\s*([A-Za-z_][\w.]*)\s*>\s*\("
)
SELF_DATACONTEXT_ASSIGN_RE = re.compile(
    r"(?<![\w.])(?:this\.)?DataContext\s*=\s*(?:"
    r"new\s+([A-Za-z_][\w.]*)\s*\("
    r"|[^;=\r\n]+\.(?:GetInstance|Resolve|GetService|GetRequiredService|GetExportedValue|CreateInstance)\s*<\s*([A-Za-z_][\w.]*)\s*>\s*\("
    r"|([A-Za-z_]\w*)\s*;)"
)
EXT_DATACONTEXT_ASSIGN_RE = re.compile(
    r"\b([A-Za-z_]\w*)\.DataContext\s*=\s*(?:"
    r"new\s+([A-Za-z_][\w.]*)\s*\("
    r"|[^;=\r\n]+\.(?:GetInstance|Resolve|GetService|GetRequiredService|GetExportedValue|CreateInstance)\s*<\s*([A-Za-z_][\w.]*)\s*>\s*\("
    r"|([A-Za-z_]\w*)\s*;)"
)
METHOD_SCOPE_RE = re.compile(
    r"(?:^|[;\n{}])\s*(?:(?:public|internal|protected|private|static|virtual|override|async|sealed|new|partial)\s+)*"
    r"(?:([A-Za-z_][\w.<>?\[\]]*)\s+)?([A-Za-z_]\w*)\s*(?:<[^>]+>)?\s*\(([^)]{0,360})\)"
)
GENERIC_MOUNT_CALL_RE = re.compile(
    r"\b(?:this\.)?[A-Za-z_]\w*\s*<\s*([A-Za-z_][\w.]*)\s*>\s*\(([^;()\r\n]{1,160})\)"
)
REGION_REGISTER_RE = re.compile(
    r"\bRegisterViewWithRegion\s*(?:<\s*([A-Za-z_][\w.]*)\s*>)?\s*\(\s*\"([^\"]+)\""
    r"(?:\s*,\s*typeof\s*\(\s*([A-Za-z_][\w.]*)\s*\))?"
)

# Generic ViewModel / Code-Behind member value-flow patterns (zero product-specific method names!)
_STR_MEMBER_DECL_RE = re.compile(
    r"\b(?:public|internal|protected|private)\s+(?:(?:virtual|override|new|static|readonly|const)\s+)*string\??\s+([A-Za-z_]\w*)\b"
)
_VM_TYPE_PROP_RE = re.compile(
    r"\b(?:public|internal)\s+(?:(?:virtual|override|new)\s+)*([A-Za-z_][\w.]*(?:ViewModel|VM|View|Panel|Control))\??\s+([A-Za-z_]\w*)\s*(?:\{|=>)"
)
_STR_EXPR_PROP_RE = re.compile(r"\bstring\??\s+([A-Za-z_]\w*)\s*=>\s*([^;\r\n]+);")
_STR_AUTO_INIT_RE = re.compile(r"\bstring\??\s+([A-Za-z_]\w*)\s*\{[^}]*\}\s*=\s*([^;\r\n]+);")
_STR_GETTER_PROP_RE = re.compile(
    r"\bstring\??\s+([A-Za-z_]\w*)\s*\{\s*get\s*(?:=>\s*([^;\r\n]+);|\{\s*return\s+([^;\r\n]+);\s*\})"
)
_STR_SETTER_FIELD_RE = re.compile(
    r"\bstring\??\s+([A-Za-z_]\w*)\s*\{[^}]*\bset\s*\{\s*(?:this\.)?([A-Za-z_]\w*)\s*=\s*value\s*;"
)
_STR_ASSIGN_RE = re.compile(r"\b(?:this\.)?([A-Za-z_]\w*)\s*=\s*([^;{}]{1,280});")
_CTRL_TEXT_ASSIGN_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_]\w*)\.(Content|Text|Header|ToolTip)\s*=\s*([^;{}]{1,280});"
)
_ROOT_TITLE_ASSIGN_RE = re.compile(
    r"\b(?:this\.)?Title\s*=\s*([^;{}\r\n]{1,200});"
)

_CS_FLOW_KEYWORDS = frozenset({
    "if", "else", "for", "foreach", "while", "switch", "catch", "using", "lock", "fixed", "try", "when",
})
_CS_NON_RETURN_KEYWORDS = frozenset({
    "return", "throw", "new", "await", "yield", "else", "case", "goto",
})

# Pure single-expression RHS extractors for closed-universe localization verification
_TERNARY_IS_NULL_RE = re.compile(
    r"^(!?)\s*(?:string|String)\.(?:IsNullOrEmpty|IsNullOrWhiteSpace)\s*\((.+?)\)\s*\?\s*(.+?)\s*:\s*(.+)$",
    re.DOTALL,
)
_PURE_CALL_KEY_RE = re.compile(
    r"^(?:[A-Za-z_][\w.()]*\.)?([A-Za-z_]\w*)\s*\(\s*\"((?:[^\"\\]|\\.)+)\"\s*(?:,[^()]{0,60})?\)$"
)
_PURE_INDEXER_KEY_RE = re.compile(
    r"^([A-Za-z_][\w.()]*)\s*\[\s*\"((?:[^\"\\]|\\.)+)\"\s*\]$"
)
_SIMPLE_LITERAL_RE = re.compile(r'^"((?:[^"\\]|\\.)+)"$')
_SIMPLE_STATIC_REF_RE = re.compile(r"^([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)$")
_SIMPLE_IDENT_RE = re.compile(r"^(?:this\.)?([A-Za-z_]\w*)$")

_NON_LOC_METHOD_NAMES = frozenset({
    "format", "replace", "contains", "startswith", "endswith", "split", "join",
    "substring", "remove", "indexof", "lastindexof", "tostring", "parse", "tryparse",
    "combine", "getextension", "getfilename", "getfilenamewithoutextension",
    "getdirectoryname", "getfullpath", "getenvironmentvariable", "getfolderpath",
    "getattribute", "getsetting", "getconfig", "getproperty", "getfield", "gettype",
    "readalltext", "writealltext", "onpropertychanged", "raisepropertychanged",
    "notifypropertychanged", "setproperty", "writeline", "write", "append",
    "assert", "equals", "compare", "ismatch", "match", "regex", "loadgeometry",
    "loadicon", "loadimage", "loadbrush", "findresource", "tryfindresource",
})
_NON_LOC_METHOD_PREFIXES = ("log", "trace", "debug", "warn", "error", "write", "print", "raise", "notify", "invoke", "throw")
_NON_LOC_INDEXER_HINTS = frozenset({
    "config", "configuration", "settings", "appsettings", "env", "environment",
    "headers", "cookies", "query", "params", "parameters", "args", "dict",
    "dictionary", "map", "cache", "session", "attributes", "metadata", "properties",
})
_SKIP_XSTATIC_CLASSES = frozenset({
    "string", "environment", "path", "guid", "datetime", "cultureinfo", "visibility", "colors", "brushes", "math",
})
_NON_UI_TYPES_LOWER = frozenset({
    "int", "long", "short", "byte", "sbyte", "uint", "ulong", "ushort",
    "bool", "boolean", "float", "double", "decimal", "char", "string", "object", "void", "dynamic",
    "int16", "int32", "int64", "single", "guid", "datetime", "datetimeoffset", "timespan",
    "uri", "type", "task", "valuetask", "exception", "cancellationtoken",
    "color", "brush", "solidcolorbrush", "lineargradientbrush", "imagesource", "bitmapimage",
    "geometry", "visibility", "thickness", "point", "size", "rect", "cornerradius",
    "list", "dictionary", "hashset", "queue", "stack", "array", "observablecollection",
    "collectionview", "listcollectionview", "bindinglistcollectionview", "collectionviewsource",
    "eventargs", "routedeventargs", "propertychangedeventargs", "dependencyobject",
    "t", "tkey", "tvalue", "tresult", "titem", "tmodel", "tviewmodel",
})
_SKIP_SLOT_ARG_NAMES = frozenset({"this", "base", "null", "true", "false"})
_VM_PROP_KIND_PRIORITY = {"dynamic_multi": 10, "key_probe": 4, "static_ref": 3, "literal": 2, "alias": 1, "type_ref": 0}

# UI Wiring patterns
CONTENT_ASSIGN_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\.Content\s*=\s*(?:new\s+([A-Za-z_][\w.]*)\s*\(|([A-Za-z_][\w.]*)\s*;)"
)
APP_UI_PANEL_RE = re.compile(
    r"\b(?:Application\.)?ApplicationUI\.(ControlPanel|FirstLayout|MultiWindowView)\s*=\s*(?:new\s+([A-Za-z_][\w.]*)\s*\(|([A-Za-z_]\w*)\b)"
)
ADD_CUSTOM_UI_RE = re.compile(
    r"\b([A-Za-z_]\w*)\.(?:AddCustomUI|AddSubAppPanel|AddDockContent)\s*\(\s*(?:\"[^\"]*\"\s*,\s*)?(?:new\s+([A-Za-z_][\w.]*)\s*\(|([A-Za-z_]\w*))"
)
CHILDREN_ADD_RE = re.compile(
    r"\b(?:this\.)?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\.(?:Children|Controls)\.Add\s*\(\s*(?:new\s+([A-Za-z_][\w.]*)\s*\(|([A-Za-z_]\w*)\s*\))"
)
SHOW_DIALOG_NEW_RE = re.compile(
    r"\bnew\s+([A-Za-z_][\w.]*)\s*\([^;]*?\)\s*\.\s*(ShowDialog|Show)\s*\("
)
SHOW_DIALOG_VAR_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*\.\s*(ShowDialog|Show)\s*\(\s*\)"
)
VISIBILITY_COLLAPSED_RE = re.compile(
    r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*Visi?bility)\s*=\s*Visibility\.Collapsed\b"
)
CONFIG_VIEWER_TYPE_RE = re.compile(
    r'InnerText\s*=\s*"([A-Za-z_][\w.]*\.[A-Za-z_]\w*)"'
)

_ABSTRACT_UI_TYPES = frozenset({
    "UIElement", "FrameworkElement", "Control", "ContentControl", "Visual",
    "object", "Object", "IView", "UserControl",
})
_IGNORED_INTERFACES_PREFIX = ("INotify", "IDisposable", "ICommand", "IEnumerable", "IComparable", "IFormattable")


@dataclass
class CSharpScanResult:
    types: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    vm_properties: List[Dict[str, Any]] = field(default_factory=list)


def _is_non_ui_type(type_token: str) -> bool:
    if not type_token:
        return True
    short = type_token.rsplit(".", 1)[-1].rstrip("?[]")
    return short.lower() in _NON_UI_TYPES_LOWER or short in _ABSTRACT_UI_TYPES


def _unescape_cs_string(raw: str) -> str:
    return unescape_csharp_string(raw)


def _is_plausible_loc_key(key: str) -> bool:
    if not key or not key.strip():
        return False
    if len(key) > 96 or "\n" in key or "\r" in key or "://" in key or "\\\\" in key or "{0" in key:
        return False
    # Reject multi-word sentences as lookup keys unless short
    if " " in key and len(key) > 32:
        return False
    return True


def _classify_single_rhs(
    rhs: str,
    declared_members: Set[str],
    known_var_names: Set[str],
) -> Optional[Tuple[str, str]]:
    rhs = " ".join(rhs.split()).strip()
    if not rhs or rhs in ("null", '""', "string.Empty", "String.Empty"):
        return None
    # Strip outer parens
    while rhs.startswith("(") and rhs.endswith(")"):
        inner = rhs[1:-1].strip()
        if inner.count("(") == inner.count(")"):
            rhs = inner
        else:
            break

    # 1. Pure single method call with string-literal first arg (verified against closed localization table in linker!)
    m_call = _PURE_CALL_KEY_RE.match(rhs)
    if m_call:
        meth_low = m_call.group(1).lower()
        key = _unescape_cs_string(m_call.group(2))
        if (
            meth_low not in _NON_LOC_METHOD_NAMES
            and not meth_low.startswith(_NON_LOC_METHOD_PREFIXES)
            and _is_plausible_loc_key(key)
        ):
            return ("key_probe", key)
        return None

    # 2. Pure single indexer lookup (`_loc["KEY"]`, `Strings["KEY"]`)
    m_idx = _PURE_INDEXER_KEY_RE.match(rhs)
    if m_idx:
        recv_last = m_idx.group(1).rsplit(".", 1)[-1].lstrip("_").rstrip("()").lower()
        key = _unescape_cs_string(m_idx.group(2))
        if recv_last not in _NON_LOC_INDEXER_HINTS and _is_plausible_loc_key(key):
            return ("key_probe", key)
        return None

    # 3. Direct string literal
    m_lit = _SIMPLE_LITERAL_RE.match(rhs)
    if m_lit:
        val = _unescape_cs_string(m_lit.group(1))
        if (
            not val.strip()
            or len(val) > 96
            or "{0" in val
            or "{1" in val
            or "://" in val
            or "\\\\" in val
        ):
            return None
        return ("literal", val)

    # 4. Same-class member alias
    m_id = _SIMPLE_IDENT_RE.match(rhs)
    if m_id:
        ident = m_id.group(1)
        if ident in declared_members:
            return ("alias", ident)
        return None

    # 5. Strict static reference (`ResClass.Key` — requires UpperCamel head, never `_field.Sub.Prop` or local var!)
    m_st = _SIMPLE_STATIC_REF_RE.match(rhs)
    if m_st:
        full_expr = f"{m_st.group(1)}.{m_st.group(2)}"
        parts = full_expr.split(".")
        head = parts[0]
        rcls, rkey = parts[-2], parts[-1]
        if (
            head
            and head[0].isupper()
            and head not in known_var_names
            and head not in declared_members
            and rcls[0].isupper()
            and rcls.lower() not in _SKIP_XSTATIC_CLASSES
            and rkey not in ("Empty", "Length", "Count", "Name", "Value")
        ):
            return ("static_ref", f"{rcls}.{rkey}")
    return None


def _split_null_coalesce(rhs: str) -> Optional[Tuple[str, str]]:
    """Split on the first `??` strictly OUTSIDE string/verbatim/char literals."""
    i = 0
    n = len(rhs)
    while i < n:
        ch = rhs[i]
        if ch == "@" and i + 1 < n and rhs[i + 1] == '"':
            i += 2
            while i < n:
                if rhs[i] == '"':
                    if i + 1 < n and rhs[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                if rhs[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if rhs[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "'":
            i += 1
            while i < n:
                if rhs[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if rhs[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if ch == "?" and i + 1 < n and rhs[i + 1] == "?":
            return rhs[:i], rhs[i + 2 :]
        i += 1
    return None


def _find_matching_brace(code: str, open_pos: int) -> int:
    """Return index of the `}` matching `code[open_pos] == '{'`, skipping string/verbatim/char literals."""
    n = len(code)
    if open_pos < 0 or open_pos >= n or code[open_pos] != "{":
        return n
    depth = 0
    i = open_pos
    while i < n:
        ch = code[i]
        if ch == "@" and i + 1 < n and code[i + 1] == '"':
            i += 2
            while i < n:
                if code[i] == '"':
                    if i + 1 < n and code[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                if code[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if code[i] == '"' or code[i] == "\n":
                    i += 1
                    break
                i += 1
            continue
        if ch == "'":
            i += 1
            while i < n:
                if code[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if code[i] == "'" or code[i] == "\n":
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _scan_to_body_brace(code: str, start_pos: int) -> int:
    """Scan forward from `start_pos` outside strings until `{` or `;`; return `{` index or `-1`."""
    n = len(code)
    i = start_pos
    while i < n:
        ch = code[i]
        if ch == "@" and i + 1 < n and code[i + 1] == '"':
            i += 2
            while i < n:
                if code[i] == '"':
                    if i + 1 < n and code[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                if code[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if code[i] == '"' or code[i] == "\n":
                    i += 1
                    break
                i += 1
            continue
        if ch == "'":
            i += 1
            while i < n:
                if code[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if code[i] == "'" or code[i] == "\n":
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            return i
        if ch == ";":
            return -1
        i += 1
    return -1


def _parse_param_list(raw_params: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for param in raw_params.split(","):
        tokens = [tok for tok in param.strip().split() if tok not in ("in", "ref", "out", "params", "this")]
        if len(tokens) >= 2:
            ptype = tokens[-2].split("<", 1)[0].rstrip("?[]").strip()
            pname = tokens[-1].split("=", 1)[0].strip()
            if ptype and pname and not _is_likely_interface(ptype) and not _is_non_ui_type(ptype):
                out[pname] = ptype
    return out


def _blank_subspans(text: str, base_offset: int, subspans: List[Tuple[int, int]]) -> str:
    """Replace `subspans` (absolute offsets) with spaces so outer-class/field regexes never see nested bodies."""
    if not subspans:
        return text
    buf = list(text)
    tlen = len(buf)
    for s, e in subspans:
        rs = max(0, s - base_offset)
        re_ = min(tlen, e - base_offset)
        for k in range(rs, re_):
            if buf[k] != "\n":
                buf[k] = " "
    return "".join(buf)


def _classify_string_rhs(
    rhs_raw: str,
    declared_members: Set[str],
    known_var_names: Set[str],
) -> Optional[Tuple[str, str]]:
    """Classify a C# string expression into `(kind, value)` supporting null-fallback ternaries and `??`."""
    rhs = " ".join(rhs_raw.split()).strip()
    if not rhs or rhs in ("null", '""', "string.Empty", "String.Empty"):
        return None

    # Handle `string.IsNullOrEmpty(expr) ? fallback : expr` and `!string.IsNullOrEmpty(expr) ? expr : fallback`
    m_tern = _TERNARY_IS_NULL_RE.match(rhs)
    if m_tern:
        negated = bool(m_tern.group(1))
        primary_branch = m_tern.group(3) if negated else m_tern.group(4)
        fallback_branch = m_tern.group(4) if negated else m_tern.group(3)
        res_p = _classify_single_rhs(primary_branch, declared_members, known_var_names)
        if res_p is not None:
            return res_p
        return _classify_single_rhs(fallback_branch, declared_members, known_var_names)

    # Handle `expr ?? fallback` (strictly outside string literals!)
    nc = _split_null_coalesce(rhs)
    if nc is not None:
        left, right = nc
        res_l = _classify_single_rhs(left, declared_members, known_var_names)
        if res_l is not None:
            return res_l
        return _classify_single_rhs(right, declared_members, known_var_names)

    return _classify_single_rhs(rhs, declared_members, known_var_names)


def _extract_vm_properties_from_class(
    cls_body: str,
    owner_full_cls: str,
    assembly_name: str,
    known_var_names: Set[str],
    *,
    extract_ctrl_assigns: bool = False,
) -> List[Dict[str, str]]:
    """Extract member value flow (with multi-value dynamic detection) and sub-VM type refs from one C# class."""
    declared: Set[str] = {m.group(1) for m in _STR_MEMBER_DECL_RE.finditer(cls_body)}
    best: Dict[str, Tuple[str, str]] = {}
    concrete_seen: Dict[str, Set[Tuple[str, str]]] = {}

    def _record(prop_name: str, kind: str, val: str) -> None:
        if not prop_name or not val:
            return
        if kind == "alias" and val == prop_name:
            return
        prev = best.get(prop_name)
        if prev is not None and prev[0] == "dynamic_multi":
            return
        if kind in ("key_probe", "static_ref", "literal"):
            seen = concrete_seen.setdefault(prop_name, set())
            seen.add((kind, val))
            if len(seen) >= 2:
                # Multiple distinct static assignments (e.g. Start/Stop state toggle) -> honestly mark dynamic!
                best[prop_name] = ("dynamic_multi", "*")
                return
        if prev is None or _VM_PROP_KIND_PRIORITY.get(kind, 0) > _VM_PROP_KIND_PRIORITY.get(prev[0], 0):
            best[prop_name] = (kind, val)

    for m in _VM_TYPE_PROP_RE.finditer(cls_body):
        ptype, pname = m.group(1).strip(), m.group(2).strip()
        if not _is_likely_interface(ptype) and not _is_non_ui_type(ptype):
            _record(pname, "type_ref", ptype)

    if declared:
        for m in _STR_EXPR_PROP_RE.finditer(cls_body):
            classified = _classify_string_rhs(m.group(2), declared, known_var_names)
            if classified:
                _record(m.group(1), classified[0], classified[1])
        for m in _STR_AUTO_INIT_RE.finditer(cls_body):
            classified = _classify_string_rhs(m.group(2), declared, known_var_names)
            if classified:
                _record(m.group(1), classified[0], classified[1])
        for m in _STR_GETTER_PROP_RE.finditer(cls_body):
            rhs = m.group(2) or m.group(3) or ""
            classified = _classify_string_rhs(rhs, declared, known_var_names)
            if classified:
                _record(m.group(1), classified[0], classified[1])
        for m in _STR_SETTER_FIELD_RE.finditer(cls_body):
            prop_n, field_n = m.group(1), m.group(2)
            if field_n in declared and prop_n != field_n:
                _record(field_n, "alias", prop_n)
        for m in _STR_ASSIGN_RE.finditer(cls_body):
            lhs = m.group(1)
            if lhs not in declared:
                continue
            classified = _classify_string_rhs(m.group(2), declared, known_var_names)
            if classified:
                _record(lhs, classified[0], classified[1])

    # Code-Behind control text assignments (`BtnOk.Content = ...;`, `Title = ...;`) via the same value-flow engine
    if extract_ctrl_assigns:
        for m in _CTRL_TEXT_ASSIGN_RE.finditer(cls_body):
            ctrl_name, prop_attr, rhs_raw = m.group(1), m.group(2).lower(), m.group(3)
            if ctrl_name.lower() in ("this", "base", "string", "console"):
                continue
            classified = _classify_string_rhs(rhs_raw, declared, known_var_names)
            if classified:
                _record(f"$ctrl:{ctrl_name}.{prop_attr}", classified[0], classified[1])
        for m in _ROOT_TITLE_ASSIGN_RE.finditer(cls_body):
            classified = _classify_string_rhs(m.group(1), declared, known_var_names)
            if classified:
                _record("$root.title", classified[0], classified[1])

    return [
        {
            "assembly": assembly_name,
            "owner_class": owner_full_cls,
            "prop": prop,
            "kind": kind,
            "value": val,
        }
        for prop, (kind, val) in sorted(best.items())
    ]


def _is_likely_interface(type_token: str) -> bool:
    short = type_token.rsplit(".", 1)[-1]
    if any(short.startswith(p) for p in _IGNORED_INTERFACES_PREFIX):
        return True
    return len(short) >= 2 and short[0] == "I" and short[1].isupper()


def scan_csharp_file(
    cs_text: str,
    rel_path: str,
    assembly_name: str,
    default_ns: str,
    is_shipping: bool,
) -> CSharpScanResult:
    res = CSharpScanResult()
    cleaned = strip_csharp_comments(cs_text)
    usings = [m.group(1) for m in USING_NS_RE.finditer(cleaned)]

    # 1. Brace-matched multi-namespace & file-scoped namespace spans
    ns_spans: List[Tuple[int, int, str]] = []
    for nm in NAMESPACE_RE.finditer(cleaned):
        ns_name = nm.group(1).strip()
        bpos = _scan_to_body_brace(cleaned, nm.end())
        if bpos >= 0:
            end_b = _find_matching_brace(cleaned, bpos)
            ns_spans.append((bpos, end_b, ns_name))
        else:
            ns_spans.append((nm.start(), len(cleaned), ns_name))

    def _namespace_at(pos: int) -> str:
        chosen = default_ns
        best_span = 10**9
        for s, e, nm_name in ns_spans:
            if s <= pos <= e and (e - s) <= best_span:
                chosen = nm_name
                best_span = e - s
        return chosen

    has_custom_peer_in_file = "OnCreateAutomationPeer" in cleaned
    fname_only = rel_path.rsplit("/", 1)[-1]
    if fname_only.lower().endswith(".xaml.cs"):
        expected_cls_stem = fname_only[:-8]
    elif fname_only.lower().endswith(".cs"):
        expected_cls_stem = fname_only[:-3]
    else:
        expected_cls_stem = fname_only

    # 2. Brace-matched class spans (handling nested classes cleanly without member bleed)
    # Each entry: (hdr_start, body_start, body_end, full_cls, short_cls)
    class_spans: List[Tuple[int, int, int, str, str]] = []
    primary_full_cls = ""
    primary_short_cls = ""
    for m in CLASS_HEADER_RE.finditer(cleaned):
        addin_name, cls_name, base_list_raw = m.group(1), m.group(2), m.group(3)
        bpos = _scan_to_body_brace(cleaned, m.end())
        if bpos < 0:
            continue
        end_b = _find_matching_brace(cleaned, bpos)
        ns = _namespace_at(m.start())
        full_cls = f"{ns}.{cls_name}" if ns else cls_name
        class_spans.append((m.start(), bpos, end_b, full_cls, cls_name))
        if not primary_full_cls:
            primary_full_cls = full_cls
            primary_short_cls = cls_name
        elif cls_name == expected_cls_stem or addin_name:
            primary_full_cls = full_cls
            primary_short_cls = cls_name

        base_cls = ""
        if base_list_raw:
            candidates = [b.strip().split("<", 1)[0].strip() for b in base_list_raw.split(",")]
            non_ifaces = [c for c in candidates if c and not _is_likely_interface(c)]
            if non_ifaces:
                base_cls = non_ifaces[0]

        if base_cls:
            res.types.append({
                "assembly": assembly_name,
                "full_name": full_cls,
                "base_full_name": base_cls,
                "has_custom_peer": has_custom_peer_in_file,
                "usings": usings[:12],
                "addin_name": addin_name or "",
                "source_kind": "Cs",
            })
        elif addin_name:
            res.types.append({
                "assembly": assembly_name,
                "full_name": full_cls,
                "base_full_name": "AddInMain",
                "has_custom_peer": False,
                "usings": usings[:12],
                "addin_name": addin_name,
                "source_kind": "Cs",
            })

    if not is_shipping or not primary_full_cls:
        return res

    is_ui_cs = (
        rel_path.lower().endswith(".xaml.cs")
        or any(u.startswith("System.Windows") for u in usings)
        or "InitializeComponent" in cleaned
    )

    def _enclosing_class_at(pos: int) -> Tuple[int, int, int, str, str]:
        best_c: Optional[Tuple[int, int, int, str, str]] = None
        best_len = 10**9
        for cs in class_spans:
            if cs[1] <= pos <= cs[2] and (cs[2] - cs[1]) < best_len:
                best_c = cs
                best_len = cs[2] - cs[1]
        return best_c if best_c is not None else class_spans[0]

    def _src_def_at(pos: int) -> str:
        return f"{assembly_name}::{_enclosing_class_at(pos)[3]}"

    # 3. Extract method/constructor scopes with per-method parameter maps
    # Each entry: (body_start, body_end, method_name, params_dict, owner_full_cls, is_ctor)
    method_scopes: List[Tuple[int, int, str, Dict[str, str], str, bool]] = []
    ctor_param_types_by_cls: Dict[str, List[str]] = {}
    for mm in METHOD_SCOPE_RE.finditer(cleaned):
        ret_tok = (mm.group(1) or "").strip()
        m_name = mm.group(2).strip()
        params_raw = mm.group(3)
        if m_name in _CS_FLOW_KEYWORDS or ret_tok in _CS_NON_RETURN_KEYWORDS:
            continue
        bpos = _scan_to_body_brace(cleaned, mm.end())
        if bpos < 0:
            continue
        end_b = _find_matching_brace(cleaned, bpos)
        encl = _enclosing_class_at(mm.start())
        owner_full = encl[3]
        owner_short = encl[4]
        is_ctor = (m_name == owner_short and not ret_tok)
        params_map = _parse_param_list(params_raw)
        method_scopes.append((bpos, end_b, m_name, params_map, owner_full, is_ctor))
        if is_ui_cs and is_ctor and owner_full == primary_full_cls:
            lst_cp = ctor_param_types_by_cls.setdefault(owner_full, [])
            for pt in params_map.values():
                if pt not in lst_cp:
                    lst_cp.append(pt)

    # 4. Collect positional local/assignment variable type facts (never overwrites across methods!)
    var_decls: List[Tuple[int, str, str]] = []
    for m in TYPED_VAR_NEW_RE.finditer(cleaned):
        decl_t, vname, new_t = (m.group(1) or "").strip(), m.group(2), (m.group(3) or "").strip()
        resolved = new_t or decl_t
        if resolved and resolved != "var":
            var_decls.append((m.start(), vname, resolved))
    for m in ASSIGN_NEW_RE.finditer(cleaned):
        var_decls.append((m.start(), m.group(1), m.group(2)))
    for m in DI_VAR_RESOLVE_RE.finditer(cleaned):
        var_decls.append((m.start(), m.group(2), m.group(3)))
    for m in DI_ASSIGN_RESOLVE_RE.finditer(cleaned):
        var_decls.append((m.start(), m.group(1), m.group(2)))
    var_decls.sort(key=lambda x: x[0])

    # 5. Per-class field maps & constructor field initializations (masking nested classes & method bodies)
    cls_fields: Dict[str, Dict[str, str]] = {}
    cls_ctor_assigns: Dict[str, Dict[str, str]] = {}
    for hdr_s, b_start, b_end, full_cls_sp, _ in class_spans:
        nested_spans = [
            (other[0], other[2] + 1)
            for other in class_spans
            if other[0] > b_start and other[2] < b_end
        ]
        own_body = _blank_subspans(cleaned[b_start : b_end + 1], b_start, nested_spans)
        own_methods = [
            (ms[0], ms[1] + 1)
            for ms in method_scopes
            if ms[0] > b_start and ms[1] < b_end and ms[4] == full_cls_sp
        ]
        field_only_body = _blank_subspans(own_body, b_start, own_methods)
        fmap: Dict[str, str] = {}
        for fm in FIELD_TYPE_RE.finditer(field_only_body):
            fmap[fm.group(2)] = fm.group(1)
        cls_fields[full_cls_sp] = fmap

        ctor_map: Dict[str, str] = {}
        for ms in method_scopes:
            if ms[4] == full_cls_sp and ms[5]:
                for pos, vn, vt in var_decls:
                    if ms[0] <= pos <= ms[1] and vn in fmap and not _is_non_ui_type(vt):
                        ctor_map[vn] = vt
        cls_ctor_assigns[full_cls_sp] = ctor_map

        # Collect known variable/field/param names strictly scoped to this class
        cls_known_vars: Set[str] = set(fmap.keys())
        for ms in method_scopes:
            if ms[4] == full_cls_sp:
                cls_known_vars.update(ms[3].keys())
        for pos, vn, _ in var_decls:
            if b_start <= pos <= b_end and _enclosing_class_at(pos)[3] == full_cls_sp:
                cls_known_vars.add(vn)

        res.vm_properties.extend(
            _extract_vm_properties_from_class(
                own_body,
                full_cls_sp,
                assembly_name,
                cls_known_vars,
                extract_ctrl_assigns=(is_ui_cs and full_cls_sp == primary_full_cls),
            )
        )

    def _enclosing_method_at(pos: int) -> Optional[Tuple[int, int, str, Dict[str, str], str, bool]]:
        best_m: Optional[Tuple[int, int, str, Dict[str, str], str, bool]] = None
        best_len = 10**9
        for ms in method_scopes:
            if ms[0] <= pos <= ms[1] and (ms[1] - ms[0]) < best_len:
                best_m = ms
                best_len = ms[1] - ms[0]
        return best_m

    def resolve_var_type(var_name: str, at_pos: int) -> str:
        if not var_name:
            return ""
        encl_m = _enclosing_method_at(at_pos)
        if encl_m is not None:
            m_start, _, _, m_params, _, _ = encl_m
            for pos, vn, vt in reversed(var_decls):
                if pos >= at_pos:
                    continue
                if pos < m_start:
                    break
                if vn == var_name:
                    return vt
            if var_name in m_params:
                return m_params[var_name]
        encl_c = _enclosing_class_at(at_pos)
        owner_cls = encl_c[3]
        if var_name in cls_ctor_assigns.get(owner_cls, {}):
            return cls_ctor_assigns[owner_cls][var_name]
        if var_name in cls_fields.get(owner_cls, {}):
            return cls_fields[owner_cls][var_name]
        if encl_m is None:
            for pos, vn, vt in reversed(var_decls):
                if pos >= at_pos:
                    continue
                if pos < encl_c[1]:
                    break
                if vn == var_name and _enclosing_method_at(pos) is None:
                    return vt
        return ""

    # Collect initial Collapsed overrides on sub-views (e.g. commonToolBarView.AxialVisbility = Visibility.Collapsed)
    vis_overrides: Dict[str, List[str]] = {}
    for m in VISIBILITY_COLLAPSED_RE.finditer(cleaned):
        target_var, vis_prop = m.group(1), m.group(2)
        vis_overrides.setdefault(target_var, []).append(vis_prop)

    def qualify_target(raw_t: str) -> str:
        return raw_t

    # 1. ApplicationUI.ControlPanel / FirstLayout mounts
    for m in APP_UI_PANEL_RE.finditer(cleaned):
        slot_role, new_t, var_n = m.group(1), m.group(2), m.group(3)
        resolved_t = new_t or resolve_var_type(var_n or "", m.start())
        if resolved_t and not _is_non_ui_type(resolved_t):
            res.edges.append({
                "src_def_id": _src_def_at(m.start()),
                "src_elem_id": slot_role,
                "edge_kind": "ADDIN_MOUNT",
                "dst_type_or_key": qualify_target(resolved_t),
                "extra": {
                    "file": rel_path,
                    "collapsed_props": vis_overrides.get(var_n or "", []),
                },
            })

    # 2. ConfigGenerator ViewerType registrations
    if "ViewerType" in cleaned:
        for m in CONFIG_VIEWER_TYPE_RE.finditer(cleaned):
            vtype = m.group(1)
            res.edges.append({
                "src_def_id": _src_def_at(m.start()),
                "src_elem_id": "CompositeSubApps",
                "edge_kind": "ADDIN_MOUNT",
                "dst_type_or_key": vtype,
                "extra": {"file": rel_path},
            })

    # 3. <container>.Content = <subView> (strictly ignore primitive/string text assignments like `Label.Content = msg`)
    for m in CONTENT_ASSIGN_RE.finditer(cleaned):
        lhs, new_t, rhs_expr = m.group(1), m.group(2), (m.group(3) or "").strip()
        if rhs_expr == "null":
            continue
        resolved_t = new_t or resolve_var_type(rhs_expr, m.start())
        if resolved_t:
            if _is_non_ui_type(resolved_t):
                continue
            parent_part, slot_part = (lhs.split(".", 1) if "." in lhs else ("$this", lhs))
            encl_full = _enclosing_class_at(m.start())[3]
            parent_type = resolve_var_type(parent_part, m.start()) or encl_full if parent_part != "$this" else encl_full
            res.edges.append({
                "src_def_id": f"{assembly_name}::{parent_type}",
                "src_elem_id": slot_part,
                "edge_kind": "CS_ASSIGN",
                "dst_type_or_key": qualify_target(resolved_t),
                "extra": {
                    "file": rel_path,
                    "collapsed_props": vis_overrides.get(rhs_expr, []),
                },
            })
        elif rhs_expr and not rhs_expr.startswith('"'):
            res.edges.append({
                "src_def_id": _src_def_at(m.start()),
                "src_elem_id": lhs,
                "edge_kind": "DYNAMIC_SLOT",
                "dst_type_or_key": rhs_expr,
                "extra": {"file": rel_path},
            })

    # 4. AddCustomUI / AddSubAppPanel / AddDockContent
    for m in ADD_CUSTOM_UI_RE.finditer(cleaned):
        host_var, new_t, arg_var = m.group(1), m.group(2), m.group(3)
        resolved_t = new_t or resolve_var_type(arg_var or "", m.start())
        if resolved_t and not _is_non_ui_type(resolved_t):
            res.edges.append({
                "src_def_id": _src_def_at(m.start()),
                "src_elem_id": host_var,
                "edge_kind": "CS_ASSIGN",
                "dst_type_or_key": qualify_target(resolved_t),
                "extra": {
                    "file": rel_path,
                    "collapsed_props": vis_overrides.get(host_var, []),
                },
            })

    # 5. .Children.Add / .Controls.Add
    for m in CHILDREN_ADD_RE.finditer(cleaned):
        lhs, new_t, arg_var = m.group(1), m.group(2), m.group(3)
        resolved_t = new_t or resolve_var_type(arg_var or "", m.start())
        if resolved_t and not _is_non_ui_type(resolved_t):
            res.edges.append({
                "src_def_id": _src_def_at(m.start()),
                "src_elem_id": lhs,
                "edge_kind": "CS_ASSIGN",
                "dst_type_or_key": qualify_target(resolved_t),
                "extra": {"file": rel_path},
            })

    # 6. DataContext assignment (self `DataContext = vm;` + external assembler `viewVar.DataContext = vm;` + UI ctor injection)
    seen_vm_links: Set[Tuple[str, str]] = set()
    for m in SELF_DATACONTEXT_ASSIGN_RE.finditer(cleaned):
        new_t, di_t, var_n = m.group(1), m.group(2), m.group(3)
        resolved_vm = new_t or di_t or resolve_var_type(var_n or "", m.start())
        s_def = _src_def_at(m.start())
        if (
            resolved_vm
            and (s_def, resolved_vm) not in seen_vm_links
            and not _is_non_ui_type(resolved_vm)
            and not _is_likely_interface(resolved_vm)
        ):
            seen_vm_links.add((s_def, resolved_vm))
            res.edges.append({
                "src_def_id": s_def,
                "src_elem_id": None,
                "edge_kind": "VIEW_VM_LINK",
                "dst_type_or_key": qualify_target(resolved_vm),
                "extra": {"source": "DataContextAssign", "file": rel_path},
            })

    seen_ext_vm_links: Set[Tuple[str, str, str]] = set()
    for m in EXT_DATACONTEXT_ASSIGN_RE.finditer(cleaned):
        view_var, new_t, di_t, vm_var = m.group(1), m.group(2), m.group(3), m.group(4)
        if view_var in ("this", "base"):
            continue
        view_t = resolve_var_type(view_var, m.start())
        vm_t = new_t or di_t or resolve_var_type(vm_var or "", m.start())
        s_def = _src_def_at(m.start())
        if (
            view_t
            and vm_t
            and view_t != vm_t
            and not _is_non_ui_type(view_t)
            and not _is_non_ui_type(vm_t)
            and not _is_likely_interface(vm_t)
        ):
            triple = (s_def, qualify_target(view_t), qualify_target(vm_t))
            if triple not in seen_ext_vm_links:
                seen_ext_vm_links.add(triple)
                res.edges.append({
                    "src_def_id": s_def,
                    "src_elem_id": triple[1],
                    "edge_kind": "EXT_VIEW_VM_LINK",
                    "dst_type_or_key": triple[2],
                    "extra": {"source": "ExternalDataContextAssign", "file": rel_path},
                })

    if is_ui_cs:
        primary_def_id = f"{assembly_name}::{primary_full_cls}"
        for ptype in ctor_param_types_by_cls.get(primary_full_cls, []):
            if (primary_def_id, ptype) not in seen_vm_links:
                res.edges.append({
                    "src_def_id": primary_def_id,
                    "src_elem_id": None,
                    "edge_kind": "VIEW_VM_CTOR",
                    "dst_type_or_key": qualify_target(ptype),
                    "extra": {"source": "CtorInjection", "file": rel_path},
                })

    # 7. Prism / MEF Region registration (`RegisterViewWithRegion("MainRegion", typeof(MyView))`)
    for m in REGION_REGISTER_RE.finditer(cleaned):
        gen_t, reg_name, typeof_t = m.group(1), m.group(2), m.group(3)
        view_t = gen_t or typeof_t or ""
        if view_t and reg_name and not _is_non_ui_type(view_t):
            res.edges.append({
                "src_def_id": _src_def_at(m.start()),
                "src_elem_id": reg_name.strip(),
                "edge_kind": "REGION_REGISTER",
                "dst_type_or_key": qualify_target(view_t),
                "extra": {"file": rel_path},
            })

    # 8. Generic helper mounting (`AddView<PatientPositionView>(serviceLocator, PatientPositionView);`)
    # Emitted only for UI-related C# files and cross-validated against real XAML container slot in linker.
    if is_ui_cs:
        for m in GENERIC_MOUNT_CALL_RE.finditer(cleaned):
            gen_t, args_raw = m.group(1), m.group(2)
            if _is_non_ui_type(gen_t) or _is_likely_interface(gen_t):
                continue
            s_def = _src_def_at(m.start())
            for raw_arg in args_raw.split(","):
                arg_tok = raw_arg.strip()
                if re.fullmatch(r"[A-Za-z_]\w*", arg_tok):
                    low_arg = arg_tok.lower()
                    if (
                        low_arg in _SKIP_SLOT_ARG_NAMES
                        or low_arg.endswith(("locator", "service", "container", "manager", "factory", "context", "logger"))
                    ):
                        continue
                    res.edges.append({
                        "src_def_id": s_def,
                        "src_elem_id": arg_tok,
                        "edge_kind": "CS_GENERIC_MOUNT",
                        "dst_type_or_key": qualify_target(gen_t),
                        "extra": {"file": rel_path},
                    })

    # 9. Dialog invocations (.ShowDialog / .Show)
    seen_dialogs: Set[Tuple[str, str]] = set()
    for m in SHOW_DIALOG_NEW_RE.finditer(cleaned):
        dtype, mode = m.group(1), m.group(2)
        s_def = _src_def_at(m.start())
        if (s_def, dtype) not in seen_dialogs and dtype != "MessageBox" and not _is_non_ui_type(dtype):
            seen_dialogs.add((s_def, dtype))
            res.edges.append({
                "src_def_id": s_def,
                "src_elem_id": None,
                "edge_kind": "OPENS_DIALOG",
                "dst_type_or_key": dtype,
                "extra": {"mode": mode, "file": rel_path},
            })
    for m in SHOW_DIALOG_VAR_RE.finditer(cleaned):
        vname, mode = m.group(1), m.group(2)
        dtype = resolve_var_type(vname, m.start())
        s_def = _src_def_at(m.start())
        if (
            dtype
            and (s_def, dtype) not in seen_dialogs
            and dtype not in ("Window", "Dialog", "Form", "MessageBox")
            and not _is_non_ui_type(dtype)
        ):
            seen_dialogs.add((s_def, dtype))
            encl_m = _enclosing_method_at(m.start())
            owner_scope_slice = cleaned[encl_m[0] : encl_m[1] + 1] if encl_m else cleaned
            has_owner = bool(re.search(rf"\b{re.escape(vname)}\.Owner\s*=", owner_scope_slice))
            res.edges.append({
                "src_def_id": s_def,
                "src_elem_id": None,
                "edge_kind": "OPENS_DIALOG",
                "dst_type_or_key": dtype,
                "extra": {"mode": mode, "has_owner": has_owner, "file": rel_path},
            })

    return res
