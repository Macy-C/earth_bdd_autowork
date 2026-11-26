"""Zero-dependency standard-library ECMA-376 OpenXML `.xlsx` exporter (`xlsx_writer.py`).

Generates the 4-Sheet Pure Objective UI Inventory & Governance Audit workbook without `openpyxl`
(Zero pre-baked XPath — 100% aligned with `md_exporter.py` objective facts):
1. `1_UI_Controls_Catalog` (22-column full projected UIA facts, AutoId global counts, sibling order & contracts)
2. `2_Uid_Shadow_And_Conflicts` (`x:Uid != x:Name` shadowing & duplicate `x:Uid` defects)
3. `3_Missing_Identity_Gaps` (Missing/weak identity & Peer-less wrapper `x:Name` dissolution audit)
4. `4_Dynamic_And_Unmounted` (Dynamic polymorphic slots, external repo refs, & unmounted views)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any, Dict, List, Sequence, Tuple
from xml.sax.saxutils import escape
import zipfile

from autowork_core.ui_inventory.models import (
    WorkspaceIndex,
    classify_auto_id_quality,
    classify_surface_scope,
    compute_identity_counts,
    describe_semantic_ancestors,
    describe_uia_sibling_order,
    format_surface_host_fact,
    get_effective_display_text,
)
from autowork_core.ui_inventory.solver import is_weak_generic_auto_id


_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean_xml_str(val: Any) -> str:
    if val is None:
        return ""
    s = _ILLEGAL_XML_CHARS_RE.sub("", str(val))
    return s[:4000]


def _col_letter(col_1based: int) -> str:
    out: List[str] = []
    n = col_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


class _SharedStrings:
    def __init__(self) -> None:
        self._index: Dict[str, int] = {}
        self._items: List[str] = []
        self.total_refs = 0

    def intern(self, text: str) -> int:
        self.total_refs += 1
        existing = self._index.get(text)
        if existing is not None:
            return existing
        idx = len(self._items)
        self._items.append(text)
        self._index[text] = idx
        return idx

    def to_xml_bytes(self) -> bytes:
        parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'count="{self.total_refs}" uniqueCount="{len(self._items)}">',
        ]
        for s in self._items:
            esc = escape(s)
            if s != s.strip() or "\n" in s:
                parts.append(f'<si><t xml:space="preserve">{esc}</t></si>')
            else:
                parts.append(f"<si><t>{esc}</t></si>")
        parts.append("</sst>")
        return "".join(parts).encode("utf-8")


def _build_sheet_xml(
    headers: Sequence[str],
    rows: Sequence[Tuple[Sequence[Any], bool]],
    sst: _SharedStrings,
) -> bytes:
    num_cols = len(headers)
    col_max_widths = [max(10, min(60, len(h) + 4)) for h in headers]

    row_xml_chunks: List[str] = []

    # Header row (row 1, style s="1")
    hdr_cells: List[str] = []
    for c_idx, h_text in enumerate(headers, 1):
        ref = f"{_col_letter(c_idx)}1"
        s_id = sst.intern(_clean_xml_str(h_text))
        hdr_cells.append(f'<c r="{ref}" s="1" t="s"><v>{s_id}</v></c>')
    row_xml_chunks.append(f'<row r="1" ht="22" customHeight="1">{"".join(hdr_cells)}</row>')

    # Data rows (row 2..N, style s="2" if highlight_alert else s="0")
    for r_idx, (vals, is_alert) in enumerate(rows, 2):
        style_id = "2" if is_alert else "0"
        c_parts: List[str] = []
        for c_idx in range(1, num_cols + 1):
            raw_v = vals[c_idx - 1] if c_idx - 1 < len(vals) else ""
            if isinstance(raw_v, (int, float)) and not isinstance(raw_v, bool):
                c_parts.append(f'<c r="{_col_letter(c_idx)}{r_idx}" s="{style_id}"><v>{raw_v}</v></c>')
            else:
                s_clean = _clean_xml_str(raw_v)
                if not s_clean:
                    c_parts.append(f'<c r="{_col_letter(c_idx)}{r_idx}" s="{style_id}"/>')
                else:
                    if len(s_clean) + 2 > col_max_widths[c_idx - 1]:
                        col_max_widths[c_idx - 1] = min(65, len(s_clean) + 2)
                    s_id = sst.intern(s_clean)
                    c_parts.append(f'<c r="{_col_letter(c_idx)}{r_idx}" s="{style_id}" t="s"><v>{s_id}</v></c>')
        row_xml_chunks.append(f'<row r="{r_idx}">{"".join(c_parts)}</row>')

    cols_xml = "".join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(col_max_widths, 1)
    )
    last_col_letter = _col_letter(num_cols)
    total_rows = len(rows) + 1

    xml_doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols_xml}</cols>"
        f'<sheetData>{"".join(row_xml_chunks)}</sheetData>'
        f'<autoFilter ref="A1:{last_col_letter}{total_rows}"/>'
        "</worksheet>"
    )
    return xml_doc.encode("utf-8")


_STYLES_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="10"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFCE4D6"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFD9D9D9"/></left>
      <right style="thin"><color rgb="FFD9D9D9"/></right>
      <top style="thin"><color rgb="FFD9D9D9"/></top>
      <bottom style="thin"><color rgb="FFD9D9D9"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">
      <alignment vertical="top"/>
    </xf>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment vertical="top"/>
    </xf>
  </cellXfs>
</styleSheet>
"""


