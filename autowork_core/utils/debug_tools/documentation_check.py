from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

from autowork_core.utils.debug_tools.ai_paths import (
    FRAMEWORK_AI_ROOT,
    PROJECT_AI_ROOT,
    PROJECT_KNOWLEDGE_ROOT,
)
from autowork_core.utils.debug_tools.shadow_companion import (
    POLICY_PATH as SHADOW_COMPANION_POLICY_PATH,
    SHADOW_COMPANION_VERSION,
    STORE_PATH as SHADOW_COMPANION_STORE_PATH,
    load_shadow_companion_policy,
)
from autowork_core.utils.debug_tools.portable_knowledge import (
    MAX_QUERY_BYTES,
    MAX_QUERY_ITEMS,
    PORTABLE_KNOWLEDGE_VERSION,
    validate_portable_knowledge_schema,
)

DOCUMENTATION_CHECK_VERSION = "1.1"
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCED_CODE_BLOCK = re.compile(
    r"```(?P<language>python|ya?ml)[^\n]*\n(?P<body>.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)
MARKDOWN_PATTERNS = (
    "README.md",
    "docs/*.md",
    "docs/**/*.md",
    "ai/*.md",
    "ai/context/*.md",
    "ai/prompts/*.md",
    "ai/instructions/*.md",
    ".github/*.md",
    ".github/prompts/*.md",
    ".github/instructions/*.md",
)
NAVIGATION_GROUPS = (
    ("docs/1.快速开始.md", "docs"),
    ("docs/维护/1.维护者指南.md", "docs/维护"),
)
HUMAN_DOCUMENTATION_PATTERNS = (
    "README.md",
    "docs/*.md",
    "docs/**/*.md",
)


def inspect_documentation(project_root):
    root = Path(project_root).resolve()
    errors = []
    warnings = []
    sources = _markdown_sources(root)
    human_sources = _human_documentation_sources(root)
    links_checked = 0
    code_blocks_checked = 0
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as error:
            errors.append(
                f"文档无法读取: {_relative(source, root)}: "
                f"{type(error).__name__}: {error}"
            )
            continue
        for raw_target in MARKDOWN_LINK.findall(text):
            target = _link_target(raw_target)
            if target is None:
                continue
            links_checked += 1
            resolved = (source.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                errors.append(
                    f"文档链接越界: {_relative(source, root)} -> {raw_target}"
                )
                continue
            if not resolved.exists():
                errors.append(
                    f"文档链接不存在: {_relative(source, root)} -> "
                    f"{raw_target}"
                )
        if source in human_sources:
            block_errors, block_count = _validate_code_blocks(
                source,
                text,
                root,
            )
            errors.extend(block_errors)
            code_blocks_checked += block_count

    manifest_path = root / "ai" / "manifest.json"
    manifest_references = 0
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(
            f"AI manifest 无法读取: {type(error).__name__}: {error}"
        )
        manifest = {}
    project_assets_root_value = manifest.get("project_assets_root")
    project_assets_root = (root / str(
        project_assets_root_value or "__invalid_project_assets__"
    )).resolve()
    manifest_references += 1
    if (
        project_assets_root_value != PROJECT_AI_ROOT.as_posix()
        or not _contained(project_assets_root, root / "Bdd")
        or not project_assets_root.is_dir()
    ):
        errors.append(
            f"project_assets_root 必须为 {PROJECT_AI_ROOT.as_posix()}"
        )
    knowledge = manifest.get("knowledge") or {}
    if not isinstance(knowledge, dict):
        errors.append("knowledge 必须是 object")
        knowledge = {}
    knowledge_path = knowledge.get("path")
    quarantine_path = knowledge.get("quarantine_path")
    if not knowledge_path:
        errors.append("knowledge.path 不能为空")
        knowledge_root = project_assets_root / "__invalid_knowledge__"
    else:
        manifest_references += 1
        knowledge_root = (project_assets_root / str(knowledge_path)).resolve()
        if (
            not _contained(knowledge_root, project_assets_root)
            or not knowledge_root.is_dir()
        ):
            errors.append(f"knowledge.path 引用无效: {knowledge_path}")
    if not quarantine_path:
        errors.append("knowledge.quarantine_path 不能为空")
    else:
        manifest_references += 1
        quarantine = (project_assets_root / str(quarantine_path)).resolve()
        if not _contained(quarantine, knowledge_root):
            errors.append(
                f"knowledge.quarantine_path 引用无效: {quarantine_path}"
            )
    if knowledge.get("contains_raw_runtime_evidence") is not False:
        errors.append("knowledge.contains_raw_runtime_evidence 必须为 false")
    if knowledge.get("quarantine_in_portable_export") is not False:
        errors.append("knowledge.quarantine_in_portable_export 必须为 false")
    if knowledge.get("maintenance_requires_user_confirmation") is not True:
        errors.append(
            "knowledge.maintenance_requires_user_confirmation 必须为 true"
        )
    portable = manifest.get("portable_knowledge") or {}
    if not isinstance(portable, dict):
        errors.append("portable_knowledge 必须是 object")
        portable = {}
    portable_fields = {
        "attributes",
        "path",
        "records_path",
        "schema",
        "source",
        "version",
        "automatic_publish",
        "default_context_injection",
        "automatic_stage",
        "automatic_commit",
        "automatic_push",
        "sync_requires_user_confirmation",
        "query_max_items",
        "query_max_bytes",
    }
    if set(portable) != portable_fields:
        errors.append("portable_knowledge 字段无效")
    attributes_path = (root / str(
        portable.get("attributes") or "__invalid_attributes__"
    )).resolve()
    manifest_references += 1
    if (
        portable.get("attributes") != ".gitattributes"
        or attributes_path != (root / ".gitattributes").resolve()
        or not attributes_path.is_file()
    ):
        errors.append("portable_knowledge.attributes 引用无效")
    else:
        attributes = attributes_path.read_text(encoding="utf-8").splitlines()
        required_attribute = (
            "Bdd/ai/portable-knowledge/records/*.json text eol=lf"
        )
        if required_attribute not in attributes:
            errors.append(
                "portable_knowledge.attributes 缺少 records LF 规则"
            )
    portable_root = (project_assets_root / str(
        portable.get("path") or "__invalid_portable__"
    )).resolve()
    records_root = (project_assets_root / str(
        portable.get("records_path") or "__invalid_records__"
    )).resolve()
    portable_schema_path = (root / FRAMEWORK_AI_ROOT / str(
        portable.get("schema") or "__invalid_portable_schema__"
    )).resolve()
    portable_source = (project_assets_root / str(
        portable.get("source") or "__invalid_portable_source__"
    )).resolve()
    manifest_references += 4
    if (
        portable.get("path") != "portable-knowledge"
        or not _contained(portable_root, project_assets_root)
        or not portable_root.is_dir()
    ):
        errors.append("portable_knowledge.path 引用无效")
    if (
        portable.get("records_path") != "portable-knowledge/records"
        or not _contained(records_root, portable_root)
    ):
        errors.append("portable_knowledge.records_path 引用无效")
    if (
        portable.get("schema") != "context/portable-knowledge.schema.json"
        or not _contained(portable_schema_path, root / FRAMEWORK_AI_ROOT)
        or not portable_schema_path.is_file()
    ):
        errors.append("portable_knowledge.schema 引用无效")
    if (
        portable.get("source") != "knowledge/capabilities"
        or not _contained(portable_source, root / PROJECT_KNOWLEDGE_ROOT)
    ):
        errors.append("portable_knowledge.source 引用无效")
    if portable.get("version") != PORTABLE_KNOWLEDGE_VERSION:
        errors.append("portable_knowledge.version 必须为 1.0")
    for field in (
        "automatic_publish",
        "default_context_injection",
        "automatic_stage",
        "automatic_commit",
        "automatic_push",
    ):
        if portable.get(field) is not False:
            errors.append(f"portable_knowledge.{field} 必须为 false")
    if portable.get("sync_requires_user_confirmation") is not True:
        errors.append(
            "portable_knowledge.sync_requires_user_confirmation 必须为 true"
        )
    if portable.get("query_max_items") != MAX_QUERY_ITEMS:
        errors.append(
            f"portable_knowledge.query_max_items 必须为 {MAX_QUERY_ITEMS}"
        )
    if portable.get("query_max_bytes") != MAX_QUERY_BYTES:
        errors.append(
            f"portable_knowledge.query_max_bytes 必须为 {MAX_QUERY_BYTES}"
        )
    if portable_schema_path.is_file():
        try:
            portable_schema = json.loads(
                portable_schema_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            errors.append(
                "Portable Knowledge schema 无法读取: "
                f"{type(error).__name__}: {error}"
            )
        else:
            schema_errors = validate_portable_knowledge_schema(
                portable_schema
            )
            if schema_errors:
                errors.append(
                    f"Portable Knowledge schema 无效: {schema_errors}"
                )
    for relative in [
        *(manifest.get("context") or []),
        *(manifest.get("prompts") or []),
        *(manifest.get("instructions") or []),
    ]:
        manifest_references += 1
        path = (root / "ai" / str(relative)).resolve()
        if not _contained(path, root / "ai") or not path.is_file():
            errors.append(f"AI manifest 引用无效: {relative}")
    for relative in manifest.get("discovery_adapters") or []:
        manifest_references += 1
        path = (root / str(relative)).resolve()
        if not _contained(path, root) or not path.is_file():
            errors.append(f"AI discovery adapter 无效: {relative}")
    collaboration = manifest.get("collaboration_learning") or {}
    collaboration_fields = {
        "review_schema",
        "promotion_schema",
        "runtime_reviews",
        "runtime_promotions",
        "operator_scope",
        "manual_review",
        "manual_promotion",
        "automatic_due_reminder",
        "automatic_review",
        "automatic_promotion",
        "automatic_rule_mutation",
    }
    if set(collaboration) != collaboration_fields:
        errors.append("collaboration_learning 字段无效")
    collaboration_expected = {
        "review_schema": "context/collaboration-review.schema.json",
        "promotion_schema": "context/collaboration-promotion.schema.json",
        "runtime_reviews": "knowledge/collaboration-reviews",
        "runtime_promotions": "knowledge/collaboration-promotions",
        "operator_scope": "framework_maintainers",
        "manual_review": True,
        "manual_promotion": True,
        "automatic_due_reminder": False,
        "automatic_review": False,
        "automatic_promotion": False,
        "automatic_rule_mutation": False,
    }
    for key, expected_value in collaboration_expected.items():
        if collaboration.get(key) != expected_value:
            errors.append(f"collaboration_learning.{key} 无效")
    collaboration_paths = {
        "review_schema": (root / FRAMEWORK_AI_ROOT, True),
        "promotion_schema": (root / FRAMEWORK_AI_ROOT, True),
        "runtime_reviews": (project_assets_root, False),
        "runtime_promotions": (project_assets_root, False),
    }
    for key, (base, must_exist) in collaboration_paths.items():
        relative = collaboration.get(key)
        if not relative:
            continue
        manifest_references += 1
        path = (base / str(relative)).resolve()
        allowed_root = (
            root / PROJECT_KNOWLEDGE_ROOT
            if key.startswith("runtime_")
            else base
        )
        if not _contained(path, allowed_root) or (
            must_exist and not path.is_file()
        ):
            errors.append(
                f"collaboration_learning.{key} 引用无效: {relative}"
            )

    companion = manifest.get("shadow_companion")
    if not isinstance(companion, dict):
        errors.append("shadow_companion 必须是 object")
        companion = {}
    companion_fields = {
        "version",
        "policy",
        "runtime_store",
        "session_start_hook",
        "session_start_hook_windows_wrapper",
        "enabled",
        "scope",
        "task_confirmation_required",
        "write_mode",
        "session_start_detection",
        "default_context_injection",
        "local_only",
        "advisory_only",
        "restrict_search",
        "restrict_file_reads",
        "skip_tests",
        "select_final_validation",
        "mutate_instructions",
        "mutate_source",
        "record_source_content",
        "record_terminal_output",
        "automatic_git_operations",
        "single_active_capsule",
        "max_recent",
        "full_fallback",
    }
    if set(companion) != companion_fields:
        errors.append("shadow_companion 字段无效")
    companion_paths = {
        "policy": (
            root / FRAMEWORK_AI_ROOT,
            root / FRAMEWORK_AI_ROOT,
            True,
        ),
        "runtime_store": (
            project_assets_root,
            root / PROJECT_KNOWLEDGE_ROOT,
            False,
        ),
        "session_start_hook": (root, root / ".github" / "hooks", True),
        "session_start_hook_windows_wrapper": (
            root,
            root / ".github" / "hooks" / "scripts",
            True,
        ),
    }
    for key, (base, allowed_root, must_exist) in companion_paths.items():
        relative = companion.get(key)
        if not relative:
            continue
        manifest_references += 1
        path = (base / str(relative)).resolve()
        if not _contained(path, allowed_root) or (
            must_exist and not path.is_file()
        ):
            errors.append(
                f"shadow_companion.{key} 引用无效: {relative}"
            )
    if companion:
        expected = {
            "version": SHADOW_COMPANION_VERSION,
            "policy": SHADOW_COMPANION_POLICY_PATH.relative_to(
                FRAMEWORK_AI_ROOT
            ).as_posix(),
            "runtime_store": SHADOW_COMPANION_STORE_PATH.relative_to(
                PROJECT_AI_ROOT
            ).as_posix(),
            "session_start_hook": ".github/hooks/shadow-companion.json",
            "session_start_hook_windows_wrapper": (
                ".github/hooks/scripts/shadow-companion.ps1"
            ),
            "enabled": True,
            "scope": "project_engineering_work",
            "task_confirmation_required": False,
            "write_mode": "semantic_milestone_only",
            "session_start_detection": True,
            "default_context_injection": "validated_pointer_only",
            "local_only": True,
            "advisory_only": True,
            "restrict_search": False,
            "restrict_file_reads": False,
            "skip_tests": False,
            "select_final_validation": False,
            "mutate_instructions": False,
            "mutate_source": False,
            "record_source_content": False,
            "record_terminal_output": False,
            "automatic_git_operations": False,
            "single_active_capsule": True,
            "full_fallback": True,
        }
        for key, expected_value in expected.items():
            if companion.get(key) != expected_value:
                errors.append(f"shadow_companion.{key} 无效")
        try:
            policy = load_shadow_companion_policy(root)
        except (OSError, ValueError) as error:
            errors.append(f"shadow_companion.policy 无效: {error}")
        else:
            if companion.get("max_recent") != policy["max_recent"]:
                errors.append("shadow_companion.max_recent 与 policy 不一致")

    required_false = (
        "restrict_search",
        "restrict_file_reads",
        "skip_tests",
        "select_final_validation",
        "mutate_instructions",
        "mutate_source",
        "record_source_content",
    )
    for key in required_false:
        if companion and companion.get(key) is not False:
            errors.append(f"shadow_companion.{key} 必须为 false")

    hygiene = manifest.get("project_context_hygiene")
    if not isinstance(hygiene, dict):
        errors.append("project_context_hygiene 必须是 object")
        hygiene = {}
    hygiene_fields = {
        "mode",
        "scope",
        "semantic_milestones",
        "automatic_context_discard",
        "automatic_session_switch",
        "fixed_budget_cutoff",
        "persistent_runtime_state",
        "raw_output_reference_required",
        "capability_restrictions",
        "full_fallback",
    }
    if set(hygiene) != hygiene_fields:
        errors.append("project_context_hygiene 字段无效")
    if hygiene:
        if hygiene.get("mode") != "reversible_advisory":
            errors.append(
                "project_context_hygiene.mode 必须为 reversible_advisory"
            )
        if hygiene.get("scope") != "project_engineering_work":
            errors.append(
                "project_context_hygiene.scope 必须为 project_engineering_work"
            )
        for key in (
            "semantic_milestones",
            "raw_output_reference_required",
            "full_fallback",
        ):
            if hygiene.get(key) is not True:
                errors.append(f"project_context_hygiene.{key} 必须为 true")
        for key in (
            "automatic_context_discard",
            "automatic_session_switch",
            "fixed_budget_cutoff",
            "persistent_runtime_state",
            "capability_restrictions",
        ):
            if hygiene.get(key) is not False:
                errors.append(f"project_context_hygiene.{key} 必须为 false")

    adaptive = manifest.get("adaptive_work_loop")
    if not isinstance(adaptive, dict):
        errors.append("adaptive_work_loop 必须是 object")
        adaptive = {}
    adaptive_fields = {
        "enabled",
        "mode",
        "scope",
        "milestone_driven",
        "persistent_runtime_state",
        "fixed_turn_or_tool_cutoff",
        "restrict_search",
        "restrict_file_reads",
        "skip_tests",
        "reuse_stale_validation",
        "allow_validation_escalation",
        "raw_output_in_default_context",
        "shadow_handoff_optional",
        "full_fallback",
    }
    if set(adaptive) != adaptive_fields:
        errors.append("adaptive_work_loop 字段无效")
    if adaptive:
        if adaptive.get("mode") != "adaptive_advisory":
            errors.append(
                "adaptive_work_loop.mode 必须为 adaptive_advisory"
            )
        if adaptive.get("scope") != "recorder_maintenance":
            errors.append(
                "adaptive_work_loop.scope 必须为 recorder_maintenance"
            )
        for key in (
            "enabled",
            "milestone_driven",
            "allow_validation_escalation",
            "shadow_handoff_optional",
            "full_fallback",
        ):
            if adaptive.get(key) is not True:
                errors.append(f"adaptive_work_loop.{key} 必须为 true")
        for key in (
            "persistent_runtime_state",
            "fixed_turn_or_tool_cutoff",
            "restrict_search",
            "restrict_file_reads",
            "skip_tests",
            "reuse_stale_validation",
            "raw_output_in_default_context",
        ):
            if adaptive.get(key) is not False:
                errors.append(f"adaptive_work_loop.{key} 必须为 false")

    retired_collaboration_hooks = (
        ".github/hooks/collaboration-review.json",
        ".github/hooks/scripts/collaboration-review.ps1",
    )
    for relative in retired_collaboration_hooks:
        if (root / relative).is_file():
            errors.append(f"Collaboration 自动提醒入口已退役: {relative}")

    for hook_path in (root / ".github" / "hooks").glob("*.json"):
        try:
            hook_value = json.loads(hook_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(
                f"Hook 配置无法读取: {_relative(hook_path, root)}: "
                f"{type(error).__name__}: {error}"
            )
            continue
        for entries in (hook_value.get("hooks") or {}).values():
            if not isinstance(entries, list):
                errors.append(
                    f"Hook entries 必须是 array: {_relative(hook_path, root)}"
                )
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    errors.append(
                        f"Hook entry 无效: {_relative(hook_path, root)}"
                    )
                    continue
                cwd = entry.get("cwd", ".")
                cwd_path = (root / str(cwd)).resolve()
                if not _contained(cwd_path, root):
                    errors.append(
                        f"Hook cwd 越界: {_relative(hook_path, root)} -> {cwd}"
                    )

    missing_navigation = []
    for navigation_relative, directory_relative in NAVIGATION_GROUPS:
        path = root / navigation_relative
        if not path.is_file():
            errors.append(f"文档导航文件不存在: {navigation_relative}")
            continue
        navigation_targets = set()
        documentation_root = (root / directory_relative).resolve()
        text = path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = _link_target(raw_target)
            if target is None:
                continue
            resolved = (path.parent / target).resolve()
            if resolved.parent == documentation_root:
                navigation_targets.add(resolved.name)
        documentation_files = {
            document.name
            for document in documentation_root.glob("*.md")
            if document.resolve() != path.resolve()
        }
        group_missing = sorted(documentation_files - navigation_targets)
        if group_missing:
            group_missing_paths = [
                f"{directory_relative}/{name}"
                for name in group_missing
            ]
            missing_navigation.extend(group_missing_paths)
            errors.append(
                f"正式文档未被 {navigation_relative} 收录: "
                f"{group_missing_paths}"
            )

    return {
        "documentation_check_version": DOCUMENTATION_CHECK_VERSION,
        "status": "passed" if not errors else "invalid",
        "project_root": ".",
        "summary": {
            "markdown_files": len(sources),
            "links_checked": links_checked,
            "code_blocks_checked": code_blocks_checked,
            "manifest_references": manifest_references,
            "unlisted_documents": len(missing_navigation),
        },
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
    }


def _markdown_sources(root):
    result = set()
    for pattern in MARKDOWN_PATTERNS:
        result.update(path.resolve() for path in root.glob(pattern))
    return sorted(path for path in result if path.is_file())


def _human_documentation_sources(root):
    result = set()
    for pattern in HUMAN_DOCUMENTATION_PATTERNS:
        result.update(path.resolve() for path in root.glob(pattern))
    return {path for path in result if path.is_file()}


def _validate_code_blocks(source, text, root):
    errors = []
    count = 0
    for match in FENCED_CODE_BLOCK.finditer(text):
        count += 1
        language = match.group("language").casefold()
        body = match.group("body")
        block_line = text.count("\n", 0, match.start()) + 1
        try:
            if language == "python":
                ast.parse(body, filename=str(source))
            else:
                yaml.safe_load(body)
        except (SyntaxError, yaml.YAMLError) as error:
            relative = _relative(source, root)
            detail = getattr(error, "msg", None) or str(error).splitlines()[0]
            errors.append(
                f"文档 {language} 代码块无效: {relative}:{block_line}: "
                f"{detail}"
            )
    return errors, count


def _link_target(raw_target):
    value = raw_target.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    value = value.split("#", 1)[0].strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme or value.startswith("//"):
        return None
    return Path(unquote(value))


def _contained(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def _relative(path, root):
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check BDD Autowork documentation links and indexes",
    )
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    report = inspect_documentation(args.project_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())