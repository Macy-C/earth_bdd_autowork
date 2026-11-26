"""Bilingual `.resx` string tables, C# StringTable constants, AssemblyInfo Xmlns, and default `.config`/`.xml`.

100% Python standard library.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET


RESX_LOCALE_SUFFIX_RE = re.compile(r"^(.+?)\.([a-z]{2}(?:-[A-Za-z0-9]{2,8})+|[a-z]{2})$", re.IGNORECASE)
HYPHEN_LOCALE_SUFFIX_RE = re.compile(r"^(.+?)-([a-z]{2}(?:-[A-Za-z]{2,4})+)$", re.IGNORECASE)
LCID_SUFFIX_RE = re.compile(r"^(.+?)[.-](\d{4,5})$")
FOLDER_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2,4})+$", re.IGNORECASE)
_KNOWN_ISO_LANGS = frozenset({
    "af", "am", "ar", "az", "be", "bg", "bn", "bs", "ca", "cs", "cy", "da",
    "de", "el", "en", "es", "et", "eu", "fa", "fi", "fil", "fr", "ga", "gl",
    "gu", "he", "hi", "hr", "hu", "hy", "id", "is", "it", "ja", "ka", "kk",
    "km", "kn", "ko", "ky", "lo", "lt", "lv", "mk", "ml", "mn", "mr", "ms",
    "mt", "my", "nb", "ne", "nl", "nn", "no", "or", "pa", "pl", "pt", "ro",
    "ru", "si", "sk", "sl", "sq", "sr", "sv", "sw", "ta", "te", "th", "tr",
    "uk", "ur", "uz", "vi", "zh", "zu",
})
# Official Windows [MS-LCID] decimal culture IDs (used in `.1033.xaml` / `.2052.xaml` / satellite folders)
_EN_LCIDS = frozenset({"1033", "2057", "3081", "4105", "5129", "6153", "7177", "8201", "16393"})
_ZH_LCIDS = frozenset({"2052", "1028", "3076", "4100", "5124"})
_OTHER_LCIDS = frozenset({
    "1025", "1026", "1027", "1029", "1030", "1031", "1032", "1034", "1035", "1036",
    "1037", "1038", "1039", "1040", "1041", "1042", "1043", "1044", "1045", "1046",
    "1048", "1049", "1050", "1051", "1053", "1054", "1055", "1057", "1058", "1060",
    "1061", "1062", "1063", "1066", "2055", "2058", "2060", "2064", "2067", "2068",
    "2070", "2074", "2077", "3079", "3082", "3084", "4108",
})
_ALL_KNOWN_LCIDS = _EN_LCIDS | _ZH_LCIDS | _OTHER_LCIDS
XMLNS_ATTR_CS_RE = re.compile(
    r'\[\s*assembly\s*:\s*XmlnsDefinition(?:Attribute)?\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)\s*\]'
)
CS_CLASS_RE = re.compile(r"\b(?:public|internal|static|partial|\s)+class\s+([A-Za-z_]\w*)")
CS_CONST_STR_RE = re.compile(
    r'\b(?:public|internal)\s+(?:const|static\s+readonly|readonly\s+static)\s+string\s+([A-Za-z_]\w*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*;'
)
CS_ALIAS_STR_RE = re.compile(
    r"\b(?:public|internal)\s+(?:static\s+readonly|readonly\s+static)\s+string\s+([A-Za-z_]\w*)\s*=\s*([A-Za-z_][\w.]*\.[A-Za-z_]\w*)\s*;"
)
ROUTED_UI_CMD_RE = re.compile(
    r'new\s+RoutedUICommand\s*\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*"([A-Za-z_]\w*)"'
)
ROUTED_UI_CMD_RES_RE = re.compile(
    r'new\s+RoutedUICommand\s*\(\s*([A-Za-z_][\w.]*\.[\w]+)\s*,\s*"([A-Za-z_]\w*)"'
)


def strip_csharp_comments(code: str) -> str:
    """Strip C# `//` and `/*...*/` comments while strictly preserving string/verbatim/char literals and line counts."""
    out: List[str] = []
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        # Verbatim string: @"...", $@"...", @$"..."
        if (ch == "@" and i + 1 < n and code[i + 1] == '"') or (
            ch == "$" and i + 2 < n and code[i + 1 : i + 3] == '@"'
        ) or (ch == "@" and i + 2 < n and code[i + 1 : i + 3] == '$"'):
            if ch == "@" and code[i + 1] == '"':
                out.append('@"')
                i += 2
            else:
                out.append(code[i : i + 3])
                i += 3
            while i < n:
                c = code[i]
                out.append(c)
                i += 1
                if c == '"':
                    if i < n and code[i] == '"':
                        out.append('"')
                        i += 1
                    else:
                        break
            continue
        # Normal / interpolated string literal
        if ch == '"':
            out.append('"')
            i += 1
            while i < n:
                c = code[i]
                out.append(c)
                i += 1
                if c == "\\" and i < n:
                    out.append(code[i])
                    i += 1
                elif c == '"' or c == "\n":
                    break
            continue
        # Char literal
        if ch == "'":
            out.append("'")
            i += 1
            while i < n:
                c = code[i]
                out.append(c)
                i += 1
                if c == "\\" and i < n:
                    out.append(code[i])
                    i += 1
                elif c == "'" or c == "\n":
                    break
            continue
        # Single-line comment
        if ch == "/" and i + 1 < n and code[i + 1] == "/":
            i += 2
            while i < n and code[i] != "\n":
                i += 1
            continue
        # Multi-line block comment (preserve newlines for line-number accuracy)
        if ch == "/" and i + 1 < n and code[i + 1] == "*":
            i += 2
            while i < n:
                if code[i] == "*" and i + 1 < n and code[i + 1] == "/":
                    i += 2
                    break
                if code[i] == "\n":
                    out.append("\n")
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def unescape_csharp_string(raw: str) -> str:
    if "\\" not in raw:
        return raw
    return (
        raw.replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )


