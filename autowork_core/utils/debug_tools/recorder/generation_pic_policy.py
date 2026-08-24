from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    excluded_api_names,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    resolve_session_path,
)


PIC_POLICY_VERSION = "1.0"
DIRECT_PIC_APIS = frozenset(excluded_api_names("direct_pic"))


def snapshot_pic_policy(project_root):
    project_root = Path(project_root).resolve()
    findings = []
    for root, suffixes in (
        (Path("Bdd/locators"), {".yaml", ".yml"}),
        (Path("Bdd/steps"), {".py"}),
        (Path("Bdd/page_obj"), {".py"}),
    ):
        directory = project_root / root
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            if path.suffix.casefold() not in suffixes:
                continue
            findings.extend(_pic_findings(project_root, path))
    counts = Counter(item["fingerprint"] for item in findings)
    return {
        "pic_policy_version": PIC_POLICY_VERSION,
        "finding_counts": dict(sorted(counts.items())),
        "finding_count": len(findings),
    }


def validate_pic_authorizations(session_dir, request, plan_artifact):
    plan = plan_artifact.get("plan") or {}
    authorizations = plan.get("pic_authorizations") or []
    errors = []
    validated = []
    action_steps, action_targets, action_locator_files = _plan_action_scope(plan)
    evidence = {
        str((item.get("step") or {}).get("id") or ""): item
        for item in request.get("evidence") or ()
    }
    seen_actions = set()
    for authorization in authorizations:
        if not authorization.get("authorized"):
            continue
        action_id = str(authorization.get("action_id") or "")
        step_id = str(authorization.get("step_id") or "")
        action_key = (step_id, action_id)
        audit_errors = []
        if not action_id or action_key in seen_actions:
            audit_errors.append("PIC authorization action_id 缺失或重复")
        seen_actions.add(action_key)
        if action_key not in action_targets:
            audit_errors.append("PIC authorization 与 Plan Step/Action 不匹配")
        target = action_targets.get(action_key)
        if not target:
            audit_errors.append("PIC authorization action 缺少 Plan target")
        locator_file = _normalize_locator_path(
            action_locator_files.get(action_key)
        )
        if locator_file is None:
            audit_errors.append("PIC authorization action 缺少合法 locator_file")
        entry = evidence.get(step_id) or {}
        artifacts = entry.get("artifacts") or {}
        hashes = entry.get("artifact_hashes") or {}
        template_path = authorization.get("template_request_path")
        audit_path = authorization.get("audit_request_path")
        if template_path not in artifacts.values():
            audit_errors.append("PIC template 不属于当前 Step 的 Request artifacts")
        if audit_path != artifacts.get("pic_template_audit"):
            audit_errors.append("PIC audit 不属于当前 Step 的 Request artifacts")
        template_key = f"pic_template:{authorization.get('candidate_id')}"
        if artifacts.get(template_key) != template_path:
            audit_errors.append("PIC candidate 与 template artifact key 不匹配")
        expected_template_hash = hashes.get(template_key)
        if any((
            not expected_template_hash,
            authorization.get("template_sha256") != expected_template_hash,
            authorization.get("template_request_sha256") != expected_template_hash,
        )):
            audit_errors.append("PIC template hash 未完整绑定 Request")
        if authorization.get("audit_request_sha256") != hashes.get(
                "pic_template_audit"
        ):
            audit_errors.append("PIC audit hash 未绑定 Request")
        semantic_candidate = _semantic_candidate(
            session_dir,
            artifacts.get("semantic_pack"),
            authorization.get("candidate_id"),
        )
        audit_record = _audit_record(
            session_dir,
            audit_path,
            authorization.get("candidate_id"),
        )
        if semantic_candidate is None:
            audit_errors.append("PIC candidate 不存在于 sealed Semantic Pack")
        if audit_record is None:
            audit_errors.append("PIC candidate 不存在于 sealed template audit")
        else:
            expected_fields = {
                "audit_id": audit_record.get("audit_id"),
                "audit_status": audit_record.get("status"),
                "template_sha256": (
                    audit_record.get("template") or {}
                ).get("sha256"),
                "region": audit_record.get("region"),
                "cross_frame_validation": audit_record.get("validation"),
            }
            for field, expected in expected_fields.items():
                if authorization.get(field) != expected:
                    audit_errors.append(
                        f"PIC authorization {field} 与 sealed audit 不一致"
                    )
        if semantic_candidate is not None:
            for field in (
                "region_locator_name",
                "region_locator",
                "source_frame",
                "crop_rectangle",
            ):
                if authorization.get(field) != semantic_candidate.get(field):
                    audit_errors.append(
                        f"PIC authorization {field} 与 sealed Semantic Pack 不一致"
                    )
        if authorization.get("audit_status") != "passed":
            audit_errors.append("PIC template audit 未通过")
        validation = authorization.get("cross_frame_validation") or {}
        if any((
            validation.get("cross_frame_unique_match") is not True,
            int(validation.get("validated_frame_count") or 0) < 2,
        )):
            audit_errors.append("PIC 跨帧唯一匹配未通过")
        region_name = str(authorization.get("region_locator_name") or "")
        if not region_name or not authorization.get("region"):
            audit_errors.append("PIC authorization 缺少命名 Region")
        target_data_path = _normalize_data_path(
            authorization.get("target_data_path")
        )
        if target_data_path is None:
            audit_errors.append("PIC target_data_path 非法")
        source = None
        if template_path:
            try:
                source = resolve_session_path(session_dir, template_path)
                if _sha256(source) != expected_template_hash:
                    audit_errors.append("PIC template 文件 hash 与 Request 不一致")
            except Exception as error:
                audit_errors.append(
                    f"PIC template 无法读取: {type(error).__name__}: {error}"
                )
        if audit_path:
            try:
                audit_file = resolve_session_path(session_dir, audit_path)
                if _sha256(audit_file) != hashes.get("pic_template_audit"):
                    audit_errors.append("PIC audit 文件 hash 与 Request 不一致")
            except Exception as error:
                audit_errors.append(
                    f"PIC audit 无法读取: {type(error).__name__}: {error}"
                )
        record = {
            "authorization_id": authorization.get("authorization_id"),
            "action_id": action_id,
            "step_id": step_id,
            "locator_name": target,
            "locator_file": locator_file,
            "candidate_id": authorization.get("candidate_id"),
            "template_source": str(source) if source else None,
            "template_sha256": expected_template_hash,
            "target_data_path": target_data_path,
            "region_locator_name": region_name,
            "region_locator": authorization.get("region_locator"),
            "region": authorization.get("region"),
            "threshold": validation.get("threshold"),
            "audit_id": authorization.get("audit_id"),
            "status": "passed" if not audit_errors else "failed",
            "errors": audit_errors,
        }
        errors.extend(
            f"PIC authorization {record['authorization_id']}: {error}"
            for error in audit_errors
        )
        validated.append(record)
    return errors, {
        "pic_policy_version": PIC_POLICY_VERSION,
        "status": "passed" if not errors else "failed",
        "authorization_count": len(validated),
        "authorizations": validated,
        "errors": errors,
    }


