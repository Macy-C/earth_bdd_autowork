from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from behave.model import ScenarioOutline
from behave.parser import Parser

from autowork_core.runtime.temporary_features import is_temporary_feature
from config.paths import Paths


_OUTLINE_SUFFIX_RE = re.compile(r"^(?P<name>.+?)\s+--\s+@(?P<example_id>[1-9]\d*\.[1-9]\d*)(?:\s+.*)?$")


@dataclass(frozen=True)
class StepItemDTO:
    index: int
    keyword: str
    name: str
    line: int | None
    label: str

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "keyword": self.keyword,
            "name": self.name,
            "line": self.line,
            "label": self.label,
        }


@dataclass(frozen=True)
class FeatureTreeNode:
    node_id: str
    kind: str
    title: str
    type_label: str
    detail: str
    target_path: Path
    rel_path: str
    tags: tuple[str, ...] = ()
    line: int | None = None
    rule_name: str | None = None
    rule_line: int | None = None
    scenario_name: str | None = None
    scenario_line: int | None = None
    example_id: str | None = None
    step_index: int | None = None
    available_steps: tuple[StepItemDTO, ...] = ()
    parse_error: str | None = None
    children: tuple[FeatureTreeNode, ...] = ()
    clean_title: str = ""
    badge_count: int | None = None
    row_params: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "title": self.title,
            "clean_title": self.clean_title or self.title,
            "type_label": self.type_label,
            "detail": self.detail,
            "target_path": str(self.target_path),
            "rel_path": self.rel_path,
            "tags": list(self.tags),
            "line": self.line,
            "rule_name": self.rule_name,
            "rule_line": self.rule_line,
            "scenario_name": self.scenario_name,
            "scenario_line": self.scenario_line,
            "example_id": self.example_id,
            "step_index": self.step_index,
            "available_steps": [s.as_dict() for s in self.available_steps],
            "parse_error": self.parse_error,
            "badge_count": self.badge_count,
            "row_params": [{"key": k, "value": v} for k, v in self.row_params],
            "children": [c.as_dict() for c in self.children],
        }


@dataclass(frozen=True)
class FeatureCatalog:
    root_path: Path
    nodes: tuple[FeatureTreeNode, ...]
    feature_count: int
    scenario_count: int
    parse_errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "root_path": str(self.root_path),
            "feature_count": self.feature_count,
            "scenario_count": self.scenario_count,
            "parse_errors": list(self.parse_errors),
            "nodes": [n.as_dict() for n in self.nodes],
        }


@dataclass(frozen=True)
class FailedCaseSummary:
    feature_path: Path
    rel_path: str
    feature_name: str
    scenario_name: str
    example_id: str | None
    error_summary: str
    display_label: str

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_path": str(self.feature_path),
            "rel_path": self.rel_path,
            "feature_name": self.feature_name,
            "scenario_name": self.scenario_name,
            "example_id": self.example_id,
            "error_summary": self.error_summary,
            "display_label": self.display_label,
        }


def discover_feature_catalog(root_path: str | Path | None = None) -> FeatureCatalog:
    target = Path(root_path or Paths.TEST_FEATURES_DIR).resolve()
    if not target.exists():
        return FeatureCatalog(
            root_path=target,
            nodes=(),
            feature_count=0,
            scenario_count=0,
            parse_errors=(f"路径不存在: {target}",),
        )

    errors: list[str] = []
    stats = {"features": 0, "scenarios": 0}

    if target.is_file():
        if target.suffix.lower() != ".feature" or is_temporary_feature(target):
            return FeatureCatalog(
                root_path=target,
                nodes=(),
                feature_count=0,
                scenario_count=0,
                parse_errors=(f"不是有效的 .feature 文件: {target.name}",),
            )
        node = _build_feature_node(target, target.parent, errors, stats)
        return FeatureCatalog(
            root_path=target,
            nodes=(node,),
            feature_count=stats["features"],
            scenario_count=stats["scenarios"],
            parse_errors=tuple(errors),
        )

    root_node = _build_directory_node(target, target, errors, stats, is_root=True)
    nodes = (root_node,) if root_node is not None else ()
    return FeatureCatalog(
        root_path=target,
        nodes=nodes,
        feature_count=stats["features"],
        scenario_count=stats["scenarios"],
        parse_errors=tuple(errors),
    )


