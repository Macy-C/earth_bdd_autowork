"""Pure-stdlib recursive-descent parser for WPF XAML MarkupExtension expressions.

Models the official `[MS-XAML-2009]` expression grammar as a typed AST:
    `MarkupExpr = MarkupLiteral | MarkupCall`

100% Python standard library (packaged into standalone `ct_ui_scanner.pyz`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union


@dataclass(frozen=True)
class MarkupLiteral:
    """Plain literal text or unquoted token."""

    value: str
    quoted: bool = False


@dataclass(frozen=True)
class MarkupCall:
    """Structured WPF MarkupExtension call: `{[prefix:]TypeName posArg, Key=Val, ...}`."""

    type_name: str
    pos_args: Tuple["MarkupExpr", ...] = field(default_factory=tuple)
    named_args: Tuple[Tuple[str, "MarkupExpr"], ...] = field(default_factory=tuple)

    @property
    def prefix(self) -> str:
        return self.type_name.split(":", 1)[0] if ":" in self.type_name else ""

    @property
    def local_name(self) -> str:
        return self.type_name.split(":", 1)[-1]

    def named_map_lower(self) -> Dict[str, "MarkupExpr"]:
        return {k.lower(): v for k, v in self.named_args}


MarkupExpr = Union[MarkupLiteral, MarkupCall]

_BINDING_MODIFIER_KEYS = frozenset({
    "source",
    "elementname",
    "relativesource",
    "mode",
    "converter",
    "converterparameter",
    "converterculture",
    "updatesourcetrigger",
    "stringformat",
    "fallbackvalue",
    "targetnullvalue",
    "validatesondataerrors",
    "validatesonexceptions",
    "notifyonvalidationerror",
    "notifyonsourceupdated",
    "notifyontargetupdated",
    "isasync",
    "bindsdirectlytosource",
    "xpath",
    "delay",
})


def _unescape_markup_token(raw: str) -> str:
    if "\\" not in raw:
        return raw
    out: List[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _split_top_level_args(body: str) -> List[str]:
    """Split comma-separated markup extension arguments respecting nested `{...}` and quotes."""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    quote_ch = ""
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if quote_ch:
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(body[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == quote_ch:
                quote_ch = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(body[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote_ch = ch
            buf.append(ch)
        elif ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            item = "".join(buf).strip()
            if item:
                parts.append(item)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_named_arg(arg: str) -> Optional[Tuple[str, str]]:
    """If `arg` is `Name = Value` at top-level (depth 0, outside quotes), return `(Name, Value)`."""
    depth = 0
    quote_ch = ""
    i = 0
    n = len(arg)
    while i < n:
        ch = arg[i]
        if quote_ch:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote_ch:
                quote_ch = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in ("'", '"'):
            quote_ch = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            name = arg[:i].strip()
            val = arg[i + 1 :].strip()
            if name and all(c.isalnum() or c in ("_", ".", ":") for c in name):
                return name, val
            return None
        i += 1
    return None


def _has_balanced_outer_braces(text: str) -> bool:
    """Return True iff `text` starts with `{`, ends with `}`, and the opening `{` closes at the very last char."""
    if len(text) < 2 or text[0] != "{" or text[-1] != "}":
        return False
    depth = 0
    quote_ch = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote_ch:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote_ch:
                quote_ch = ""
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in ("'", '"'):
            quote_ch = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and i != n - 1:
                return False
            if depth < 0:
                return False
        i += 1
    return depth == 0


def parse_markup_expr(raw: str, *, _depth: int = 0) -> MarkupExpr:
    """Parse a raw XAML attribute value into a structured `MarkupExpr` AST."""
    if raw is None:
        return MarkupLiteral("")
    s = raw.strip()
    if not s or _depth > 8:
        return MarkupLiteral(s)

    # XAML escape sequence `{}` forces literal interpretation
    if s.startswith("{}"):
        return MarkupLiteral(s[2:], quoted=True)

    # Quoted value inside a nested MarkupExtension argument (`_depth > 0` ONLY; top-level XML quotes were already stripped by expat)
    if _depth > 0 and len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return MarkupLiteral(_unescape_markup_token(s[1:-1]), quoted=True)

    if not _has_balanced_outer_braces(s):
        return MarkupLiteral(_unescape_markup_token(s) if _depth > 0 else s)

    inner = s[1:-1].strip()
    if not inner:
        return MarkupLiteral("")

    # Extract leading MarkupExtension type name up to first whitespace or `{`
    idx = 0
    n = len(inner)
    while idx < n and not inner[idx].isspace() and inner[idx] not in ("{", ","):
        idx += 1
    type_name = inner[:idx].strip()
    rest = inner[idx:].strip()

    if not type_name or not (type_name[0].isalpha() or type_name[0] == "_"):
        return MarkupLiteral(s)

    pos_args: List[MarkupExpr] = []
    named_args: List[Tuple[str, MarkupExpr]] = []

    if rest:
        for raw_arg in _split_top_level_args(rest):
            kv = _split_named_arg(raw_arg)
            if kv is not None:
                k, v = kv
                named_args.append((k, parse_markup_expr(v, _depth=_depth + 1)))
            else:
                pos_args.append(parse_markup_expr(raw_arg, _depth=_depth + 1))

    return MarkupCall(
        type_name=type_name,
        pos_args=tuple(pos_args),
        named_args=tuple(named_args),
    )


def extract_binding_info(expr: str) -> Tuple[str, bool, bool]:
    """Return `(path, has_relative_source, has_external_source)` from a `{Binding ...}` or `{TemplateBinding ...}`.

    - `path`: clean binding path (never a modifier like `Source=`/`Mode=`).
    - `has_relative_source`: True when `RelativeSource=...` is present (e.g. `AncestorType={x:Type DataGrid}`).
    - `has_external_source`: True when `Source=` or `ElementName=` redirects binding away from DataContext.
    """
    if not expr or "Binding" not in expr:
        return "", False, False
    ast = parse_markup_expr(expr)
    if not isinstance(ast, MarkupCall) or ast.local_name not in ("Binding", "TemplateBinding"):
        return "", False, False
    nmap = ast.named_map_lower()
    has_rel = "relativesource" in nmap
    has_ext = "source" in nmap or "elementname" in nmap

    path = ""
    path_node = nmap.get("path")
    if isinstance(path_node, MarkupLiteral):
        path = path_node.value.strip()
    elif ast.pos_args and isinstance(ast.pos_args[0], MarkupLiteral):
        cand = ast.pos_args[0].value.strip()
        if cand and cand.lower() not in _BINDING_MODIFIER_KEYS:
            path = cand
    return path, has_rel, has_ext


def extract_binding_path(expr: str) -> str:
    """Extract only the actual data-binding Path from `{Binding ...}` (ignoring `Source=`/`ElementName=`/`Mode=` etc.)."""
    path, _, _ = extract_binding_info(expr)
    return path


def extract_resource_key_ref(expr: str) -> Optional[str]:
    """Extract literal resource key from `{StaticResource Key}` / `{DynamicResource ResourceKey=Key}`."""
    if not expr or "Resource" not in expr:
        return None
    ast = parse_markup_expr(expr)
    if not isinstance(ast, MarkupCall) or ast.local_name not in ("StaticResource", "DynamicResource"):
        return None
    nmap = ast.named_map_lower()
    target = nmap.get("resourcekey") or (ast.pos_args[0] if ast.pos_args else None)
    if isinstance(target, MarkupLiteral):
        val = target.value.strip()
        return val or None
    return None