def split_resx_stem_and_locale(file_stem: str) -> Tuple[str, str]:
    """Return `(resource_class_name, locale_tag)` supporting BCP-47 (`.zh-CN`, `-en-US`) and Windows LCID (`.1033`, `.2052`)."""
    m = RESX_LOCALE_SUFFIX_RE.match(file_stem)
    if m:
        lang = m.group(2).split("-", 1)[0].lower()
        if lang in _KNOWN_ISO_LANGS:
            return m.group(1), m.group(2).lower()
    mh = HYPHEN_LOCALE_SUFFIX_RE.match(file_stem)
    if mh:
        lang = mh.group(2).split("-", 1)[0].lower()
        if lang in _KNOWN_ISO_LANGS:
            return mh.group(1), mh.group(2).lower()
    ml = LCID_SUFFIX_RE.match(file_stem)
    if ml and ml.group(2) in _ALL_KNOWN_LCIDS:
        return ml.group(1), f"lcid:{ml.group(2)}"
    return file_stem, ""


def classify_locale_bucket(locale: str, rel_path: str = "") -> str:
    """Unified resource locale classifier -> `'en'`, `'zh'`, or `'other'` across BCP-47 tags, culture folders, and Windows LCIDs."""
    loc = (locale or "").strip().lower()
    if not loc and rel_path:
        # Check satellite culture folder segments like `zh-CN/Foo.resources.dll`, `de-DE/App.resources`, `2052/Strings.xaml`
        parts = rel_path.replace("\\", "/").split("/")[:-1]
        for seg in reversed(parts):
            low_seg = seg.strip().lower()
            if FOLDER_LOCALE_SEGMENT_RE.match(low_seg) and low_seg.split("-", 1)[0] in _KNOWN_ISO_LANGS:
                loc = low_seg
                break
            if low_seg in _ALL_KNOWN_LCIDS:
                loc = f"lcid:{low_seg}"
                break
    if loc.startswith("lcid:"):
        code = loc[5:]
        if code in _EN_LCIDS:
            return "en"
        if code in _ZH_LCIDS:
            return "zh"
        return "other"
    if not loc or loc == "en" or loc.startswith("en-"):
        return "en"
    if loc == "zh" or loc.startswith("zh-"):
        return "zh"
    return "other"