def filter_feature_tree(
        nodes: tuple[FeatureTreeNode, ...],
        query: str,
) -> tuple[FeatureTreeNode, ...]:
    needle = str(query or "").strip().casefold()
    if not needle:
        return nodes
    filtered = []
    for node in nodes:
        matched = _filter_node(node, needle)
        if matched is not None:
            filtered.append(matched)
    return tuple(filtered)


def discover_failed_targets(
        report_json_path: str | Path | None = None,
        *,
        base_dir: Path | None = None,
) -> tuple[FailedCaseSummary, ...]:
    base_root = Path(base_dir or Paths.BASE_DIR).resolve()
    report_path = Path(
        report_json_path or (Paths.REPORTS_DIR / "autowork-report.json")
    ).resolve()
    if not report_path.is_file():
        return ()
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return ()

    results: list[FailedCaseSummary] = []
    seen: set[tuple[str, str, str | None]] = set()
    for feature in payload.get("features") or ():
        if not isinstance(feature, dict):
            continue
        raw_file = str(feature.get("file") or feature.get("source") or "").strip()
        if not raw_file:
            continue
        feature_path = Path(raw_file)
        if not feature_path.is_absolute():
            feature_path = (base_root / feature_path).resolve()
        else:
            feature_path = feature_path.resolve()
        if not feature_path.is_file():
            continue
        rel_path = _display_relpath(feature_path, base_root)
        feature_name = str(feature.get("name") or feature_path.stem)
        outline_lookup = _outline_templates_by_concrete_name(feature_path)

        for scenario in feature.get("scenarios") or ():
            if not isinstance(scenario, dict):
                continue
            status = str(scenario.get("status") or scenario.get("test_status") or "").lower()
            if status not in {"failed", "error", "undefined"}:
                continue
            raw_name = str(scenario.get("name") or "").strip()
            if not raw_name:
                continue
            scenario_name, example_id = _resolve_report_scenario_identity(
                raw_name,
                scenario,
                outline_lookup,
            )
            dedup_key = (str(feature_path), scenario_name, example_id)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            error_text = _extract_scenario_error(scenario)
            suffix = f" [Example {example_id}]" if example_id else ""
            label = f"{rel_path} › {scenario_name}{suffix}"
            if error_text:
                label = f"{label} — {error_text}"
            results.append(FailedCaseSummary(
                feature_path=feature_path,
                rel_path=rel_path,
                feature_name=feature_name,
                scenario_name=scenario_name,
                example_id=example_id,
                error_summary=error_text,
                display_label=label,
            ))
    return tuple(results)


def _build_directory_node(
        directory: Path,
        base_root: Path,
        errors: list[str],
        stats: dict[str, int],
        *,
        is_root: bool = False,
) -> FeatureTreeNode | None:
    try:
        entries = sorted(
            directory.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.casefold()),
        )
    except OSError as exc:
        errors.append(f"{directory}: {type(exc).__name__}: {exc}")
        return None

    child_dirs: list[FeatureTreeNode] = []
    child_features: list[FeatureTreeNode] = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_dir():
            sub_node = _build_directory_node(entry, base_root, errors, stats, is_root=False)
            if sub_node is not None:
                child_dirs.append(sub_node)
        elif entry.is_file() and entry.suffix.lower() == ".feature" and not is_temporary_feature(entry):
            child_features.append(_build_feature_node(entry, base_root, errors, stats))

    children = tuple([*child_dirs, *child_features])
    if not children and not is_root:
        return None

    feature_total = _count_kind(children, "feature")
    rel_path = _display_relpath(directory, Paths.BASE_DIR)
    clean_dir_title = directory.name if not is_root else rel_path
    title = (
        f"📁 {directory.name}  ({feature_total})"
        if not is_root
        else f"📁 {rel_path}  ({feature_total} 个 Feature)"
    )
    return FeatureTreeNode(
        node_id=f"dir:{directory.as_posix()}",
        kind="directory",
        title=title,
        clean_title=clean_dir_title,
        badge_count=feature_total,
        type_label="目录",
        detail=f"{feature_total} 个 Feature",
        target_path=directory,
        rel_path=rel_path,
        children=children,
    )