def validate_generated_pic_usage(
        project_root,
        changed_files,
        frozen_audit,
        baseline=None,
    ):
    project_root = Path(project_root).resolve()
    authorizations = {
        item.get("locator_name"): item
        for item in (frozen_audit or {}).get("authorizations") or ()
        if item.get("status") == "passed" and item.get("locator_name")
    }
    errors = []
    locator_records = []
    locator_documents = {}
    direct_calls = []
    baseline_counts = Counter((baseline or {}).get("finding_counts") or {})
    current_counts = Counter()
    expected_locator_files = {
        item.get("locator_file")
        for item in authorizations.values()
        if item.get("locator_file")
    }
    for relative in expected_locator_files:
        path = (project_root / relative).resolve()
        if not path.is_file():
            continue
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(value, dict):
            locator_documents[relative] = {
                str(name): locator
                for name, locator in value.items()
            }
    for relative in changed_files or ():
        path = (project_root / relative).resolve()
        if not path.is_file():
            continue
        if (
            Path("Bdd/locators") in Path(relative).parents
            and path.suffix.casefold() in {".yaml", ".yml"}
        ):
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            relative_key = Path(relative).as_posix()
            locator_documents[relative_key] = {
                str(name): locator
                for name, locator in value.items()
            }
            for name, locator in value.items():
                if not isinstance(locator, dict) or str(
                        locator.get("by") or ""
                ).casefold() != "pic":
                    continue
                record = {
                    "path": str(relative),
                    "locator_name": str(name),
                    "file": locator.get("file") or locator.get("value"),
                    "region": locator.get("region"),
                    "threshold": locator.get("threshold"),
                }
                locator_records.append(record)
                authorization = authorizations.get(str(name))
                if authorization is None:
                    fingerprint = _pic_locator_fingerprint(
                        relative_key,
                        str(name),
                        locator,
                    )
                    current_counts[fingerprint] += 1
                    if current_counts[fingerprint] > baseline_counts[fingerprint]:
                        errors.append(f"生成了未授权 PIC locator: {name}")
                    continue
                if _normalize_data_path(record["file"]) != authorization.get(
                        "target_data_path"
                ):
                    errors.append(f"PIC locator 模板路径不符合授权: {name}")
                if str(record.get("region") or "") != authorization.get(
                        "region_locator_name"
                ):
                    errors.append(f"PIC locator Region 不符合授权: {name}")
                expected_threshold = authorization.get("threshold")
                actual_threshold = record.get("threshold")
                try:
                    threshold_matches = (
                        actual_threshold is not None
                        and expected_threshold is not None
                        and float(actual_threshold) == float(expected_threshold)
                    )
                except (TypeError, ValueError):
                    threshold_matches = False
                if not threshold_matches:
                    errors.append(f"PIC locator threshold 不符合授权: {name}")
        elif (
            Path("Bdd/steps") in Path(relative).parents
            or Path("Bdd/page_obj") in Path(relative).parents
        ) and path.suffix.casefold() == ".py":
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            except (OSError, SyntaxError, UnicodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node.func)
                if name in DIRECT_PIC_APIS:
                    record = {
                        "path": str(relative),
                        "line": getattr(node, "lineno", 0),
                        "name": name,
                    }
                    direct_calls.append(record)
                    fingerprint = _direct_call_fingerprint(
                        Path(relative).as_posix(),
                        node,
                    )
                    current_counts[fingerprint] += 1
                    if current_counts[fingerprint] > baseline_counts[fingerprint]:
                        errors.append(
                            f"受控 PIC 禁止直接调用 {name}: "
                            f"{relative}:{getattr(node, 'lineno', 0)}"
                        )
    for locator_name, authorization in authorizations.items():
        target_path = (
            project_root / "Bdd" / "data" / authorization["target_data_path"]
        ).resolve()
        if not target_path.is_file():
            errors.append(f"授权 PIC 模板未写入 Bdd/data: {locator_name}")
        elif _sha256(target_path) != authorization.get("template_sha256"):
            errors.append(f"授权 PIC 模板 hash 被改变: {locator_name}")
        locator_file = authorization.get("locator_file")
        expected_document = locator_documents.get(locator_file) or {}
        pic_locator = expected_document.get(locator_name)
        region_name = authorization.get("region_locator_name")
        if not isinstance(pic_locator, dict) or str(
                pic_locator.get("by") or ""
        ).casefold() != "pic":
            errors.append(f"授权 PIC locator 未生成: {locator_name}")
        if _canonical_locator(expected_document.get(region_name)) != _canonical_locator(
                authorization.get("region_locator")
            ):
                errors.append(f"授权 PIC Region locator 不符合录制证据: {locator_name}")
    return errors, {
        "pic_policy_version": PIC_POLICY_VERSION,
        "status": "passed" if not errors else "failed",
        "authorized_count": len(authorizations),
        "pic_locators": locator_records,
        "direct_pic_calls": direct_calls,
        "baseline_finding_count": sum(baseline_counts.values()),
        "errors": errors,
    }