def parse_resx_entries(xml_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for data_elem in root.findall("data"):
        key = data_elem.attrib.get("name", "").strip()
        if not key or data_elem.attrib.get("type") or data_elem.attrib.get("mimetype"):
            continue
        val_elem = data_elem.find("value")
        if val_elem is not None and val_elem.text is not None:
            out[key] = val_elem.text
    return out


def extract_assembly_xmlns_defs(cs_text: str, assembly_name: str) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for m in XMLNS_ATTR_CS_RE.finditer(cs_text):
        results.append({
            "assembly": assembly_name,
            "xml_uri": m.group(1).strip(),
            "clr_namespace": m.group(2).strip(),
        })
    return results


def extract_csharp_string_constants(cs_text: str, assembly_name: str) -> List[Dict[str, str]]:
    """Extract static string constants, static resource aliases, and RoutedUICommand display texts from C# files."""
    if "string" not in cs_text and "RoutedUICommand" not in cs_text:
        return []
    cleaned = strip_csharp_comments(cs_text)
    class_spans = [(m.start(), m.group(1)) for m in CS_CLASS_RE.finditer(cleaned)]
    if not class_spans:
        return []

    def _owner_class(offset: int) -> str:
        chosen = class_spans[0][1]
        for pos, cname in class_spans:
            if pos <= offset:
                chosen = cname
            else:
                break
        return chosen

    entries: List[Dict[str, str]] = []

    for m in CS_CONST_STR_RE.finditer(cleaned):
        entries.append({
            "assembly": assembly_name,
            "res_class": _owner_class(m.start()),
            "key": m.group(1),
            "val_en": unescape_csharp_string(m.group(2)),
            "val_zh": "",
        })

    for m in CS_ALIAS_STR_RE.finditer(cleaned):
        key_name = m.group(1)
        rhs = m.group(2).strip()
        parts = rhs.split(".")
        if len(parts) < 2:
            continue
        ref_cls, ref_key = parts[-2], parts[-1]
        if ref_cls.lower() in ("string", "environment", "path", "guid", "datetime", "cultureinfo"):
            continue
        entries.append({
            "assembly": assembly_name,
            "res_class": _owner_class(m.start()),
            "key": key_name,
            "val_en": f"{{x:Static {ref_cls}.{ref_key}}}",
            "val_zh": "",
        })

    for m in ROUTED_UI_CMD_RE.finditer(cleaned):
        cmd_text, cmd_name = unescape_csharp_string(m.group(1)), m.group(2)
        cls_name = _owner_class(m.start())
        entries.append({
            "assembly": assembly_name,
            "res_class": cls_name,
            "key": cmd_name,
            "val_en": cmd_text,
            "val_zh": "",
        })
        entries.append({
            "assembly": assembly_name,
            "res_class": cls_name,
            "key": f"{cmd_name}Command",
            "val_en": cmd_text,
            "val_zh": "",
        })

    for m in ROUTED_UI_CMD_RES_RE.finditer(cleaned):
        res_ref, cmd_name = m.group(1), m.group(2)
        cls_name = _owner_class(m.start())
        entries.append({
            "assembly": assembly_name,
            "res_class": cls_name,
            "key": cmd_name,
            "val_en": f"{{x:Static {res_ref}}}",
            "val_zh": "",
        })
        entries.append({
            "assembly": assembly_name,
            "res_class": cls_name,
            "key": f"{cmd_name}Command",
            "val_en": f"{{x:Static {res_ref}}}",
            "val_zh": "",
        })
    return entries


def parse_default_config_xml(xml_text: str, rel_path: str) -> List[Dict[str, str]]:
    """Extract concise scalar attributes/leaves from shipping `.config` / `.xml` files."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    results: List[Dict[str, str]] = []

    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    def walk(elem: ET.Element, path_prefix: str, depth: int) -> None:
        if depth > 5 or len(results) >= 120:
            return
        tname = strip_ns(elem.tag)
        cur_path = f"{path_prefix}/{tname}" if path_prefix else tname
        for ak, av in sorted(elem.attrib.items()):
            ak_clean = strip_ns(ak)
            if av and len(av) <= 120 and not ak_clean.startswith("xmlns"):
                results.append({
                    "rel_path": rel_path,
                    "xpath_key": f"{cur_path}/@{ak_clean}",
                    "value": av.strip(),
                })
        txt = (elem.text or "").strip()
        if txt and not list(elem) and len(txt) <= 120:
            results.append({
                "rel_path": rel_path,
                "xpath_key": cur_path,
                "value": txt,
            })
        for child in elem:
            walk(child, cur_path, depth + 1)

    walk(root, "", 0)
    return results