def _build_feature_node(
        feature_path: Path,
        base_root: Path,
        errors: list[str],
        stats: dict[str, int],
) -> FeatureTreeNode:
    stats["features"] += 1
    rel_path = _display_relpath(feature_path, Paths.BASE_DIR)
    node_prefix = f"feature:{feature_path.as_posix()}"
    try:
        raw_text = feature_path.read_text(encoding="utf-8-sig")
        feature = Parser().parse(raw_text, filename=str(feature_path))
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".splitlines()[0]
        errors.append(f"{rel_path}: {msg}")
        return FeatureTreeNode(
            node_id=node_prefix,
            kind="feature",
            title=f"⚠ {feature_path.name}  [语法错误]",
            clean_title=feature_path.name,
            type_label="Feature",
            detail=f"[解析失败] {msg}",
            target_path=feature_path,
            rel_path=rel_path,
            parse_error=msg,
        )

    tags = tuple(str(tag) for tag in getattr(feature, "tags", ()) or ())
    children: list[FeatureTreeNode] = []
    for scenario in feature.scenarios:
        children.append(_build_scenario_node(
            feature_path,
            rel_path,
            node_prefix,
            scenario,
            rule=None,
            stats=stats,
        ))
    for rule in feature.rules:
        children.append(_build_rule_node(
            feature_path,
            rel_path,
            node_prefix,
            rule,
            stats=stats,
        ))

    scen_total = len(feature.scenarios) + sum(len(r.scenarios) for r in feature.rules)
    name_part = _single_line(feature.name) if getattr(feature, "name", "") else ""
    if name_part and name_part.casefold() != feature_path.stem.casefold():
        clean_feat_title = f"{feature_path.name} — {name_part}"
        feature_title = f"📄 {clean_feat_title}  ({scen_total})"
    else:
        clean_feat_title = feature_path.name
        feature_title = f"📄 {feature_path.name}  ({scen_total})"
    tag_summary = _format_tags(tags)
    detail_parts = [f"{scen_total} 个场景"]
    if feature.rules:
        detail_parts.append(f"{len(feature.rules)} 个 Rule")
    if tag_summary:
        detail_parts.append(tag_summary)

    return FeatureTreeNode(
        node_id=node_prefix,
        kind="feature",
        title=feature_title,
        clean_title=clean_feat_title,
        badge_count=scen_total,
        type_label="Feature",
        detail=" · ".join(detail_parts),
        target_path=feature_path,
        rel_path=rel_path,
        tags=tags,
        line=getattr(feature, "line", None),
        children=tuple(children),
    )


def _build_rule_node(
        feature_path: Path,
        rel_path: str,
        parent_id: str,
        rule,
        *,
        stats: dict[str, int],
) -> FeatureTreeNode:
    rule_line = getattr(rule, "line", None)
    rule_name = _single_line(getattr(rule, "name", "") or "未命名 Rule")
    node_id = f"{parent_id}|rule:{rule_line}:{rule_name}"
    tags = tuple(str(tag) for tag in getattr(rule, "tags", ()) or ())
    children = tuple(
        _build_scenario_node(
            feature_path,
            rel_path,
            node_id,
            scenario,
            rule=rule,
            stats=stats,
        )
        for scenario in getattr(rule, "scenarios", ())
    )
    detail_parts = [f"L{rule_line}" if rule_line else "", f"{len(children)} 个场景"]
    tag_text = _format_tags(tags)
    if tag_text:
        detail_parts.append(tag_text)
    return FeatureTreeNode(
        node_id=node_id,
        kind="rule",
        title=f"📦 Rule: {rule_name}  ({len(children)})",
        clean_title=f"Rule: {rule_name}",
        badge_count=len(children),
        type_label="Rule",
        detail=" · ".join(part for part in detail_parts if part),
        target_path=feature_path,
        rel_path=rel_path,
        tags=tags,
        line=rule_line,
        rule_name=rule.name,
        rule_line=rule_line,
        children=children,
    )


