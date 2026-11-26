"""Pure-Python binary reader for .NET PE/ECMA-335 Metadata, .resources, and BAML2006.

Zero .NET reflection, zero Windows-only dependencies, 100% Python standard library.
Extracts:
- Assembly identity
- `[assembly: XmlnsDefinition(xmlUri, clrNamespace)]`
- Public/nested CLR type inheritance chains + `OnCreateAutomationPeer` overrides
- `[TemplatePart(Name = ..., Type = ...)]`
- Embedded `.g.resources` -> `*.baml` `Style`, `ControlTemplate`, and `DataTemplate` trees
  (enforcing Iron Law 1: `x:Uid` inside templates is ignored at runtime, while `Name` is kept).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import Any, Dict, List, Optional, Set, Tuple

from autowork_core.ui_inventory.scanner.resx_config_parser import (
    classify_locale_bucket,
    split_resx_stem_and_locale,
)
from autowork_core.ui_inventory.scanner.wpf_builtin_specs import (
    BAML_KNOWN_ELEMENTS,
    BAML_KNOWN_PROPERTIES,
)


@dataclass
class BinaryAssemblyMetadata:
    assembly_name: str
    xmlns_defs: List[Dict[str, str]] = field(default_factory=list)
    types: List[Dict[str, Any]] = field(default_factory=list)
    baml_definitions: List[Dict[str, Any]] = field(default_factory=list)
    baml_elements: List[Dict[str, Any]] = field(default_factory=list)
    baml_edges: List[Dict[str, Any]] = field(default_factory=list)
    strings: List[Dict[str, str]] = field(default_factory=list)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers: 7-bit integer & ECMA-335 compressed unsigned integer readers
# ---------------------------------------------------------------------------
def _read_7bit_int(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf) or shift >= 35:
            raise ValueError("Truncated or invalid 7-bit int")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos
        shift += 7


def _read_7bit_utf8_str(buf: bytes, pos: int) -> Tuple[str, int]:
    byte_len, pos = _read_7bit_int(buf, pos)
    end = pos + byte_len
    if end > len(buf):
        raise ValueError("Truncated BAML UTF-8 string")
    return buf[pos:end].decode("utf-8", errors="replace"), end


def _read_cli_compressed_uint(buf: bytes, pos: int) -> Tuple[Optional[int], int]:
    if pos >= len(buf):
        raise ValueError("Truncated CLI blob compressed uint")
    b0 = buf[pos]
    if b0 == 0xFF:
        return None, pos + 1
    if (b0 & 0x80) == 0:
        return b0, pos + 1
    if (b0 & 0xC0) == 0x80:
        if pos + 2 > len(buf):
            raise ValueError("Truncated 2-byte CLI compressed uint")
        return ((b0 & 0x3F) << 8) | buf[pos + 1], pos + 2
    if (b0 & 0xE0) == 0xC0:
        if pos + 4 > len(buf):
            raise ValueError("Truncated 4-byte CLI compressed uint")
        return ((b0 & 0x1F) << 24) | (buf[pos + 1] << 16) | (buf[pos + 2] << 8) | buf[pos + 3], pos + 4
    raise ValueError(f"Invalid CLI compressed uint prefix {b0:#x}")


def _read_cli_ser_string(buf: bytes, pos: int) -> Tuple[Optional[str], int]:
    length, pos = _read_cli_compressed_uint(buf, pos)
    if length is None:
        return None, pos
    end = pos + length
    if end > len(buf):
        raise ValueError("Truncated CLI SerString")
    return buf[pos:end].decode("utf-8", errors="replace"), end


def _get_heap_string(strings_heap: bytes, idx: int) -> str:
    if idx <= 0 or idx >= len(strings_heap):
        return ""
    end = strings_heap.find(b"\x00", idx)
    if end == -1:
        end = len(strings_heap)
    return strings_heap[idx:end].decode("utf-8", errors="replace")


def _get_heap_blob(blob_heap: bytes, idx: int) -> bytes:
    if idx <= 0 or idx >= len(blob_heap):
        return b""
    length, pos = _read_cli_compressed_uint(blob_heap, idx)
    if length is None or pos + length > len(blob_heap):
        return b""
    return blob_heap[pos : pos + length]


# ---------------------------------------------------------------------------
# ECMA-335 PE & Metadata Table Reader
# ---------------------------------------------------------------------------
def parse_managed_dll(data: bytes, file_label: str) -> Optional[BinaryAssemblyMetadata]:
    """Parse a .NET managed assembly from raw bytes; returns None if native/non-.NET PE."""
    if len(data) < 0x100 or data[:2] != b"MZ":
        return None
    try:
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
            return None
        coff_off = e_lfanew + 4
        _, num_sections, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, coff_off)
        opt_off = coff_off + 20
        if opt_off + opt_size > len(data) or opt_size < 96:
            return None
        magic = struct.unpack_from("<H", data, opt_off)[0]
        if magic == 0x10B:  # PE32
            dd_off = opt_off + 96
        elif magic == 0x20B:  # PE32+
            dd_off = opt_off + 112
        else:
            return None
        if dd_off + 15 * 8 > opt_off + opt_size:
            return None
        cli_rva, cli_size = struct.unpack_from("<II", data, dd_off + 14 * 8)
        if cli_rva == 0 or cli_size < 72:
            return None  # Native PE (not .NET)

        sec_off = opt_off + opt_size
        sections: List[Tuple[int, int, int]] = []
        for i in range(num_sections):
            s_pos = sec_off + i * 40
            if s_pos + 40 > len(data):
                break
            _, vsize, vrva, rsize, roff = struct.unpack_from("<8sIIII", data, s_pos)
            sections.append((vrva, max(vsize, rsize), roff))

        def rva_to_offset(rva: int) -> int:
            for vrva, span, roff in sections:
                if vrva <= rva < vrva + span:
                    off = roff + (rva - vrva)
                    if off < len(data):
                        return off
            raise ValueError(f"Unmapped RVA {rva:#x}")

        cli_off = rva_to_offset(cli_rva)
        meta_rva, meta_size, _, _, res_rva, res_size = struct.unpack_from("<IIIIII", data, cli_off + 8)
        if meta_rva == 0 or meta_size < 32:
            return None
        meta_off = rva_to_offset(meta_rva)
        if data[meta_off : meta_off + 4] != b"BSJB":
            return None

        ver_len = struct.unpack_from("<I", data, meta_off + 12)[0]
        ver_aligned = (ver_len + 3) & ~3
        stm_hdr_pos = meta_off + 16 + ver_aligned
        _, num_streams = struct.unpack_from("<HH", data, stm_hdr_pos)
        pos = stm_hdr_pos + 4

        streams: Dict[str, bytes] = {}
        for _ in range(num_streams):
            s_off, s_size = struct.unpack_from("<II", data, pos)
            pos += 8
            name_end = data.find(b"\x00", pos)
            if name_end == -1:
                raise ValueError("Unterminated metadata stream name")
            s_name = data[pos:name_end].decode("ascii", errors="replace")
            name_len_aligned = ((name_end - pos + 1) + 3) & ~3
            pos += name_len_aligned
            streams[s_name] = data[meta_off + s_off : meta_off + s_off + s_size]

        tables_stream = streams.get("#~") or streams.get("#-")
        strings_heap = streams.get("#Strings", b"")
        blob_heap = streams.get("#Blob", b"")
        if not tables_stream or len(tables_stream) < 24:
            return None

        _, _, _, heap_sizes, _, valid_mask, _ = struct.unpack_from("<IBBBBQQ", tables_stream, 0)
        tpos = 24
        row_counts = [0] * 64
        present_tables: List[int] = []
        for tid in range(64):
            if valid_mask & (1 << tid):
                row_counts[tid] = struct.unpack_from("<I", tables_stream, tpos)[0]
                tpos += 4
                present_tables.append(tid)

        str_sz = 4 if (heap_sizes & 0x01) else 2
        guid_sz = 4 if (heap_sizes & 0x02) else 2
        blob_sz = 4 if (heap_sizes & 0x04) else 2

        def tbl_sz(tid: int) -> int:
            return 4 if row_counts[tid] >= 65536 else 2

        def coded_sz(tag_bits: int, tids: Tuple[int, ...]) -> int:
            max_r = max((row_counts[t] for t in tids if 0 <= t < 64), default=0)
            return 4 if max_r >= (1 << (16 - tag_bits)) else 2

        def read_idx(buf: bytes, p: int, sz: int) -> Tuple[int, int]:
            if sz == 2:
                return struct.unpack_from("<H", buf, p)[0], p + 2
            return struct.unpack_from("<I", buf, p)[0], p + 4

        td_or_ref_tids = (0x02, 0x01, 0x1B)
        td_or_ref_sz = coded_sz(2, td_or_ref_tids)
        res_scope_sz = coded_sz(2, (0x00, 0x1A, 0x23, 0x01))
        mr_parent_tids = (0x02, 0x01, 0x1A, 0x06, 0x1B)
        mr_parent_sz = coded_sz(3, mr_parent_tids)
        has_const_sz = coded_sz(2, (0x04, 0x08, 0x17))
        has_ca_tids = (
            0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00,
            0x0E, 0x17, 0x14, 0x11, 0x1A, 0x1B, 0x20, 0x23,
            0x26, 0x27, 0x28, 0x2A, 0x2B, 0x2C,
        )
        has_ca_sz = coded_sz(5, has_ca_tids)
        ca_type_tids = (0xFF, 0xFF, 0x06, 0x0A, 0xFF)
        ca_type_sz = coded_sz(3, (0x06, 0x0A))
        has_fm_sz = coded_sz(1, (0x04, 0x08))
        has_ds_sz = coded_sz(2, (0x02, 0x06, 0x20))
        has_sem_sz = coded_sz(1, (0x14, 0x17))
        md_or_ref_sz = coded_sz(1, (0x06, 0x0A))
        mem_fwd_sz = coded_sz(1, (0x04, 0x06))
        impl_tids = (0x26, 0x23, 0x27)
        impl_sz = coded_sz(2, impl_tids)
        tom_sz = coded_sz(1, (0x02, 0x06))

        row_widths: Dict[int, int] = {
            0x00: 2 + str_sz + 3 * guid_sz,
            0x01: res_scope_sz + 2 * str_sz,
            0x02: 4 + 2 * str_sz + td_or_ref_sz + tbl_sz(0x04) + tbl_sz(0x06),
            0x03: tbl_sz(0x04),
            0x04: 2 + str_sz + blob_sz,
            0x05: tbl_sz(0x06),
            0x06: 8 + str_sz + blob_sz + tbl_sz(0x08),
            0x07: tbl_sz(0x08),
            0x08: 4 + str_sz,
            0x09: tbl_sz(0x02) + td_or_ref_sz,
            0x0A: mr_parent_sz + str_sz + blob_sz,
            0x0B: 2 + has_const_sz + blob_sz,
            0x0C: has_ca_sz + ca_type_sz + blob_sz,
            0x0D: has_fm_sz + blob_sz,
            0x0E: 2 + has_ds_sz + blob_sz,
            0x0F: 6 + tbl_sz(0x02),
            0x10: 4 + tbl_sz(0x04),
            0x11: blob_sz,
            0x12: tbl_sz(0x02) + tbl_sz(0x14),
            0x13: tbl_sz(0x14),
            0x14: 2 + str_sz + td_or_ref_sz,
            0x15: tbl_sz(0x02) + tbl_sz(0x17),
            0x16: tbl_sz(0x17),
            0x17: 2 + str_sz + blob_sz,
            0x18: 2 + tbl_sz(0x06) + has_sem_sz,
            0x19: tbl_sz(0x02) + 2 * md_or_ref_sz,
            0x1A: str_sz,
            0x1B: blob_sz,
            0x1C: 2 + mem_fwd_sz + str_sz + tbl_sz(0x1A),
            0x1D: 4 + tbl_sz(0x04),
            0x1E: 8,
            0x1F: 4,
            0x20: 16 + blob_sz + 2 * str_sz,
            0x21: 4,
            0x22: 12,
            0x23: 12 + 2 * blob_sz + 2 * str_sz,
            0x24: 4 + tbl_sz(0x23),
            0x25: 12 + tbl_sz(0x23),
            0x26: 4 + str_sz + blob_sz,
            0x27: 8 + 2 * str_sz + impl_sz,
            0x28: 8 + str_sz + impl_sz,
            0x29: 2 * tbl_sz(0x02),
            0x2A: 4 + tom_sz + str_sz,
            0x2B: md_or_ref_sz + blob_sz,
            0x2C: tbl_sz(0x2A) + td_or_ref_sz,
        }

        table_offsets: Dict[int, int] = {}
        cur = tpos
        for tid in present_tables:
            if tid not in row_widths:
                break
            table_offsets[tid] = cur
            cur += row_counts[tid] * row_widths[tid]

        # 1. Assembly Name (0x20)
        asm_name = file_label.rsplit("/", 1)[-1]
        if asm_name.lower().endswith(".dll") or asm_name.lower().endswith(".exe"):
            asm_name = asm_name[:-4]
        if 0x20 in table_offsets and row_counts[0x20] >= 1:
            p = table_offsets[0x20] + 16 + blob_sz
            name_idx, _ = read_idx(tables_stream, p, str_sz)
            parsed_asm = _get_heap_string(strings_heap, name_idx)
            if parsed_asm:
                asm_name = parsed_asm

        meta = BinaryAssemblyMetadata(assembly_name=asm_name)

        # 2. TypeRef (0x01): 1-based list of full_name
        type_refs: List[str] = [""]
        if 0x01 in table_offsets:
            p = table_offsets[0x01]
            w = row_widths[0x01]
            for i in range(row_counts[0x01]):
                rp = p + i * w + res_scope_sz
                n_idx, rp = read_idx(tables_stream, rp, str_sz)
                ns_idx, _ = read_idx(tables_stream, rp, str_sz)
                t_name = _get_heap_string(strings_heap, n_idx)
                t_ns = _get_heap_string(strings_heap, ns_idx)
                type_refs.append(f"{t_ns}.{t_name}" if t_ns else t_name)

        # 3. NestedClass (0x29): child_typedef_row -> enclosing_typedef_row
        nested_enclosing: Dict[int, int] = {}
        if 0x29 in table_offsets:
            p = table_offsets[0x29]
            w = row_widths[0x29]
            td_idx_sz = tbl_sz(0x02)
            for i in range(row_counts[0x29]):
                rp = p + i * w
                nested_row, rp = read_idx(tables_stream, rp, td_idx_sz)
                encl_row, _ = read_idx(tables_stream, rp, td_idx_sz)
                nested_enclosing[nested_row] = encl_row

        # 4. MethodDef (0x06): 1-based list of method names
        method_names: List[str] = [""]
        if 0x06 in table_offsets:
            p = table_offsets[0x06]
            w = row_widths[0x06]
            for i in range(row_counts[0x06]):
                rp = p + i * w + 8
                m_idx, _ = read_idx(tables_stream, rp, str_sz)
                method_names.append(_get_heap_string(strings_heap, m_idx))

        # 5. TypeDef (0x02)
        raw_typedefs: List[Tuple[int, str, str, int, int]] = [
            (0, "", "", 0, 1)
        ]  # (flags, ns, name, extends_coded, method_start)
        if 0x02 in table_offsets:
            p = table_offsets[0x02]
            w = row_widths[0x02]
            fld_sz = tbl_sz(0x04)
            mth_sz = tbl_sz(0x06)
            for i in range(row_counts[0x02]):
                rp = p + i * w
                flags = struct.unpack_from("<I", tables_stream, rp)[0]
                rp += 4
                n_idx, rp = read_idx(tables_stream, rp, str_sz)
                ns_idx, rp = read_idx(tables_stream, rp, str_sz)
                ext_coded, rp = read_idx(tables_stream, rp, td_or_ref_sz)
                rp += fld_sz
                m_start, _ = read_idx(tables_stream, rp, mth_sz)
                t_name = _get_heap_string(strings_heap, n_idx)
                t_ns = _get_heap_string(strings_heap, ns_idx)
                raw_typedefs.append((flags, t_ns, t_name, ext_coded, m_start))

        def full_typedef_name(row_idx: int, depth: int = 0) -> str:
            if row_idx <= 0 or row_idx >= len(raw_typedefs) or depth > 8:
                return ""
            _, ns, name, _, _ = raw_typedefs[row_idx]
            if "`" in name:
                name = name.split("`", 1)[0]
            if row_idx in nested_enclosing:
                parent = full_typedef_name(nested_enclosing[row_idx], depth + 1)
                return f"{parent}.{name}" if parent else name
            return f"{ns}.{name}" if ns else name

        def resolve_td_or_ref(coded: int) -> Optional[str]:
            if coded == 0:
                return None
            tag = coded & 0x03
            row = coded >> 2
            if tag == 0:  # TypeDef
                return full_typedef_name(row) or None
            if tag == 1:  # TypeRef
                if 0 < row < len(type_refs):
                    name = type_refs[row]
                    return name.split("`", 1)[0] if "`" in name else name
            return None

        total_methods = row_counts[0x06]
        for row_idx in range(1, len(raw_typedefs)):
            flags, _, raw_name, ext_coded, m_start = raw_typedefs[row_idx]
            if not raw_name or raw_name.startswith("<"):
                continue
            vis = flags & 0x07
            # 1 = Public, 2 = NestedPublic
            if vis not in (1, 2):
                continue
            m_end = raw_typedefs[row_idx + 1][4] if row_idx + 1 < len(raw_typedefs) else (total_methods + 1)
            has_peer = any(
                method_names[m] == "OnCreateAutomationPeer"
                for m in range(max(1, m_start), min(len(method_names), m_end))
            )
            full_name = full_typedef_name(row_idx)
            base_name = resolve_td_or_ref(ext_coded)
            if full_name and base_name:
                meta.types.append({
                    "assembly": asm_name,
                    "full_name": full_name,
                    "base_full_name": base_name,
                    "has_custom_peer": has_peer,
                    "source_kind": "Dll",
                })

        # 6. MemberRef (0x0A) -> map ctor row to parent type full name (for CustomAttribute decoding)
        member_ref_type: List[str] = [""]
        if 0x0A in table_offsets:
            p = table_offsets[0x0A]
            w = row_widths[0x0A]
            for i in range(row_counts[0x0A]):
                rp = p + i * w
                parent_coded, _ = read_idx(tables_stream, rp, mr_parent_sz)
                tag = parent_coded & 0x07
                row = parent_coded >> 3
                owner_type = ""
                if tag == 0:
                    owner_type = full_typedef_name(row)
                elif tag == 1 and 0 < row < len(type_refs):
                    owner_type = type_refs[row]
                member_ref_type.append(owner_type)

        # Map MethodDef row -> owning TypeDef full name as well
        method_owner_typedef: Dict[int, str] = {}
        for row_idx in range(1, len(raw_typedefs)):
            m_start = raw_typedefs[row_idx][4]
            m_end = raw_typedefs[row_idx + 1][4] if row_idx + 1 < len(raw_typedefs) else (total_methods + 1)
            td_full = full_typedef_name(row_idx)
            for m in range(max(1, m_start), min(total_methods + 1, m_end)):
                method_owner_typedef[m] = td_full

        # 7. CustomAttribute (0x0C): extract [assembly: XmlnsDefinition(uri, clrNs)]
        if 0x0C in table_offsets:
            p = table_offsets[0x0C]
            w = row_widths[0x0C]
            seen_xmlns: Set[Tuple[str, str]] = set()
            for i in range(row_counts[0x0C]):
                rp = p + i * w
                parent_coded, rp = read_idx(tables_stream, rp, has_ca_sz)
                ca_type_coded, rp = read_idx(tables_stream, rp, ca_type_sz)
                val_idx, _ = read_idx(tables_stream, rp, blob_sz)

                ca_tag = ca_type_coded & 0x07
                ca_row = ca_type_coded >> 3
                attr_type = ""
                if ca_tag == 2:  # MethodDef
                    attr_type = method_owner_typedef.get(ca_row, "")
                elif ca_tag == 3 and 0 < ca_row < len(member_ref_type):  # MemberRef
                    attr_type = member_ref_type[ca_row]

                if attr_type.endswith("XmlnsDefinitionAttribute"):
                    blob = _get_heap_blob(blob_heap, val_idx)
                    if len(blob) >= 4 and struct.unpack_from("<H", blob, 0)[0] == 0x0001:
                        try:
                            xml_uri, bpos = _read_cli_ser_string(blob, 2)
                            clr_ns, _ = _read_cli_ser_string(blob, bpos)
                            if xml_uri and clr_ns and (xml_uri, clr_ns) not in seen_xmlns:
                                seen_xmlns.add((xml_uri, clr_ns))
                                meta.xmlns_defs.append({
                                    "assembly": asm_name,
                                    "xml_uri": xml_uri,
                                    "clr_namespace": clr_ns,
                                })
                        except ValueError:
                            pass

        # 8. ManifestResource (0x28): extract embedded `*.g.resources` (BAML) & `*.resources` (string tables)
        if 0x28 in table_offsets and res_rva > 0 and res_size > 0:
            res_base_off = rva_to_offset(res_rva)
            p = table_offsets[0x28]
            w = row_widths[0x28]
            for i in range(row_counts[0x28]):
                rp = p + i * w
                mr_offset, _ = struct.unpack_from("<II", tables_stream, rp)
                rp += 8
                mr_name_idx, rp = read_idx(tables_stream, rp, str_sz)
                mr_impl, _ = read_idx(tables_stream, rp, impl_sz)
                if mr_impl != 0:
                    continue  # External resource, not embedded in this PE
                mr_name = _get_heap_string(strings_heap, mr_name_idx)
                mr_lower = mr_name.lower()
                if not mr_lower.endswith(".resources"):
                    continue
                abs_res_off = res_base_off + mr_offset
                if abs_res_off + 4 <= len(data):
                    stream_len = struct.unpack_from("<I", data, abs_res_off)[0]
                    res_bytes = data[abs_res_off + 4 : abs_res_off + 4 + stream_len]
                    if mr_lower.endswith(".g.resources"):
                        _extract_g_resources_baml(res_bytes, asm_name, file_label, meta)
                    else:
                        str_entries = parse_dot_resources_strings(res_bytes)
                        if str_entries:
                            stem = mr_name[: -len(".resources")]
                            res_cls_full, locale = split_resx_stem_and_locale(stem)
                            res_cls = res_cls_full.rsplit(".", 1)[-1] or res_cls_full
                            bucket = classify_locale_bucket(locale, file_label)
                            if bucket in ("en", "zh"):
                                clean_asm = asm_name[:-10] if asm_name.lower().endswith(".resources") else asm_name
                                for sk, sv in str_entries.items():
                                    meta.strings.append({
                                        "assembly": clean_asm,
                                        "res_class": res_cls,
                                        "key": sk,
                                        "val_en": sv if bucket == "en" else "",
                                        "val_zh": sv if bucket == "zh" else "",
                                    })

        return meta
    except Exception as exc:  # Fault-isolated per DLL
        diag_meta = BinaryAssemblyMetadata(assembly_name=file_label.rsplit("/", 1)[-1])
        diag_meta.diagnostics.append({
            "code": "PE_METADATA_SKIP",
            "severity": "Warn",
            "file": file_label,
            "line": 0,
            "message": f"Skipped binary metadata parse: {type(exc).__name__}: {exc}",
        })
        return diag_meta


# ---------------------------------------------------------------------------
# .NET ResourceReader (0xBEEFCACE) & MSBAML (BAML2006) Stream Parser
# ---------------------------------------------------------------------------
def parse_dot_resources_strings(res_stream: bytes) -> Dict[str, str]:
    """Parse all `System.String` entries from a compiled .NET `.resources` (`0xBEEFCACE`) stream (v1 & v2)."""
    if len(res_stream) < 28:
        return {}
    try:
        magic, _, hdr_skip = struct.unpack_from("<III", res_stream, 0)
        if magic != 0xBEEFCACE:
            return {}
        pos = 12 + hdr_skip
        if pos + 12 > len(res_stream):
            return {}
        ver, num_res, num_types = struct.unpack_from("<iii", res_stream, pos)
        pos += 12
        if num_res <= 0 or num_types < 0 or ver not in (1, 2):
            return {}
        type_names: List[str] = []
        for _ in range(num_types):
            tname, pos = _read_7bit_utf8_str(res_stream, pos)
            type_names.append(tname)
        if pos % 8:
            pos += 8 - (pos % 8)
        pos += num_res * 4  # skip name hashes
        if pos + num_res * 4 + 4 > len(res_stream):
            return {}
        name_positions = struct.unpack_from(f"<{num_res}i", res_stream, pos)
        pos += num_res * 4
        data_sec_off = struct.unpack_from("<i", res_stream, pos)[0]
        pos += 4
        name_sec_off = pos
        if data_sec_off < 0 or data_sec_off > len(res_stream):
            return {}

        out: Dict[str, str] = {}
        for np in name_positions:
            p = name_sec_off + np
            if p < name_sec_off or p >= len(res_stream):
                continue
            byte_len, p = _read_7bit_int(res_stream, p)
            if byte_len <= 0 or p + byte_len + 4 > len(res_stream):
                continue
            key_name = res_stream[p : p + byte_len].decode("utf-16-le", errors="replace").strip()
            p += byte_len
            if not key_name:
                continue
            doff = struct.unpack_from("<i", res_stream, p)[0]
            dp = data_sec_off + doff
            if dp < data_sec_off or dp >= len(res_stream):
                continue
            type_code, dp = _read_7bit_int(res_stream, dp)
            if ver == 2:
                if type_code == 0x01:  # ResourceTypeCode.String
                    val, _ = _read_7bit_utf8_str(res_stream, dp)
                    out[key_name] = val
            elif ver == 1:
                if 0 <= type_code < len(type_names) and type_names[type_code].startswith("System.String"):
                    val, _ = _read_7bit_utf8_str(res_stream, dp)
                    out[key_name] = val
        return out
    except (ValueError, struct.error):
        return {}


def _extract_g_resources_baml(
    res_stream: bytes,
    assembly_name: str,
    file_label: str,
    meta: BinaryAssemblyMetadata,
) -> None:
    if len(res_stream) < 28:
        return
    magic, _, hdr_skip = struct.unpack_from("<III", res_stream, 0)
    if magic != 0xBEEFCACE:
        return
    pos = 12 + hdr_skip
    if pos + 12 > len(res_stream):
        return
    ver, num_res, num_types = struct.unpack_from("<iii", res_stream, pos)
    pos += 12
    if num_res <= 0 or num_types < 0:
        return
    for _ in range(num_types):
        slen, pos = _read_7bit_int(res_stream, pos)
        pos += slen
    if pos % 8:
        pos += 8 - (pos % 8)
    pos += num_res * 4  # skip hashes
    if pos + num_res * 4 + 4 > len(res_stream):
        return
    name_positions = struct.unpack_from(f"<{num_res}i", res_stream, pos)
    pos += num_res * 4
    data_sec_off = struct.unpack_from("<i", res_stream, pos)[0]
    pos += 4
    name_sec_off = pos

    entries: List[Tuple[str, bytes]] = []
    for np in name_positions:
        p = name_sec_off + np
        if p >= len(res_stream):
            continue
        byte_len, p = _read_7bit_int(res_stream, p)
        key_name = res_stream[p : p + byte_len].decode("utf-16-le", errors="replace")
        p += byte_len
        if not key_name.lower().endswith(".baml"):
            continue
        # Only parse theme / style / generic / control BAML streams (skip pure vector icon paths)
        key_lower = key_name.lower()
        if "/icons/" in f"/{key_lower}" or key_lower.startswith("icons/"):
            continue
        if p + 4 > len(res_stream):
            continue
        doff = struct.unpack_from("<i", res_stream, p)[0]
        dp = data_sec_off + doff
        if dp >= len(res_stream):
            continue
        type_code, dp = _read_7bit_int(res_stream, dp)
        if ver == 2:
            if type_code in (0x20, 0x21) and dp + 4 <= len(res_stream):
                blen = struct.unpack_from("<i", res_stream, dp)[0]
                dp += 4
                entries.append((key_name, res_stream[dp : dp + blen]))
        elif ver == 1:
            # ResourceReader v1 stores stream length directly when types[type_code] is MemoryStream/Byte[]
            if dp + 4 <= len(res_stream):
                blen = struct.unpack_from("<i", res_stream, dp)[0]
                if 0 < blen <= len(res_stream) - dp - 4:
                    entries.append((key_name, res_stream[dp + 4 : dp + 4 + blen]))

    for baml_key, baml_bytes in sorted(entries, key=lambda item: item[0]):
        try:
            _parse_baml_stream(baml_bytes, assembly_name, f"{file_label}!{baml_key}", meta)
        except Exception as exc:
            meta.diagnostics.append({
                "code": "BAML_PARTIAL_SKIP",
                "severity": "Info",
                "file": f"{file_label}!{baml_key}",
                "line": 0,
                "message": f"Skipped BAML stream tail: {type(exc).__name__}: {exc}",
            })


_ZERO_PAYLOAD_RECORDS = frozenset({2, 4, 8, 10, 12, 14, 26, 41, 42, 43, 49})
_FIXED_PAYLOAD_SIZES: Dict[int, int] = {
    1: 6,   # DocumentStart
    3: 3,   # ElementStart
    7: 2,   # PropertyComplexStart
    9: 2,   # PropertyArrayStart
    11: 2,  # PropertyIListStart
    13: 2,  # PropertyIDictionaryStart
    33: 4,  # PropertyStringReference
    34: 4,  # PropertyTypeReference
    35: 6,  # PropertyWithExtension
    37: 4,  # DeferableContentStart
    39: 9,  # DefAttributeKeyType
    40: 9,  # KeyElementStart
    44: 2,  # ConstructorParameterType
    45: 4,  # ConnectionId
    46: 2,  # ContentProperty
    48: 3,  # StaticResourceStart
    50: 2,  # StaticResourceId
    53: 8,  # LineNumberAndPosition
    54: 4,  # LinePosition
    55: 3,  # OptimizedStaticResource
    56: 4,  # PropertyWithStaticResourceId
}


@dataclass
class _BamlNode:
    type_name: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List["_BamlNode"] = field(default_factory=list)
    is_key_tree: bool = False


def _parse_baml_stream(
    baml: bytes,
    assembly_name: str,
    baml_source_id: str,
    meta: BinaryAssemblyMetadata,
) -> None:
    if len(baml) < 28:
        return
    fmt_len = struct.unpack_from("<I", baml, 0)[0]
    if fmt_len != 12 or baml[4:16].decode("utf-16-le", errors="ignore") != "MSBAML":
        return

    pos = 28
    type_table: Dict[int, str] = {}
    attr_table: Dict[int, str] = {}
    str_table: Dict[int, str] = {-1: "Name", -2: "Uid"}

    def resolve_type(tid: int) -> str:
        if tid < 0:
            return BAML_KNOWN_ELEMENTS.get(-tid, f"KnownElement_{-tid}")
        return type_table.get(tid, f"Type_{tid}")

    def resolve_attr(aid: int) -> str:
        if aid < 0:
            kp = BAML_KNOWN_PROPERTIES.get(-aid, f"KnownProp_{-aid}")
            return kp.rsplit(".", 1)[-1] if "." in kp else kp
        return attr_table.get(aid, f"Attr_{aid}")

    def resolve_str(sid: int) -> str:
        return str_table.get(sid, "")

    elem_stack: List[_BamlNode] = []
    prop_stack: List[Optional[str]] = []
    roots: List[_BamlNode] = []
    pending_dict_key: Optional[str] = None

    while pos < len(baml):
        rec_type = baml[pos]
        pos += 1
        if rec_type == 0:
            break

        if rec_type in _ZERO_PAYLOAD_RECORDS:
            if rec_type in (4, 41):  # ElementEnd, KeyElementEnd
                if elem_stack:
                    finished = elem_stack.pop()
                    if finished.is_key_tree:
                        if finished.type_name in ("TypeExtension", "System.Windows.Markup.TypeExtension"):
                            pending_dict_key = finished.attrs.get("_text") or finished.attrs.get("TypeName")
                    elif not elem_stack:
                        roots.append(finished)
            elif rec_type in (8, 10, 12, 14):  # PropertyComplexEnd etc.
                if prop_stack:
                    prop_stack.pop()
            continue

        if rec_type == 47:  # NamedElementStart (TypeId + 7bit string)
            tid = struct.unpack_from("<h", baml, pos)[0]
            pos += 2
            rname, pos = _read_7bit_utf8_str(baml, pos)
            node = _BamlNode(type_name=resolve_type(tid))
            if rname:
                node.attrs["Name"] = rname
            if pending_dict_key:
                node.attrs.setdefault("x:Key", pending_dict_key)
                pending_dict_key = None
            if elem_stack:
                elem_stack[-1].children.append(node)
            elem_stack.append(node)
            continue

        if rec_type in _FIXED_PAYLOAD_SIZES:
            sz = _FIXED_PAYLOAD_SIZES[rec_type]
            if pos + sz > len(baml):
                break
            chunk = baml[pos : pos + sz]
            pos += sz

            if rec_type == 3:  # ElementStart
                tid, flags = struct.unpack_from("<hB", chunk, 0)
                is_injected = (flags & 0x02) != 0
                node = _BamlNode(type_name=resolve_type(tid) if not is_injected else "_Injected")
                if pending_dict_key:
                    node.attrs.setdefault("x:Key", pending_dict_key)
                    pending_dict_key = None
                if elem_stack:
                    elem_stack[-1].children.append(node)
                elem_stack.append(node)
            elif rec_type in (7, 9, 11, 13):  # PropertyComplexStart etc.
                aid = struct.unpack_from("<h", chunk, 0)[0]
                prop_stack.append(resolve_attr(aid))
            elif rec_type == 33:  # PropertyStringReference
                aid, sid = struct.unpack_from("<hh", chunk, 0)
                if elem_stack:
                    elem_stack[-1].attrs[resolve_attr(aid)] = resolve_str(sid)
            elif rec_type == 34:  # PropertyTypeReference
                aid, tid = struct.unpack_from("<hh", chunk, 0)
                if elem_stack:
                    elem_stack[-1].attrs[resolve_attr(aid)] = resolve_type(tid)
            elif rec_type == 39:  # DefAttributeKeyType
                tid = struct.unpack_from("<h", chunk, 0)[0]
                pending_dict_key = resolve_type(tid)
            elif rec_type == 40:  # KeyElementStart
                tid = struct.unpack_from("<h", chunk, 0)[0]
                node = _BamlNode(type_name=resolve_type(tid), is_key_tree=True)
                elem_stack.append(node)
            continue

        # Variable-sized record: starts with 7-bit RecordSize (includes size bytes!)
        size_start = pos
        rec_size, payload_pos = _read_7bit_int(baml, pos)
        rec_end = size_start + rec_size
        if rec_end > len(baml) or rec_end < payload_pos:
            break
        pos = rec_end

        if rec_type == 28:  # AssemblyInfo
            pass
        elif rec_type in (29, 30):  # TypeInfo / TypeSerializerInfo
            tid, _ = struct.unpack_from("<hh", baml, payload_pos)
            tfull, _ = _read_7bit_utf8_str(baml, payload_pos + 4)
            type_table[tid] = tfull
        elif rec_type == 31:  # AttributeInfo
            aid, _ = struct.unpack_from("<hh", baml, payload_pos)
            aname, _ = _read_7bit_utf8_str(baml, payload_pos + 5)
            attr_table[aid] = aname
        elif rec_type == 32:  # StringInfo
            sid = struct.unpack_from("<h", baml, payload_pos)[0]
            sval, _ = _read_7bit_utf8_str(baml, payload_pos + 2)
            str_table[sid] = sval
        elif rec_type in (5, 36):  # Property / PropertyWithConverter
            aid = struct.unpack_from("<h", baml, payload_pos)[0]
            pval, _ = _read_7bit_utf8_str(baml, payload_pos + 2)
            if elem_stack:
                elem_stack[-1].attrs[resolve_attr(aid)] = pval
        elif rec_type == 25:  # DefAttribute (x:Key, x:Name, x:Uid)
            dval, p2 = _read_7bit_utf8_str(baml, payload_pos)
            nid = struct.unpack_from("<h", baml, p2)[0]
            dname = resolve_str(nid) or attr_table.get(nid, "")
            if elem_stack:
                if dname in ("Key", "x:Key"):
                    elem_stack[-1].attrs["x:Key"] = dval
                elif dname in ("Name", "x:Name"):
                    elem_stack[-1].attrs["x:Name"] = dval
                elif dname in ("Uid", "x:Uid"):
                    elem_stack[-1].attrs["x:Uid"] = dval
        elif rec_type == 38:  # DefAttributeKeyString
            vid = struct.unpack_from("<h", baml, payload_pos)[0]
            pending_dict_key = resolve_str(vid)
        elif rec_type in (16, 17):  # Text / TextWithConverter
            tval, _ = _read_7bit_utf8_str(baml, payload_pos)
            if elem_stack:
                elem_stack[-1].attrs["_text"] = tval

    for root_node in roots:
        _emit_baml_templates(root_node, assembly_name, baml_source_id, meta)


def _short_type(type_name: str) -> str:
    return type_name.rsplit(".", 1)[-1] if "." in type_name else type_name


def _emit_baml_templates(
    root_node: _BamlNode,
    assembly_name: str,
    baml_source_id: str,
    meta: BinaryAssemblyMetadata,
) -> None:
    """Extract Style(ControlTemplate) and ControlTemplate trees from parsed BAML nodes."""
    template_counter = 0

    def walk_dict(node: _BamlNode) -> None:
        nonlocal template_counter
        short = _short_type(node.type_name)
        if short == "_Injected":
            for ch in node.children:
                walk_dict(ch)
            return

        if short == "Style":
            target_type = node.attrs.get("TargetType") or node.attrs.get("x:Key") or ""
            style_key = node.attrs.get("x:Key") or target_type
            for sub in _iter_descendants(node):
                if _short_type(sub.type_name) == "ControlTemplate":
                    template_counter += 1
                    ct_target = sub.attrs.get("TargetType") or target_type
                    def_id = f"{assembly_name}::baml_template::{baml_source_id}#{template_counter}"
                    meta.baml_definitions.append({
                        "def_id": def_id,
                        "kind": "ControlTemplate",
                        "framework": "BAML",
                        "file": baml_source_id,
                        "target_type": ct_target,
                        "res_key": style_key,
                        "is_shipping": True,
                    })
                    _emit_template_elements(sub, def_id, baml_source_id, meta)
            return

        if short in ("ControlTemplate", "DataTemplate", "HierarchicalDataTemplate"):
            template_counter += 1
            target_type = node.attrs.get("TargetType") or node.attrs.get("DataType") or ""
            res_key = node.attrs.get("x:Key") or target_type
            kind = "ControlTemplate" if short == "ControlTemplate" else "DataTemplate"
            def_id = f"{assembly_name}::baml_template::{baml_source_id}#{template_counter}"
            meta.baml_definitions.append({
                "def_id": def_id,
                "kind": kind,
                "framework": "BAML",
                "file": baml_source_id,
                "target_type": target_type,
                "res_key": res_key,
                "is_shipping": True,
            })
            _emit_template_elements(node, def_id, baml_source_id, meta)
            return

        for ch in node.children:
            walk_dict(ch)

    walk_dict(root_node)


def _iter_descendants(node: _BamlNode) -> List[_BamlNode]:
    out: List[_BamlNode] = []
    for ch in node.children:
        out.append(ch)
        out.extend(_iter_descendants(ch))
    return out


_SKIP_TEMPLATE_SUBTREES = frozenset({
    "Setter", "Trigger", "DataTrigger", "MultiTrigger", "MultiDataTrigger",
    "EventTrigger", "Storyboard", "Condition", "ColumnDefinition", "RowDefinition",
})


def _emit_template_elements(
    template_root: _BamlNode,
    def_id: str,
    baml_source_id: str,
    meta: BinaryAssemblyMetadata,
) -> None:
    seq = 0

    def visit(node: _BamlNode, parent_id: Optional[str], sibling_order: int) -> None:
        nonlocal seq
        short = _short_type(node.type_name)
        if short in _SKIP_TEMPLATE_SUBTREES:
            return
        if short == "_Injected":
            for idx, ch in enumerate(node.children):
                visit(ch, parent_id, idx)
            return

        seq += 1
        elem_id = f"{def_id}::e{seq}"
        # Iron Law 1: Inside ControlTemplate / DataTemplate, WPF strips x:Uid at runtime!
        x_name = node.attrs.get("x:Name") or node.attrs.get("Name") or ""
        auto_id = node.attrs.get("AutomationId") or ""
        text_expr = (
            node.attrs.get("Content")
            or node.attrs.get("Header")
            or node.attrs.get("Text")
            or node.attrs.get("_text")
            or ""
        )
        meta.baml_elements.append({
            "elem_id": elem_id,
            "def_id": def_id,
            "parent_elem_id": parent_id,
            "xml_order": sibling_order,
            "context": "ControlTemplate",
            "clr_type": node.type_name,
            "attrs": {
                k: v
                for k, v in {
                    "explicit_auto_id": auto_id,
                    "x_uid": "",  # Explicitly stripped per Iron Law 1
                    "x_name": x_name,
                    "raw_text_expr": text_expr,
                    "raw_tooltip_expr": node.attrs.get("ToolTip", ""),
                    "is_editable": node.attrs.get("IsEditable", ""),
                    "is_readonly": node.attrs.get("IsReadOnly", ""),
                    "initial_vis": node.attrs.get("Visibility", ""),
                    "command_expr": node.attrs.get("Command", ""),
                }.items()
                if v
            },
            "grid_rc": [
                int(node.attrs["Row"]) if node.attrs.get("Row", "").isdigit() else None,
                int(node.attrs["Column"]) if node.attrs.get("Column", "").isdigit() else None,
            ],
            "line": 0,
        })
        child_order = 0
        for ch in node.children:
            visit(ch, elem_id, child_order)
            child_order += 1

    for idx, ch in enumerate(template_root.children):
        visit(ch, None, idx)