def write_audit_workbook(idx: WorkspaceIndex, out_xlsx: Path) -> Path:
    """Write the 4-Sheet Pure Objective UI Inventory & Governance Audit `.xlsx` atomically."""
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    tmp_xlsx = out_xlsx.with_suffix(".xlsx.tmp")

    counts = compute_identity_counts(idx)
    include_zh_col = counts.has_any_zh_text

    # 1. Sheet 1: 1_UI_Controls_Catalog (Pure Objective UIA Facts, ZERO pre-baked XPath)
    s1_headers = [
        "Module",
        "Surface_Name",
        "Surface_Kind",
        "Host_Window",
        "Panel_Group",
        "Ancestor_Chain",
        "Source_File_Line",
        "CLR_Type",
        "UIA_ControlType",
        "Effective_AutoId",
        "AutoId_Source",
        "AutoId_Surface_Count",
        "AutoId_Global_Count",
        "AutoId_Quality",
        "Dissolved_Wrapper",
        "Display_Text",
        *(["Display_Text_ZH"] if include_zh_col else []),
        "Name_Surface_Count",
        "Name_Global_Count",
        "HelpText",
        "UIA_Sibling_Order",
        "Binding_Or_Command",
        "Initial_State",
        "Preconditions",
        "Topology",
        "Action_Contract",
        "Scope_Fact",
    ]
    s1_rows: List[Tuple[Sequence[Any], bool]] = []
    for surf_id in sorted(idx.projected_surfaces):
        surf = idx.projected_surfaces[surf_id]
        scope_fact = classify_surface_scope(surf)
        host_fact = format_surface_host_fact(surf)
        for n in surf.interactive_nodes:
            solved = n.solved
            is_alert = bool(solved and solved.uses_index)
            eff_text, is_child_text = get_effective_display_text(n)
            disp_text = (f"[ChildText] {eff_text}" if (eff_text and is_child_text) else eff_text)
            bind_cmd = " | ".join(x for x in (n.binding_path, n.command_expr) if x)
            surf_aid_cnt = counts.surface_auto_id_instances.get((surf_id, n.uia_type, n.auto_id), 0) if n.auto_id else 0
            glob_aid_cnt = counts.global_auto_id_instances.get((n.uia_type, n.auto_id), 0) if n.auto_id else 0
            aid_quality = classify_auto_id_quality(
                n.auto_id,
                is_weak_generic_auto_id(n.auto_id),
                glob_aid_cnt,
                surf_aid_cnt,
            )
            surf_name_cnt = counts.surface_name_instances.get((surf_id, n.uia_type, eff_text), 0) if eff_text else 0
            glob_name_cnt = counts.global_name_instances.get((n.uia_type, eff_text), 0) if eff_text else 0

            if n.dissolved_wrapper_name:
                wrapper_desc = f"{n.dissolved_wrapper_type}({n.dissolved_wrapper_name})"
            elif n.dissolved_wrapper_type:
                wrapper_desc = n.dissolved_wrapper_type
            else:
                wrapper_desc = ""

            anc_chain = describe_semantic_ancestors(n, is_weak_generic_auto_id)
            sib_order = describe_uia_sibling_order(n) or (f"Adjacent('{n.adjacent_label}')" if n.adjacent_label else "")
            state_bits: List[str] = []
            if not n.is_initially_visible:
                state_bits.append("Collapsed/Hidden")
            if n.is_readonly:
                state_bits.append("ReadOnly")
            if n.type_source == "inferred_external":
                state_bits.append("ExternalTypeInferred")
            init_state = ", ".join(state_bits)

            row_vals: List[Any] = [
                f"{n.repo_id}::{n.assembly}",
                surf.surface_name,
                surf.surface_kind,
                host_fact,
                n.panel_group,
                anc_chain,
                f"{n.file}:{n.line}" if n.line else n.file,
                n.short_clr_type,
                n.uia_type,
                n.auto_id,
                n.auto_id_source,
                surf_aid_cnt,
                glob_aid_cnt,
                aid_quality,
                wrapper_desc,
                disp_text,
            ]
            if include_zh_col:
                row_vals.append(n.name_zh)
            row_vals.extend([
                surf_name_cnt,
                glob_name_cnt,
                n.help_text_en,
                sib_order,
                bind_cmd,
                init_state,
                " -> ".join(n.preconditions),
                n.topology,
                n.action_contract,
                scope_fact,
            ])
            s1_rows.append((row_vals, is_alert))

    # 2. Sheet 2: 2_Uid_Shadow_And_Conflicts
    s2_headers = [
        "Severity",
        "Issue_Kind",
        "Repo",
        "Assembly",
        "File",
        "Line",
        "Definition_ID",
        "CLR_Type",
        "x_Uid",
        "x_Name",
        "Effective_AutoId",
        "Recommendation",
    ]
    s2_rows: List[Tuple[Sequence[Any], bool]] = [
        (
            [
                iss.severity,
                iss.issue_kind,
                iss.repo_id,
                iss.assembly,
                iss.file,
                iss.line,
                iss.def_id,
                iss.clr_type,
                iss.x_uid,
                iss.x_name,
                iss.effective_auto_id,
                iss.recommendation,
            ],
            iss.severity == "High",
        )
        for iss in idx.uid_shadow_issues
    ]

    # 3. Sheet 3: 3_Missing_Identity_Gaps (12 pure-objective columns, ZERO pre-baked XPath)
    s3_headers = [
        "Severity",
        "Gap_Kind",
        "Repo",
        "Surface_Name",
        "File",
        "Line",
        "UIA_ControlType",
        "CLR_Type",
        "Effective_AutoId",
        "Dissolved_Wrapper_Name",
        "Adjacent_Label",
        "Recommendation",
    ]
    s3_rows: List[Tuple[Sequence[Any], bool]] = [
        (
            [
                gap.severity,
                gap.gap_kind,
                gap.repo_id,
                gap.surface_name,
                gap.file,
                gap.line,
                gap.uia_type,
                gap.clr_type,
                gap.effective_auto_id,
                gap.dissolved_wrapper_name,
                gap.adjacent_label,
                gap.recommendation,
            ],
            gap.severity == "High",
        )
        for gap in idx.identity_gap_issues
    ]

    # 4. Sheet 4: 4_Dynamic_And_Unmounted
    s4_headers = [
        "Category",
        "Repo",
        "Assembly",
        "File",
        "Source_Definition",
        "Slot_Or_Target",
        "Detail",
    ]
    s4_rows: List[Tuple[Sequence[Any], bool]] = [
        (
            [
                dm.category,
                dm.repo_id,
                dm.assembly,
                dm.file,
                dm.src_def_id,
                dm.slot_or_target,
                dm.detail,
            ],
            dm.category == "PENDING_EXTERNAL_TYPE",
        )
        for dm in idx.dynamic_unmounted_issues
    ]

    sheets_spec = [
        ("1_UI_Controls_Catalog", s1_headers, s1_rows),
        ("2_Uid_Shadow_And_Conflicts", s2_headers, s2_rows),
        ("3_Missing_Identity_Gaps", s3_headers, s3_rows),
        ("4_Dynamic_And_Unmounted", s4_headers, s4_rows),
    ]

    sst = _SharedStrings()
    sheet_bytes_list = [
        (sname, _build_sheet_xml(hdrs, rws, sst))
        for sname, hdrs, rws in sheets_spec
    ]

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheet_bytes_list) + 1)
        )
        + "</Types>"
    ).encode("utf-8")

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ).encode("utf-8")

    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        + "".join(
            f'<sheet name="{escape(sname)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, (sname, _) in enumerate(sheet_bytes_list, 1)
        )
        + "</sheets></workbook>"
    ).encode("utf-8")

    n_sheets = len(sheet_bytes_list)
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, n_sheets + 1)
        )
        + f'<Relationship Id="rId{n_sheets + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        + f'<Relationship Id="rId{n_sheets + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        + "</Relationships>"
    ).encode("utf-8")

    def _put(zf: zipfile.ZipFile, arcname: str, payload: bytes) -> None:
        zi = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, payload)

    with zipfile.ZipFile(tmp_xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _put(zf, "[Content_Types].xml", content_types)
        _put(zf, "_rels/.rels", root_rels)
        _put(zf, "xl/workbook.xml", wb_xml)
        _put(zf, "xl/_rels/workbook.xml.rels", wb_rels)
        _put(zf, "xl/styles.xml", _STYLES_XML)
        _put(zf, "xl/sharedStrings.xml", sst.to_xml_bytes())
        for i, (_, sbytes) in enumerate(sheet_bytes_list, 1):
            _put(zf, f"xl/worksheets/sheet{i}.xml", sbytes)

    try:
        tmp_xlsx.replace(out_xlsx)
        return out_xlsx
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = out_xlsx.with_name(f"{out_xlsx.stem}_{ts}{out_xlsx.suffix}")
        tmp_xlsx.replace(fallback)
        return fallback