def _build_scenario_node(
        feature_path: Path,
        rel_path: str,
        parent_id: str,
        scenario,
        *,
        rule,
        stats: dict[str, int],
) -> FeatureTreeNode:
    stats["scenarios"] += 1
    scen_line = getattr(scenario, "line", None)
    scen_name = str(getattr(scenario, "name", "") or "")
    display_name = _single_line(scen_name) or "<未命名场景>"
    rule_name = rule.name if rule is not None else None
    rule_line = getattr(rule, "line", None) if rule is not None else None
    tags = tuple(str(tag) for tag in getattr(scenario, "tags", ()) or ())
    node_id = f"{parent_id}|scen:{scen_line}:{display_name}"

    if isinstance(scenario, ScenarioOutline):
        return _build_outline_node(
            feature_path=feature_path,
            rel_path=rel_path,
            node_id=node_id,
            outline=scenario,
            display_name=display_name,
            scen_line=scen_line,
            tags=tags,
            rule_name=rule_name,
            rule_line=rule_line,
        )

    steps = _build_step_dtos(getattr(scenario, "steps", ()))
    detail_parts = [f"L{scen_line}" if scen_line else "", f"{len(steps)} 步"]
    tag_text = _format_tags(tags)
    if tag_text:
        detail_parts.append(tag_text)
    return FeatureTreeNode(
        node_id=node_id,
        kind="scenario",
        title=f"▶ {display_name}",
        clean_title=display_name,
        badge_count=len(steps),
        type_label="Scenario",
        detail=" · ".join(part for part in detail_parts if part),
        target_path=feature_path,
        rel_path=rel_path,
        tags=tags,
        line=scen_line,
        rule_name=rule_name,
        rule_line=rule_line,
        scenario_name=scen_name,
        scenario_line=scen_line,
        available_steps=steps,
        children=(),
    )