def _plan_action_scope(plan):
    targets = {}
    locator_files = {}
    for step_id, step in (plan.get("steps") or {}).items():
        for operation in step.get("operations") or ():
            for action_id in operation.get("action_ids") or ():
                key = (str(step_id), str(action_id))
                targets[key] = operation.get("target")
                locator_files[key] = step.get("locator_file")
    return None, targets, locator_files


def _normalize_data_path(value):
    if not value:
        return None
    path = Path(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    parts = list(path.parts)
    if len(parts) >= 2 and parts[:2] == ["Bdd", "data"]:
        parts = parts[2:]
    normalized = Path(*parts).as_posix()
    return normalized if normalized.startswith("recorder_pic/") else None


def _normalize_locator_path(value):
    if not value:
        return None
    path = Path(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix()
    return normalized if normalized.startswith("Bdd/locators/") else None


def _call_name(value):
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Name):
        return value.id
    return ""


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _semantic_candidate(session_dir, relative, candidate_id):
    if not relative:
        return None
    try:
        value = _read_yaml_or_json(resolve_session_path(session_dir, relative))
    except Exception:
        return None
    return next((
        fallback.get("pic_candidate")
        for fallback in value.get("locator_fallback_candidates") or ()
        if (fallback.get("pic_candidate") or {}).get("candidate_id")
        == candidate_id
    ), None)


def _audit_record(session_dir, relative, candidate_id):
    if not relative:
        return None
    try:
        value = _read_yaml_or_json(resolve_session_path(session_dir, relative))
    except Exception:
        return None
    return next((
        item
        for item in value.get("audits") or ()
        if item.get("candidate_id") == candidate_id
    ), None)


def _read_yaml_or_json(path):
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"artifact 必须是 object: {path}")
    return value


def _canonical_locator(value):
    if not isinstance(value, dict):
        return None
    return {
        str(key): item
        for key, item in value.items()
        if key not in {"validation"}
    }


def _pic_findings(project_root, path):
    relative = Path(path).resolve().relative_to(project_root).as_posix()
    if Path(path).suffix.casefold() in {".yaml", ".yml"}:
        try:
            value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except Exception:
            return []
        return [
            {
                "fingerprint": _pic_locator_fingerprint(
                    relative,
                    str(name),
                    locator,
                ),
            }
            for name, locator in value.items()
            if isinstance(locator, dict)
            and str(locator.get("by") or "").casefold() == "pic"
        ]
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []
    return [
        {"fingerprint": _direct_call_fingerprint(relative, node)}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) in DIRECT_PIC_APIS
    ]


def _pic_locator_fingerprint(path, name, locator):
    canonical = json.dumps(
        locator,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        f"{path}|pic_locator|{name}|{canonical}".encode("utf-8")
    ).hexdigest()


def _direct_call_fingerprint(path, node):
    canonical = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(
        f"{path}|direct_pic_call|{canonical}".encode("utf-8")
    ).hexdigest()