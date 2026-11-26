"""WeKnora `STATIC_UI_DOCS` Markdown exporter: Module-level aggregation, 100% Objective UIA Facts & 1150-char heading budget.

Designed specifically for WeKnora v0.8+ `Document` knowledge bases configured with:
- `strategy = "heading"`, `chunk_size = 1200`, `chunk_overlap = 0`
- `enable_parent_child = false`, `wiki_enabled = false`, `graph_enabled = false`
- Documents are aggregated by `(repo_id, assembly)` (regular modules under `<repo>/`,
  shared components under `共享组件/`) plus `00_全局窗口与模块路由总索引.md` at root.
- Pure Objective Facts Only (Zero pre-baked XPath): exports projected UIA control types,
  effective `@auto_id` with cross-workspace occurrence count & quality, dissolved wrapper info,
  bilingual texts, true UIA sibling order, topology, preconditions, and interaction contracts,
  preserving AI's contextual reasoning and selector synthesis.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil
from typing import Dict, List, Optional, Sequence, Set, Tuple

from autowork_core.knowledge.document_sync import HASH_LABEL, calculate_document_hash
from autowork_core.ui_inventory.models import (
    IdentityCountStats,
    ProjectedNode,
    ProjectedSurface,
    WorkspaceIndex,
    classify_auto_id_quality,
    compute_identity_counts,
    describe_semantic_ancestors,
    describe_uia_sibling_order,
    format_surface_host_fact,
    get_effective_display_text,
    is_special_action_contract,
)
from autowork_core.ui_inventory.solver import is_weak_generic_auto_id
from autowork_core.ui_inventory.store import safe_rmtree


MAX_CHUNK_CHARS = 1150
TARGET_SECTION_BUDGET = 1100
SHARED_FOLDER_NAME = "共享组件"
GLOBAL_INDEX_FILENAME = "00_全局窗口与模块路由总索引.md"

_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_GENERIC_KEYWORD_STOPWORDS = frozenset({
    "ok",
    "cancel",
    "close",
    "apply",
    "reset",
    "save",
    "yes",
    "no",
    "delete",
    "remove",
    "add",
    "edit",
    "browse",
    "help",
    "next",
    "back",
    "previous",
    "finish",
    "start",
    "stop",
    "clear",
    "refresh",
    "print",
    "select",
    "all",
    "none",
    "other",
    "default",
    "confirm",
    "确定",
    "取消",
    "关闭",
    "应用",
    "保存",
    "重置",
    "删除",
    "浏览",
    "帮助",
    "是",
    "否",
})


@dataclass
class ExportedMarkdownDoc:
    rel_path: str
    folder_path: str
    file_name: str
    surface_id: str
    sha256: str
    section_count: int
    max_section_chars: int


@dataclass
class MarkdownExportReport:
    out_dir: str
    total_docs: int = 0
    shared_component_docs: int = 0
    surface_docs: int = 0
    total_sections: int = 0
    max_observed_section_chars: int = 0
    docs: List[ExportedMarkdownDoc] = field(default_factory=list)


def _safe_filename_stem(raw: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", raw.strip()).strip(". ")
    return cleaned or "UnnamedSurface"


def _attach_document_hash(title: str, metadata_lines: Sequence[str], section_blocks: Sequence[str]) -> Tuple[str, str]:
    """Assemble `# Title` + `文档哈希（SHA256）：...` + metadata + `##` sections and compute exact SHA-256."""
    header_without_hash = "\n".join([f"# {title}", HASH_LABEL, *metadata_lines]).rstrip() + "\n\n"
    body = "\n\n".join(b.strip() for b in section_blocks if b.strip()) + "\n"
    draft = header_without_hash + body
    digest = calculate_document_hash(draft)
    final_header = "\n".join([f"# {title}", f"{HASH_LABEL}`{digest}`", *metadata_lines]).rstrip() + "\n\n"
    final_text = final_header + body
    return final_text, digest


def split_markdown_by_h2(md_text: str) -> List[str]:
    """Split Markdown document into `[preamble_before_first_h2, h2_block_1, h2_block_2, ...]` (outside code fences)."""
    lines = md_text.removeprefix("\ufeff").replace("\r\n", "\n").splitlines(keepends=True)
    chunks: List[List[str]] = [[]]
    in_fence: Optional[str] = None

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if in_fence is None:
                in_fence = marker
            elif in_fence == marker:
                in_fence = None
            chunks[-1].append(line)
            continue
        if in_fence is None and line.startswith("## "):
            chunks.append([line])
        else:
            chunks[-1].append(line)

    return ["".join(c).strip() for c in chunks if "".join(c).strip()]


def _pack_entries_into_h2_sections(
    section_title: str,
    context_banner: str,
    entries: Sequence[str],
    *,
    budget: int = TARGET_SECTION_BUDGET,
    hard_cap: int = MAX_CHUNK_CHARS,
) -> List[str]:
    """Pack logical entries into one or more `## <section_title> (i/N)` blocks strictly <= `hard_cap` chars."""
    if not entries:
        return []

    # Reserve worst-case numbering suffix ` (99/99)` (8 chars) when computing bin splits
    base_overhead = len(f"## {section_title} (99/99)\n{context_banner}\n\n")
    max_entry_len = max(120, min(budget, hard_cap) - base_overhead - 2)

    normalized_entries: List[str] = []
    for raw_e in entries:
        e = raw_e.strip()
        if not e:
            continue
        if len(e) > max_entry_len:
            e = e[: max_entry_len - 3].rstrip() + "..."
        normalized_entries.append(e)

    if not normalized_entries:
        return []

    bins: List[List[str]] = []
    cur_bin: List[str] = []
    cur_len = base_overhead

    for e in normalized_entries:
        add_len = len(e) + (1 if cur_bin else 0)
        if cur_bin and cur_len + add_len > budget:
            bins.append(cur_bin)
            cur_bin = [e]
            cur_len = base_overhead + len(e)
        else:
            cur_bin.append(e)
            cur_len += add_len

    if cur_bin:
        bins.append(cur_bin)

    total = len(bins)
    blocks: List[str] = []
    for idx_b, b_entries in enumerate(bins, 1):
        suffix = f" ({idx_b}/{total})" if total > 1 else ""
        head = f"## {section_title}{suffix}\n{context_banner}\n\n"
        block = head + "\n".join(b_entries)
        if len(block) > hard_cap:
            block = block[: hard_cap - 3].rstrip() + "..."
        blocks.append(block)
    return blocks


def _collect_folded_spinner_buttons(
    nodes: Sequence[ProjectedNode],
) -> Tuple[Set[str], Dict[str, List[str]]]:
    """Fold companion `RepeatButton` spinner arrows into their owning wrapper input (`Edit`/`ComboBox`/`Spinner`)."""
    by_wrapper: Dict[str, List[ProjectedNode]] = defaultdict(list)
    for n in nodes:
        if n.wrapper_instance_key:
            by_wrapper[n.wrapper_instance_key].append(n)

    folded_ids: Set[str] = set()
    owner_spinners: Dict[str, List[str]] = {}
    for group in by_wrapper.values():
        owners = [n for n in group if n.uia_type in ("Edit", "ComboBox", "Spinner")]
        spinners = [n for n in group if n.short_clr_type == "RepeatButton"]
        if not owners or not spinners:
            continue
        owner_id = owners[0].node_id
        labels: List[str] = []
        for sp in spinners:
            folded_ids.add(sp.node_id)
            lbl = sp.auto_id or sp.name_en or "RepeatButton"
            if lbl not in labels:
                labels.append(lbl)
        if labels:
            owner_spinners[owner_id] = labels
    return folded_ids, owner_spinners


def _extract_module_keywords(
    surfs: Sequence[ProjectedSurface],
    counts: IdentityCountStats,
    *,
    shared_def_ids: Optional[Set[str]] = None,
    limit: int = 7,
) -> List[str]:
    """Pick the most distinctive real UI visible texts in a module to bridge Feature wording -> module routing."""
    candidates: Dict[str, int] = {}
    for surf in surfs:
        for n in surf.interactive_nodes:
            if shared_def_ids and n.source_def_id in shared_def_ids:
                continue
            eff_txt, _ = get_effective_display_text(n)
            for raw in (eff_txt, n.help_text_en):
                t = " ".join(str(raw or "").split()).strip()
                if len(t) < 2 or len(t) > 32:
                    continue
                if t.lower() in _GENERIC_KEYWORD_STOPWORDS:
                    continue
                if re.fullmatch(r"[\d\W_]+", t):
                    continue
                g_freq = counts.global_name_instances.get((n.uia_type, t), 1)
                if t not in candidates or g_freq < candidates[t]:
                    candidates[t] = g_freq
    ranked = sorted(candidates.items(), key=lambda item: (item[1], len(item[0]), item[0].lower()))
    return [k for k, _ in ranked[:limit]]


def _format_control_entry(
    node: ProjectedNode,
    surface: ProjectedSurface,
    counts: IdentityCountStats,
    folded_spinners: Sequence[str] = (),
) -> str:
    """Format one interactive control purely as concise, high-signal objective projected UIA facts (ZERO pre-baked XPath)."""
    is_shared = surface.surface_kind == "SharedComponent"
    scope_prefix = "本组件" if is_shared else "本界面"

    id_parts: List[str] = []
    if node.auto_id:
        surf_cnt = counts.surface_auto_id_instances.get((surface.surface_id, node.uia_type, node.auto_id), 1)
        glob_cnt = counts.global_auto_id_instances.get((node.uia_type, node.auto_id), 0)
        is_weak = is_weak_generic_auto_id(node.auto_id)
        aid_quality = classify_auto_id_quality(node.auto_id, is_weak, glob_cnt, surf_cnt)

        meta_bits: List[str] = []
        if node.auto_id_source and node.auto_id_source != "x:Name":
            meta_bits.append(node.auto_id_source)
        if aid_quality == "Weak_Generated":
            meta_bits.append("弱ID")
        elif aid_quality in ("Semantic_Unique", "Semantic_Repeated"):
            meta_bits.append("语义ID")
        if is_shared and glob_cnt == 0:
            meta_bits.append(f"{scope_prefix}{surf_cnt}, 共享组件")
        else:
            meta_bits.append(f"{scope_prefix}{surf_cnt}/全仓{glob_cnt}")
        meta_str = f" ({', '.join(meta_bits)})" if meta_bits else ""
        id_parts.append(f"`@auto_id='{node.auto_id}'`{meta_str}")

    if node.dissolved_wrapper_name:
        id_parts.append(f"外壳:`{node.dissolved_wrapper_type}({node.dissolved_wrapper_name})`")

    eff_text, is_child_text = get_effective_display_text(node)
    if eff_text:
        is_long_or_multiline = ("\n" in eff_text or "\r" in eff_text or len(eff_text) > 80)
        if is_long_or_multiline:
            flat = " ".join(eff_text.split())
            summary = flat[:60] + ("..." if len(flat) > 60 else "")
            id_parts.append(f"长文本摘要:`'{summary}'`")
        else:
            n_surf = counts.surface_name_instances.get((surface.surface_id, node.uia_type, eff_text), 1)
            n_glob = counts.global_name_instances.get((node.uia_type, eff_text), 0)
            n_cnt_str = f"{scope_prefix}{n_surf}, 共享组件" if (is_shared and n_glob == 0) else f"{scope_prefix}{n_surf}/全仓{n_glob}"
            if not is_child_text:
                primary_nm = node.name_en or node.name_zh
                zh_part = f" / `{node.name_zh}`" if node.name_en and node.name_zh and node.name_zh != node.name_en else ""
                id_parts.append(f"`@name='{primary_nm}'`{zh_part} ({n_cnt_str})")
            else:
                id_parts.append(f"子文本:`'{eff_text}'` ({n_cnt_str})")

    if node.help_text_en and node.help_text_en.strip() != eff_text.strip():
        ht_flat = " ".join(node.help_text_en.split())
        ht_short = ht_flat[:60] + ("..." if len(ht_flat) > 60 else "")
        id_parts.append(f"`@help_text='{ht_short}'`")

    bind_cmd = " | ".join(x for x in (node.binding_path, node.command_expr) if x)
    if bind_cmd:
        id_parts.append(f"绑定/命令:`{bind_cmd}`")

    head_desc = " | ".join(id_parts) if id_parts else f"`{node.file}:{node.line}`"
    clr_tag = f"`{node.short_clr_type}`, 外部推断" if node.type_source == "inferred_external" else f"`{node.short_clr_type}`"
    line1 = f"- **[{node.uia_type}]** ({clr_tag}) {head_desc}"

    struct_parts: List[str] = []
    anc_chain = describe_semantic_ancestors(node, is_weak_generic_auto_id)
    if anc_chain:
        struct_parts.append(f"容器:`{anc_chain}`")
    sib_order = describe_uia_sibling_order(node)
    if sib_order:
        struct_parts.append(f"同父UIA顺序:`{sib_order}`")
    elif node.adjacent_label:
        struct_parts.append(f"邻近标签:`'{node.adjacent_label}'`")

    state_flags: List[str] = []
    if not node.is_initially_visible:
        state_flags.append("默认隐藏(Collapsed)")
    if node.is_readonly:
        state_flags.append("只读(IsReadOnly)")
    if folded_spinners:
        state_flags.append(f"微调按钮:{'/'.join(folded_spinners)}")
    if state_flags:
        struct_parts.append(f"状态:{', '.join(state_flags)}")

    if node.topology != "InWindow":
        struct_parts.append(f"拓扑:`{node.topology}`")
    if node.preconditions:
        struct_parts.append(f"前置:`{' -> '.join(node.preconditions)}`")
    if node.file:
        loc_src = f"{node.file}:{node.line}" if node.line else node.file
        struct_parts.append(f"源码:`{loc_src}`")

    lines = [line1]
    if struct_parts:
        lines.append(f"  - 结构: {' | '.join(struct_parts)}")
    if is_special_action_contract(node) and node.action_contract:
        lines.append(f"  - 契约: {node.action_contract[:140]}")
    return "\n".join(lines)


def export_kb_markdown_docs(idx: WorkspaceIndex, kb_docs_dir: Path) -> MarkdownExportReport:
    """Export module-aggregated, pure-objective-facts (zero XPath), <=1150-char heading-chunked Markdown docs."""
    if kb_docs_dir.exists():
        safe_rmtree(kb_docs_dir)
    kb_docs_dir.mkdir(parents=True, exist_ok=True)

    report = MarkdownExportReport(out_dir=str(kb_docs_dir))
    counts = compute_identity_counts(idx)

    # 1. Group surfaces by `(repo_id, assembly)` -> ~40-60 clean module-level docs instead of 350 fragments!
    shared_by_module: Dict[Tuple[str, str], List[ProjectedSurface]] = defaultdict(list)
    regular_by_module: Dict[Tuple[str, str], List[ProjectedSurface]] = defaultdict(list)

    for sid in sorted(idx.projected_surfaces):
        surf = idx.projected_surfaces[sid]
        mod_key = (surf.repo_id, surf.assembly)
        if surf.surface_kind == "SharedComponent":
            if surf.interactive_nodes:
                shared_by_module[mod_key].append(surf)
        else:
            if surf.interactive_nodes or surf.mounted_component_defs or surf.triggered_dialogs:
                regular_by_module[mod_key].append(surf)

    used_rel_paths: Set[str] = set()

    def allocate_rel_path(folder: str, preferred_stem: str, fallback_prefix: str) -> Tuple[str, str]:
        stem = _safe_filename_stem(preferred_stem)
        cand = f"{folder}/{stem}.md" if folder else f"{stem}.md"
        if cand.lower() in used_rel_paths:
            stem = _safe_filename_stem(f"{fallback_prefix}_{preferred_stem}")
            cand = f"{folder}/{stem}.md" if folder else f"{stem}.md"
        seq = 2
        base_stem = stem
        while cand.lower() in used_rel_paths:
            stem = f"{base_stem}_{seq}"
            cand = f"{folder}/{stem}.md" if folder else f"{stem}.md"
            seq += 1
        used_rel_paths.add(cand.lower())
        return cand, f"{stem}.md"

    shared_def_to_relpath: Dict[str, str] = {}
    shared_mod_allocs: List[Tuple[Tuple[str, str], List[ProjectedSurface], str, str]] = []
    for mod_key in sorted(shared_by_module):
        repo_id, asm = mod_key
        rel_p, fname = allocate_rel_path(SHARED_FOLDER_NAME, f"Shared_{asm}", f"Shared_{repo_id}")
        surfs = shared_by_module[mod_key]
        for s in surfs:
            shared_def_to_relpath[s.root_def_id] = rel_p
        shared_mod_allocs.append((mod_key, surfs, rel_p, fname))

    regular_mod_allocs: List[Tuple[Tuple[str, str], List[ProjectedSurface], str, str]] = []
    for mod_key in sorted(regular_by_module):
        repo_id, asm = mod_key
        folder = _safe_filename_stem(repo_id)
        rel_p, fname = allocate_rel_path(folder, asm, repo_id)
        regular_mod_allocs.append((mod_key, regular_by_module[mod_key], rel_p, fname))

    def _write_doc(
        rel_path: str,
        folder_path: str,
        file_name: str,
        surface_id: str,
        title: str,
        meta_lines: Sequence[str],
        sections: Sequence[str],
    ) -> None:
        if not sections:
            return
        md_text, digest = _attach_document_hash(title, meta_lines, sections)
        chunks = split_markdown_by_h2(md_text)
        max_len = max((len(c) for c in chunks), default=0)
        out_file = kb_docs_dir / rel_path
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(md_text, encoding="utf-8")

        doc_rec = ExportedMarkdownDoc(
            rel_path=rel_path,
            folder_path=folder_path,
            file_name=file_name,
            surface_id=surface_id,
            sha256=digest,
            section_count=len(chunks),
            max_section_chars=max_len,
        )
        report.docs.append(doc_rec)
        report.total_docs += 1
        report.total_sections += len(chunks)
        if max_len > report.max_observed_section_chars:
            report.max_observed_section_chars = max_len

    index_entries: List[str] = []

    # 2. Emit SharedComponent module docs (`共享组件/Shared_<Assembly>.md`)
    for (repo_id, asm), surfs, rel_p, fname in shared_mod_allocs:
        comp_names = [s.surface_name for s in surfs]
        meta_lines = [
            f"- **类型**: 跨界面复用共享组件集合 (`SharedComponent`, 所属 `{repo_id}` / `{asm}`, 文档 `{rel_p}`)",
            f"- **包含组件 ({len(comp_names)})**: {', '.join(f'`{c}`' for c in comp_names[:16])}"
            + (f" 等 {len(comp_names)} 个" if len(comp_names) > 16 else ""),
            "- **消费原则 (纯客观静态事实)**: 仅记录投影后的客观 UIA 属性、双维重名统计(本组件/全仓)、语义容器与特殊交互契约（不含预制 XPath），请结合宿主窗口与工程案例推导定位。",
        ]
        sections: List[str] = []
        total_ctrls = 0
        for surf in surfs:
            ref_cnt = idx.shared_component_defs.get(surf.root_def_id, 2)
            folded_ids, owner_spinners = _collect_folded_spinner_buttons(surf.interactive_nodes)
            grouped: Dict[str, List[str]] = defaultdict(list)
            for n in surf.interactive_nodes:
                if n.node_id in folded_ids:
                    continue
                total_ctrls += 1
                grp = n.panel_group or surf.surface_name
                grouped[grp].append(_format_control_entry(n, surf, counts, owner_spinners.get(n.node_id, ())))
            for grp_name, entries in grouped.items():
                sec_title = f"共享组件 - {surf.surface_name}" if grp_name == surf.surface_name else f"共享组件 - {surf.surface_name} / {grp_name}"
                banner = (
                    f"[共享组件: {surf.surface_name} (复用:{ref_cnt}处) | 模块: {repo_id}/{asm} "
                    f"| 分组: {grp_name} | 源码: {surf.file}]"
                )
                sections.extend(_pack_entries_into_h2_sections(sec_title, banner, entries))

        _write_doc(
            rel_path=rel_p,
            folder_path=SHARED_FOLDER_NAME,
            file_name=fname,
            surface_id=f"SharedModule::{repo_id}::{asm}",
            title=f"共享组件模块：{asm} ({repo_id})",
            meta_lines=meta_lines,
            sections=sections,
        )
        report.shared_component_docs += 1
        preview_names = ", ".join(comp_names[:8]) + ("..." if len(comp_names) > 8 else "")
        kws = _extract_module_keywords(surfs, counts, limit=6)
        kw_part = f" | 界面词: {', '.join(kws)}" if kws else ""
        index_entries.append(
            f"- **[共享组件模块] `{asm}`** (`{repo_id}`) -> `{rel_p}` | 包含 {len(comp_names)} 个共享组件 ({preview_names}) | 控件:{total_ctrls}{kw_part}"
        )

    # 3. Emit Regular Module docs (`<Repo>/<Assembly>.md`)
    shared_def_id_set = set(shared_def_to_relpath.keys())
    for (repo_id, asm), surfs, rel_p, fname in regular_mod_allocs:
        commit_short = (idx.repo_manifests.get(repo_id, {}).get("commit") or "")[:12]
        surf_names = [s.surface_name for s in surfs]
        meta_lines = [
            f"- **模块标识**: `{repo_id}` / `{asm}` (`{commit_short}`) | **包含界面/视图数**: `{len(surfs)}`",
            f"- **视图清单**: {', '.join(f'`{sn}`' for sn in surf_names[:16])}"
            + (f" 等 {len(surf_names)} 个" if len(surf_names) > 16 else ""),
            "- **消费原则 (纯客观静态事实)**: 仅记录经 WPF/WinForms 规则投影后的客观 UIA 属性、双维重名统计(本界面/全仓)、有效语义容器、近邻同父顺序、显隐/只读状态及特殊交互契约（不含预制 XPath），由消费端结合宿主窗口与工程案例自主推导定位。",
        ]

        sections = []
        module_inline_cnt = 0
        module_shared_refs: Set[str] = set()

        for surf in surfs:
            folded_ids, owner_spinners = _collect_folded_spinner_buttons(surf.interactive_nodes)
            grouped_inline: Dict[str, List[str]] = defaultdict(list)
            referenced_shared_docs: Dict[Tuple[str, str], str] = {}

            for n in surf.interactive_nodes:
                if n.source_def_id in shared_def_to_relpath:
                    s_rel = shared_def_to_relpath[n.source_def_id]
                    s_short = n.source_def_id.rsplit(".", 1)[-1]
                    module_shared_refs.add(s_rel)
                    referenced_shared_docs[(s_rel, s_short)] = (
                        f"- **复用共享组件 `{s_short}`** -> 详见 `{s_rel}`"
                        f"（外层分组: `{n.panel_group or surf.surface_name}`）"
                    )
                else:
                    if n.node_id in folded_ids:
                        continue
                    module_inline_cnt += 1
                    grp = n.panel_group or surf.surface_name
                    grouped_inline[grp].append(_format_control_entry(n, surf, counts, owner_spinners.get(n.node_id, ())))

            for mdef in surf.mounted_component_defs:
                if mdef in shared_def_to_relpath:
                    s_rel = shared_def_to_relpath[mdef]
                    s_short = mdef.rsplit(".", 1)[-1]
                    module_shared_refs.add(s_rel)
                    referenced_shared_docs.setdefault(
                        (s_rel, s_short),
                        f"- **复用共享组件 `{s_short}`** -> 详见 `{s_rel}`",
                    )

            host_fact = format_surface_host_fact(surf)
            base_ctx_prefix = (
                f"[模块: {repo_id}/{asm} | 界面: {surf.surface_name} ({surf.surface_kind}) "
                f"| 宿主: {host_fact}"
            )

            for grp_name, entries in grouped_inline.items():
                sec_title = (
                    f"{surf.surface_name}"
                    if grp_name == surf.surface_name
                    else f"{surf.surface_name} - {grp_name}"
                )
                grp_part = f" | 分组: {grp_name}" if grp_name != surf.surface_name else ""
                banner = f"{base_ctx_prefix}{grp_part}]"
                sections.extend(_pack_entries_into_h2_sections(sec_title, banner, entries))

            if referenced_shared_docs or surf.triggered_dialogs:
                ref_lines = [referenced_shared_docs[k] for k in sorted(referenced_shared_docs)]
                for dlg_def in surf.triggered_dialogs:
                    dlg_short = dlg_def.rsplit(".", 1)[-1]
                    ref_lines.append(f"- **可触发模态弹窗**: `{dlg_short}` (`{dlg_def}`)")
                banner = f"{base_ctx_prefix} | 分组: 复用组件与关联弹窗]"
                sections.extend(_pack_entries_into_h2_sections(f"{surf.surface_name} - 共享组件与弹窗", banner, ref_lines))

        if not sections:
            continue

        _write_doc(
            rel_path=rel_p,
            folder_path=_safe_filename_stem(repo_id),
            file_name=fname,
            surface_id=f"Module::{repo_id}::{asm}",
            title=f"模块静态 UI 事实参考：{asm} ({repo_id})",
            meta_lines=meta_lines,
            sections=sections,
        )
        report.surface_docs += 1

        preview_surfs = ", ".join(surf_names[:6]) + ("..." if len(surf_names) > 6 else "")
        kws = _extract_module_keywords(surfs, counts, shared_def_ids=shared_def_id_set, limit=7)
        kw_part = f" | 界面词: {', '.join(kws)}" if kws else ""
        index_entries.append(
            f"- **`{asm}`** (`{repo_id}`) -> `{rel_p}` | 界面({len(surfs)}): {preview_surfs} | 控件:{module_inline_cnt}, 共享:{len(module_shared_refs)}{kw_part}"
        )

    # 4. Emit `00_全局窗口与模块路由总索引.md` at root (`folder_path == ""`)
    idx_meta = [
        f"- **覆盖仓库数**: `{len(idx.repo_manifests)}` | **导出模块文档总数**: `{report.total_docs + 1}`（业务模块 `{report.surface_docs}` + 共享组件模块 `{report.shared_component_docs}` + 本总索引 `1`）",
        "- **两步检索指引**: 先按 Feature 业务词在本总索引（含模块界面词）或 `list_documents` 锁定目标业务模块 `.md` 文档，再用 `read_document(knowledge_id, query=...)` 或 `search_knowledge(mode='hybrid', limit<=6)` 读取具体控件的投影 UIA 事实、双维重名统计与容器锚点。",
    ]
    idx_banner = "[全局路由总表 | STATIC_UI_DOCS | 模块级文档与共享组件导航]"
    idx_sections = _pack_entries_into_h2_sections(
        "全局模块与共享组件路由目录",
        idx_banner,
        index_entries or ["- 当前快照集合暂无可交互界面。"],
    )
    _write_doc(
        rel_path=GLOBAL_INDEX_FILENAME,
        folder_path="",
        file_name=GLOBAL_INDEX_FILENAME,
        surface_id="GlobalRouteIndex",
        title="静态 UI 控件知识库：全局窗口与模块路由总索引",
        meta_lines=idx_meta,
        sections=idx_sections,
    )

    report.docs.sort(key=lambda d: d.rel_path)
    return report