def _build_outline_node(
        *,
        feature_path: Path,
        rel_path: str,
        node_id: str,
        outline,
        display_name: str,
        scen_line: int | None,
        tags: tuple[str, ...],
        rule_name: str | None,
        rule_line: int | None,
) -> FeatureTreeNode:
    expanded_list = list(getattr(outline, "scenarios", ()) or ())
    expanded_by_id = {
        str(getattr(item._row, "id", "")): item
        for item in expanded_list
        if getattr(item, "_row", None) is not None
    }

    group_nodes: list[FeatureTreeNode] = []
    total_rows = 0
    for group_idx, examples in enumerate(getattr(outline, "examples", ()) or (), 1):
        ex_line = getattr(examples, "line", None)
        ex_name = _single_line(getattr(examples, "name", "") or "")
        ex_tags = tuple(str(tag) for tag in getattr(examples, "tags", ()) or ())
        group_example_id = f"{group_idx}.*"
        group_node_id = f"{node_id}|exgroup:{group_idx}"
        headings = list(examples.table.headings) if getattr(examples, "table", None) else []
        rows = list(examples.table.rows) if getattr(examples, "table", None) else []
        total_rows += len(rows)

        row_nodes: list[FeatureTreeNode] = []
        for row_idx, row in enumerate(rows, 1):
            row_id = str(getattr(row, "id", "") or f"{group_idx}.{row_idx}")
            row_line = getattr(row, "line", None)
            concrete = expanded_by_id.get(row_id)
            concrete_steps = _build_step_dtos(
                getattr(concrete, "steps", ()) if concrete is not None else getattr(outline, "steps", ())
            )
            row_node_id = f"{group_node_id}|exrow:{row_id}"
            row_cells = [str(c) for c in row.cells]
            row_params = tuple((str(h), str(c)) for h, c in zip(headings, row_cells))
            row_preview = _format_row_preview(headings, row_cells)
            row_nodes.append(FeatureTreeNode(
                node_id=row_node_id,
                kind="example_row",
                title=f"• @{row_id}  {row_preview}" if row_preview else f"• @{row_id}",
                clean_title=f"@{row_id}",
                badge_count=len(concrete_steps),
                row_params=row_params,
                type_label="Example 行",
                detail=f"L{row_line} · {len(concrete_steps)} 步" if row_line else f"{len(concrete_steps)} 步",
                target_path=feature_path,
                rel_path=rel_path,
                line=row_line,
                rule_name=rule_name,
                rule_line=rule_line,
                scenario_name=outline.name,
                scenario_line=scen_line,
                example_id=row_id,
                available_steps=concrete_steps,
                children=(),
            ))

        clean_group_title = f"Examples #{group_idx}: {ex_name}" if ex_name else f"Examples #{group_idx}"
        group_title = f"📋 {clean_group_title}  ({len(row_nodes)} 行)"
        group_detail_parts = [f"L{ex_line}" if ex_line else "", f"{len(row_nodes)} 行 ({group_example_id})"]
        ex_tag_text = _format_tags(ex_tags)
        if ex_tag_text:
            group_detail_parts.append(ex_tag_text)
        group_nodes.append(FeatureTreeNode(
            node_id=group_node_id,
            kind="examples_group",
            title=group_title,
            clean_title=clean_group_title,
            badge_count=len(row_nodes),
            type_label="Examples 组",
            detail=" · ".join(part for part in group_detail_parts if part),
            target_path=feature_path,
            rel_path=rel_path,
            tags=ex_tags,
            line=ex_line,
            rule_name=rule_name,
            rule_line=rule_line,
            scenario_name=outline.name,
            scenario_line=scen_line,
            example_id=group_example_id,
            children=tuple(row_nodes),
        ))

    detail_parts = [
        f"L{scen_line}" if scen_line else "",
        f"{len(group_nodes)} 组 / {total_rows} 行",
    ]
    tag_text = _format_tags(tags)
    if tag_text:
        detail_parts.append(tag_text)

    return FeatureTreeNode(
        node_id=node_id,
        kind="scenario_outline",
        title=f"🔁 {display_name}  ({total_rows} 行)",
        clean_title=display_name,
        badge_count=total_rows,
        type_label="Outline",
        detail=" · ".join(part for part in detail_parts if part),
        target_path=feature_path,
        rel_path=rel_path,
        tags=tags,
        line=scen_line,
        rule_name=rule_name,
        rule_line=rule_line,
        scenario_name=outline.name,
        scenario_line=scen_line,
        children=tuple(group_nodes),
    )


def _build_step_dtos(steps) -> tuple[StepItemDTO, ...]:
    items = []
    for index, step in enumerate(steps or (), 1):
        keyword = str(getattr(step, "keyword", "") or getattr(step, "step_type", "Given")).strip()
        name = _single_line(getattr(step, "name", "") or "")
        line = getattr(step, "line", None)
        items.append(StepItemDTO(
            index=index,
            keyword=keyword,
            name=name,
            line=line,
            label=f"Step {index}: {keyword} {name}",
        ))
    return tuple(items)


def _filter_node(node: FeatureTreeNode, needle: str) -> FeatureTreeNode | None:
    step_texts = " ".join(step.label for step in node.available_steps)
    haystack = " ".join((
        node.title,
        node.type_label,
        node.detail,
        node.rel_path,
        " ".join(node.tags),
        node.scenario_name or "",
        node.rule_name or "",
        node.example_id or "",
        step_texts,
    )).casefold()
    self_matches = needle in haystack

    if self_matches and node.kind in {"scenario", "scenario_outline", "examples_group", "example_row"}:
        return node

    filtered_children = tuple(
        child_match
        for child in node.children
        if (child_match := _filter_node(child, needle)) is not None
    )
    if filtered_children:
        return FeatureTreeNode(
            node_id=node.node_id,
            kind=node.kind,
            title=node.title,
            clean_title=node.clean_title,
            badge_count=node.badge_count,
            row_params=node.row_params,
            type_label=node.type_label,
            detail=node.detail,
            target_path=node.target_path,
            rel_path=node.rel_path,
            tags=node.tags,
            line=node.line,
            rule_name=node.rule_name,
            rule_line=node.rule_line,
            scenario_name=node.scenario_name,
            scenario_line=node.scenario_line,
            example_id=node.example_id,
            step_index=node.step_index,
            available_steps=node.available_steps,
            parse_error=node.parse_error,
            children=filtered_children,
        )
    if self_matches:
        return node
    return None


def _outline_templates_by_concrete_name(feature_path: Path) -> dict[tuple[str, str], str]:
    try:
        parsed = Parser().parse(
            feature_path.read_text(encoding="utf-8-sig"),
            filename=str(feature_path),
        )
    except Exception:
        return {}
    mapping: dict[tuple[str, str], str] = {}
    outlines = [
        item for item in parsed.scenarios if isinstance(item, ScenarioOutline)
    ] + [
        item
        for rule in parsed.rules
        for item in rule.scenarios
        if isinstance(item, ScenarioOutline)
    ]
    for outline in outlines:
        for concrete in outline.scenarios:
            row = getattr(concrete, "_row", None)
            row_id = str(getattr(row, "id", "") or "")
            if row_id:
                mapping[(concrete.name, row_id)] = outline.name
                mapping[(concrete.name.strip(), row_id)] = outline.name
                base_without_suffix = _OUTLINE_SUFFIX_RE.match(concrete.name.strip())
                if base_without_suffix:
                    mapping[(base_without_suffix.group("name").strip(), row_id)] = outline.name
                mapping[(f"{outline.name} -- @{row_id}", row_id)] = outline.name
                mapping[(f"{outline.name.strip()} -- @{row_id}", row_id)] = outline.name
    return mapping


def _resolve_report_scenario_identity(
        raw_name: str,
        scenario: dict,
        outline_lookup: dict[tuple[str, str], str],
) -> tuple[str, str | None]:
    example = scenario.get("example")
    if isinstance(example, dict) and example.get("id"):
        ex_id = str(example["id"])
        template = outline_lookup.get((raw_name, ex_id))
        if template:
            return template, ex_id
        match = _OUTLINE_SUFFIX_RE.match(raw_name)
        if match:
            return match.group("name"), ex_id
        return raw_name, ex_id

    match = _OUTLINE_SUFFIX_RE.match(raw_name)
    if match:
        ex_id = match.group("example_id")
        template = outline_lookup.get((raw_name, ex_id)) or match.group("name")
        return template, ex_id
    return raw_name, None


def _extract_scenario_error(scenario: dict) -> str:
    for step in scenario.get("steps") or ():
        if not isinstance(step, dict):
            continue
        err = str(step.get("error") or "").strip()
        if err:
            return _single_line(err)[:80]
    err = str(scenario.get("error") or "").strip()
    return _single_line(err)[:80]


def _count_kind(nodes: tuple[FeatureTreeNode, ...], kind: str) -> int:
    total = 0
    for node in nodes:
        if node.kind == kind:
            total += 1
        total += _count_kind(node.children, kind)
    return total


def _format_tags(tags: tuple[str, ...], max_items: int = 3) -> str:
    if not tags:
        return ""
    shown = [f"@{tag}" for tag in tags[:max_items]]
    if len(tags) > max_items:
        shown.append(f"+{len(tags) - max_items}")
    return " ".join(shown)


def _format_row_preview(headings: list[str], cells: list[str], max_len: int = 72) -> str:
    pairs = [f"{h}={c}" for h, c in zip(headings, cells)]
    text = ", ".join(pairs)
    if len(text) > max_len:
        return text[: max_len - 3].rstrip() + "..."
    return text


def _display_relpath(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _single_line(value: str) -> str:
    return " ".join(str(value or "").splitlines()).strip()
