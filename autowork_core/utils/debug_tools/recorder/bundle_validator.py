from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
)
from autowork_core.utils.debug_tools.recorder.code_reuse_index import (
    build_code_reuse_index,
    build_window_asset_catalog,
)
from autowork_core.utils.debug_tools.recorder.models import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.raw_event_journal import (
    requires_capture_integrity,
    validate_capture_integrity,
)
from autowork_core.utils.debug_tools.recorder.evidence_recovery import (
    enrich_review_recovery,
)


def validate_ai_bundle(session_dir):
    session_dir = Path(session_dir).resolve()
    errors = []
    warnings = []
    stats = {
        "selected_steps": 0,
        "completed_steps": 0,
        "skipped_steps": 0,
        "pending_steps": 0,
        "linked_events": 0,
        "locator_drafts": 0,
        "review_required": 0,
    }
    review_required = []
    window_catalog = _load_window_asset_catalog(session_dir)

    manifest = _read_json(session_dir / "manifest.json", errors)
    context = _read_json(session_dir / "ai" / "context.json", errors)
    if manifest.get("schema_version") == "2.0":
        target_index = {}
    else:
        target_index = _read_json(session_dir / "ai" / "target-index.json", errors)
    contract = _read_json(session_dir / "ai" / "generation-contract.json", errors)
    locator_drafts = _read_yaml(session_dir / "ai" / "locator-drafts.yaml", errors)
    versioned_values = [
        ("manifest", manifest),
        ("context", context),
        ("generation contract", contract),
        ("locator drafts", locator_drafts),
    ]
    if target_index:
        versioned_values.append(("target index", target_index))
    for label, value in versioned_values:
        if value and value.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
            errors.append(
                f"{label} schema_version 不兼容: "
                f"{value.get('schema_version')!r} not in {SUPPORTED_SCHEMA_VERSIONS!r}"
            )

    if contract:
        excluded = (contract.get("locator_contract") or {}).get("excluded") or []
        if "pic" not in excluded:
            errors.append("generation contract 未从普通 locator 候选中排除 PIC")
    if locator_drafts:
        locators = locator_drafts.get("locators") or {}
        stats["locator_drafts"] = len(locators)
        for name, locator in locators.items():
            if isinstance(locator, dict) and locator.get("by") == "pic":
                errors.append(f"普通 locator draft 包含未授权 PIC: {name}")

    steps = (context or {}).get("steps") or []
    annotation_repository = RecordingAnnotationRepository(session_dir)
    try:
        annotation_records = annotation_repository.load()
        manifest_step_ids = {
            str((entry.get("plan") or {}).get("id") or "")
            for entry in manifest.get("steps") or ()
        }
        unknown_annotation_steps = sorted({
            str(record.get("step_id") or "")
            for record in annotation_records
            if str(record.get("step_id") or "") not in manifest_step_ids
        })
        if unknown_annotation_steps:
            errors.append(
                "Annotation引用录制范围外Step: "
                + ", ".join(unknown_annotation_steps)
            )
        _validate_observation_annotation_scopes(
            session_dir,
            manifest,
            annotation_records,
            errors,
        )
        for path in session_dir.glob(
                "steps/**/takes/**/supplements/"
                "supplement-*/recording-annotations.jsonl"
        ):
            supplement_records = RecordingAnnotationRepository(
                path.parent
            ).load()
            if any(
                    record.get("annotation_type") == "step_user_context"
                    for record in supplement_records
            ):
                errors.append(
                    "Supplement annotation不能声明StepUserContext: "
                    f"{path.relative_to(session_dir).as_posix()}"
                )
            _validate_observation_annotation_scopes(
                session_dir,
                manifest,
                supplement_records,
                errors,
            )
        for step in steps:
            step_id = str((step.get("step") or {}).get("id") or "")
            projected = step.get("step_user_context")
            current = annotation_repository.current_step_context(step_id)
            if projected != current:
                errors.append(
                    "StepUserContext投影与append-only记录不一致: "
                    f"step={step_id}"
                )
    except (OSError, ValueError) as error:
        errors.append(
            "recording-annotations.jsonl无效: "
            f"{type(error).__name__}: {error}"
        )
    stats["selected_steps"] = len(steps)
    for step in steps:
        status = step.get("status")
        if status == "completed":
            stats["completed_steps"] += 1
        elif status == "skipped":
            stats["skipped_steps"] += 1
            continue
        else:
            stats["pending_steps"] += 1
            continue

        artifacts = step.get("artifacts") or {}
        for key in (
            "take",
            "events",
            "actions",
            "tree_diff",
            "locator_candidates",
            "media_index",
            "summary",
        ):
            relative_path = artifacts.get(key)
            if not relative_path:
                errors.append(f"完成 Step 缺少 artifact 索引: step={step.get('step', {}).get('id')}, key={key}")
                continue
            if not (session_dir / relative_path).exists():
                errors.append(f"artifact 文件不存在: {relative_path}")
        if manifest.get("schema_version") != "2.0":
            for key in (
                "actions_auto",
                "actions_effective",
                "timeline_state",
                "locator_candidates_auto",
                "locator_candidates_effective",
            ):
                relative_path = artifacts.get(key)
                if not relative_path:
                    errors.append(
                        f"2.1 完成 Step 缺少 artifact 索引: "
                        f"step={step.get('step', {}).get('id')}, key={key}"
                    )
                elif not (session_dir / relative_path).exists():
                    errors.append(f"artifact 文件不存在: {relative_path}")

        media_path = artifacts.get("media_index")
        if media_path and (session_dir / media_path).exists():
            media = _read_json(session_dir / media_path, errors)
            stats["linked_events"] += len((media or {}).get("events") or [])
            _validate_media_paths(session_dir / artifacts["take"], media, errors)
        action_media_path = artifacts.get("action_media")
        if action_media_path:
            action_media = _read_json(session_dir / action_media_path, errors)
            _validate_action_media_paths(
                session_dir / artifacts["take"],
                action_media,
                errors,
            )
        semantic_path = artifacts.get("semantic_pack")
        if semantic_path and (session_dir / semantic_path).is_file():
            semantic_pack = _read_json(
                session_dir / semantic_path,
                errors,
            )
            projected_intents = [
                dict(item)
                for item in step.get("observation_intents") or ()
                if isinstance(item, dict)
            ]
            semantic_intents = [
                dict(item)
                for item in semantic_pack.get("observation_intents") or ()
                if isinstance(item, dict)
            ]
            if projected_intents != semantic_intents:
                errors.append(
                    "ObservationIntent投影与Semantic Pack不一致: "
                    f"step={(step.get('step') or {}).get('id')}"
                )

        _review_step_semantics(
            session_dir,
            step,
            review_required,
            errors,
            window_catalog=window_catalog,
        )

    _review_root_variants(locator_drafts, review_required)
    enrich_review_recovery(session_dir, steps, review_required)
    stats["review_required"] = len(review_required)
    stats["hard_recovery_blockers"] = sum(
        bool((item.get("recovery") or {}).get("hard_blocker"))
        for item in review_required
    )
    semantic_ready = not any(
        bool((item.get("recovery") or {}).get("hard_blocker"))
        for item in review_required
    )
    recording_complete = stats["completed_steps"] > 0 and stats["pending_steps"] == 0

    generation_ready = (
        not errors
        and recording_complete
        and semantic_ready
    )
    if not errors and stats["completed_steps"] == 0:
        warnings.append("没有已完成的 Step，证据包结构有效但尚不能生成脚本")
    if stats["pending_steps"]:
        warnings.append(f"仍有 {stats['pending_steps']} 个 Step 未完成")
    if review_required:
        warnings.append(f"有 {len(review_required)} 项语义证据需要复核")
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_valid": not errors,
        "recording_complete": recording_complete,
        "semantic_ready": semantic_ready,
        "generation_ready": generation_ready,
        "errors": errors,
        "warnings": warnings,
        "review_required": review_required,
        "stats": stats,
    }


def _validate_observation_annotation_scopes(
        session_dir,
        manifest,
        records,
        errors,
    ):
    scopes = _recorded_observation_event_scopes(
        session_dir,
        manifest,
        errors,
    )
    for record in records:
        if record.get("annotation_type") != "observation_intent":
            continue
        scope = (
            str(record.get("step_id") or ""),
            str(record.get("take_id") or ""),
        )
        event_id = str(record.get("event_id") or "")
        events = scopes.get(scope)
        if events is None:
            errors.append(
                "ObservationIntent引用未知Step/Take scope: "
                f"step={scope[0]}, take={scope[1]}"
            )
            continue
        if events.get(event_id) != "observation":
            errors.append(
                "ObservationIntent引用的event不是F9 observation: "
                f"take={scope[1]}, event={event_id}"
            )


def _recorded_observation_event_scopes(session_dir, manifest, errors):
    scopes = {}
    for step_entry in manifest.get("steps") or ():
        step_id = str((step_entry.get("plan") or {}).get("id") or "")
        for take in step_entry.get("takes") or ():
            take_id = str(take.get("id") or "")
            take_path = take.get("path")
            if not take_id or not take_path:
                continue
            take_dir = session_dir / str(take_path)
            scopes[(step_id, take_id)] = {
                str(event.get("id") or ""): event.get("event_type")
                for event in _read_jsonl(take_dir / "events.jsonl", errors)
            }
            supplement_root = take_dir / "supplements"
            if not supplement_root.is_dir():
                continue
            for metadata_path in supplement_root.glob(
                    "supplement-*/supplement.json"
            ):
                metadata = _read_json(metadata_path, errors)
                supplement_id = str(
                    metadata.get("supplement_id") or ""
                )
                if not supplement_id:
                    continue
                scopes[(step_id, supplement_id)] = {
                    str(event.get("id") or ""): event.get("event_type")
                    for event in _read_jsonl(
                        metadata_path.parent / "events.jsonl",
                        errors,
                    )
                }
    return scopes


def _review_step_semantics(
    session_dir,
    step,
    review_required,
    errors,
    *,
    window_catalog=None,
):
    step_plan = step.get("step") or {}
    step_id = step_plan.get("id")
    artifacts = step.get("artifacts") or {}
    take_path = artifacts.get("take")
    if not take_path:
        return
    take_dir = session_dir / take_path
    take = _read_json(take_dir / "take.json", errors)
    if requires_capture_integrity(take_dir, take):
        base_events = _read_jsonl(take_dir / "events.jsonl", errors)
        integrity = validate_capture_integrity(
            take_dir,
            [str(event.get("id") or "") for event in base_events],
        )
        if integrity.get("status") != "complete":
            errors.append(
                "Take 原始事件完整性校验失败: "
                + "; ".join(integrity.get("errors") or ())
            )
    _validate_window_evidence(take_dir, take, errors)
    projection_store = ProjectionStore(take_dir)
    projection = projection_store.current()
    effective_actions_path = projection_store.artifact_path(
        "actions_effective",
        legacy=("actions.effective.json", "actions.json"),
        snapshot=projection,
    )
    actions_data = _read_json(effective_actions_path, errors)
    tree_diff = _read_json(take_dir / "ui" / "tree-diff.json", errors)
    effective_locator_path = projection_store.artifact_path(
        "locator_candidates_effective",
        legacy=(
            "locator-candidates.effective.yaml",
            "locator-candidates.yaml",
        ),
        snapshot=projection,
    )
    locator_bundle = _read_yaml(effective_locator_path, errors)
    effective_events_path = projection_store.artifact_path(
        "events_effective",
        legacy=("events.effective.jsonl", "events.jsonl"),
        snapshot=projection,
    )
    events = _read_jsonl(effective_events_path, errors)
    event_map = {event.get("id"): event for event in events}
    target_map = {
        target.get("event_id"): target
        for target in locator_bundle.get("event_targets") or []
    }

    if take.get("capture_error"):
        _add_review(
            review_required,
            step_id,
            "capture_error",
            "录制过程存在采集错误",
            take.get("capture_error"),
        )

    actions = actions_data.get("actions") or []
    if not actions:
        _add_review(
            review_required,
            step_id,
            "no_recorded_actions",
            "完成的 Step 没有可解释动作或观察证据",
            None,
        )
    active_event_ids = {
        event_id
        for action in actions_data.get("actions") or []
        for event_id in action.get("event_ids") or ()
    }
    first_active_event = next(
        (event for event in events if event.get("id") in active_event_ids),
        None,
    )
    if first_active_event and first_active_event.get("event_type") == "mouse_up":
        _add_review(
            review_required,
            step_id,
            "orphan_mouse_boundary",
            "录制从孤立 mouse_up 开始，可能在录制启动前已开始一次点击",
            first_active_event.get("id"),
        )

    primary_window_evidence = next(
        (
            item
            for item in take.get("window_evidence") or []
            if item.get("primary")
        ),
        {},
    )
    primary_closed = bool(primary_window_evidence.get("closed_during_take"))
    comparable = tree_diff.get("comparable")
    if comparable is None:
        before = _read_json(take_dir / "ui" / "before-tree.json", errors)
        after = _read_json(take_dir / "ui" / "after-tree.json", errors)
        comparable = (
            before.get("window_handle")
            and before.get("window_handle") == after.get("window_handle")
        )
    if comparable is not True and not primary_closed:
        _add_review(
            review_required,
            step_id,
            "tree_not_comparable",
            "Step 前后控件树不是同一个目标窗口，不能据此生成断言",
            tree_diff.get("comparison_reason"),
        )

    window_evidence_by_handle = {}
    for item in take.get("window_evidence") or []:
        handle = _window_handle_key((item.get("window") or {}).get("handle"))
        if handle is not None:
            window_evidence_by_handle[handle] = item
    reviewed_closed_handles = set()
    for lifecycle in take.get("window_lifecycle") or []:
        canonical_window = _canonical_window_match(
            window_catalog or {},
            lifecycle,
        )
        if (
            lifecycle.get("admission") == "provisional"
            and canonical_window is None
        ):
            _add_review(
                review_required,
                step_id,
                "provisional_window",
                "录制中操作了未预选且无进程关系的新窗口，需要确认是否属于业务流程",
                lifecycle,
            )
        if (
            lifecycle.get("closed_during_take")
            and _closed_window_requires_review(
                lifecycle,
                window_evidence_by_handle,
                active_event_ids,
            )
        ):
            handle = _window_handle_key(lifecycle.get("handle"))
            window_evidence = window_evidence_by_handle.get(handle) or {}
            reviewed_closed_handles.add(handle)
            _add_review(
                review_required,
                step_id,
                "window_closed_during_take",
                "窗口在 Step 中关闭，需要确认关闭是否为预期业务结果",
                {
                    **lifecycle,
                    "primary": bool(window_evidence.get("primary")),
                },
            )
    for evidence in take.get("window_evidence") or []:
        window = evidence.get("window") or {}
        handle = _window_handle_key(window.get("handle"))
        if (
            evidence.get("closed_during_take")
            and handle not in reviewed_closed_handles
            and _closed_window_requires_review(
                {
                    **evidence,
                    **window,
                },
                window_evidence_by_handle,
                active_event_ids,
            )
        ):
            _add_review(
                review_required,
                step_id,
                "window_closed_during_take",
                "窗口在 Step 中关闭，需要确认关闭是否为预期业务结果",
                {
                    **window,
                    "admission": evidence.get("admission"),
                    "process_relation": evidence.get("process_relation"),
                    "primary": bool(evidence.get("primary")),
                    "opened_during_take": evidence.get("opened_during_take"),
                    "closed_during_take": True,
                    "event_ids": evidence.get("event_ids") or [],
                },
            )

    for pause in take.get("pauses") or []:
        if pause.get("state_changed"):
            _add_review(
                review_required,
                step_id,
                "pause_state_changed",
                "暂停期间目标窗口状态发生变化，需要确认这是前置准备还是业务动作",
                _pause_review_evidence(pause),
            )

    seen_codes = set()
    target_process_ids = {
        window.get("process_id")
        for window in (
            take.get("target_windows")
            or [take.get("target_window") or {}]
        )
        if window.get("process_id") is not None
    }
    for action in actions:
        action_type = action.get("type")
        action_role = action.get("role", "business")
        if action_role == "noise":
            continue
        event_ids = action.get("event_ids") or []
        first_event_id = event_ids[0] if event_ids else None
        target_event_id = action.get("target_event_id") or first_event_id
        event = event_map.get(first_event_id) or {}
        details = event.get("details") or {}
        event_process_id = details.get("process_id")
        if (
            action_role != "transport"
            and
            target_process_ids
            and event_process_id
            and int(event_process_id) not in {
                int(process_id) for process_id in target_process_ids
            }
        ):
            _add_review_once(
                review_required,
                seen_codes,
                step_id,
                "external_process_action",
                "至少一个动作发生在所选目标窗口进程之外",
                {
                    "event_id": first_event_id,
                    "target_process_ids": sorted(target_process_ids),
                    "event_process_id": event_process_id,
                },
            )
        window_class = str(details.get("window_class") or "").casefold()
        if (
            action_role != "transport"
            and window_class in {"shell_traywnd", "shell_secondarytraywnd"}
        ):
            _add_review_once(
                review_required,
                seen_codes,
                step_id,
                "shell_transport_action",
                "录制包含任务栏/系统壳操作，通常属于返回录制工具的传输动作",
                first_event_id,
            )

        target_evidence = target_map.get(target_event_id) or {}
        candidate = target_evidence.get("selected_candidate") or {}
        locator = candidate.get("locator") or {}
        validation = candidate.get("validation") or {}
        target = action.get("target") or {}
        target_element = target.get("element") or {}
        if (
            action_role != "transport"
            and action_type in {
                "click",
                "double_click",
                "keyboard",
                "input_text",
                "drag",
                "middle_click",
                "scroll",
            }
            and target_element.get("enabled") is False
        ):
            _add_review_once(
                review_required,
                seen_codes,
                step_id,
                "target_not_ready",
                "至少一个业务动作发生在尚未可用的目标上，不能证明操作已执行",
                {
                    "action_id": action.get("id"),
                    "event_id": target_event_id,
                    "auto_id": target_element.get("auto_id"),
                    "control_type": target_element.get("control_type"),
                    "enabled": False,
                },
            )
        validated_target = (
            validation.get("status") == "unique"
            and validation.get("target_matches") is True
        )
        if (
            action_role != "transport"
            and (
                target.get("quality") in ("unresolved", None)
                or not validated_target
            )
        ):
            _add_review_once(
                review_required,
                seen_codes,
                step_id,
                "weak_target_quality",
                "至少一个动作缺少唯一且回指录制目标的结构定位证据",
                target_event_id,
            )
        if action_role != "transport" and locator.get("by") in ("ocr", "pos"):
            _add_review_once(
                review_required,
                seen_codes,
                step_id,
                f"fallback_{locator.get('by')}",
                f"至少一个动作依赖 {str(locator.get('by')).upper()} fallback",
                target_event_id,
            )
        if (
            action_role != "transport"
            and action_type == "middle_click"
        ):
            _add_review_once(
                review_required,
                seen_codes,
                step_id,
                "unsupported_middle_click",
                "动作 middle_click 需要人工确认或自定义 Page Object 方法",
                first_event_id,
            )


def _pause_review_evidence(pause):
    summary = pause.get("state_diff_summary") or {}
    compact_summary = {}
    for key in (
        "added_count",
        "removed_count",
        "changed_count",
        "changed_window_count",
    ):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            compact_summary[key] = max(0, value)
    return {
        "pause_id": pause.get("id"),
        "tree_diff": pause.get("tree_diff"),
        "start_note": str(
            pause.get("start_note")
            if pause.get("start_note") is not None
            else pause.get("note") or ""
        )[:160],
        "end_note": str(pause.get("end_note") or "")[:160],
        "state_diff_summary": compact_summary,
    }


def _closed_window_requires_review(
        lifecycle,
        window_evidence_by_handle,
        active_event_ids,
):
    handle = _window_handle_key(lifecycle.get("handle"))
    evidence = window_evidence_by_handle.get(handle)
    if not isinstance(evidence, dict):
        return True
    if evidence.get("primary") is True:
        return True

    admission = str(
        lifecycle.get("admission") or evidence.get("admission") or ""
    ).casefold()
    if admission not in {"automatic", "provisional"}:
        return True

    event_ids = {
        str(item)
        for item in (
            lifecycle.get("event_ids") or evidence.get("event_ids") or []
        )
        if item
    }
    return bool(event_ids & set(active_event_ids or ()))


def _load_window_asset_catalog(session_dir):
    session_dir = Path(session_dir).resolve()
    recording_root = next(
        (
            parent
            for parent in (session_dir, *session_dir.parents)
            if parent.name == "recording_sessions"
        ),
        None,
    )
    if recording_root is None or recording_root.parent.name != "artifacts":
        return {"candidates": []}
    project_root = recording_root.parent.parent
    try:
        index = build_code_reuse_index(
            project_root,
            recording_root / "code-reuse-index.json",
        )
        return build_window_asset_catalog(index)
    except Exception:
        return {"candidates": []}


def _canonical_window_match(catalog, lifecycle):
    title = _identity_text(lifecycle.get("title"))
    class_name = _identity_text(lifecycle.get("class_name"))
    matches = []
    for candidate in (catalog or {}).get("candidates") or ():
        if candidate.get("kind") != "canonical_window":
            continue
        criteria = candidate.get("criteria") or {}
        candidate_title = _identity_text(
            criteria.get("title") or criteria.get("name")
        )
        candidate_class = _identity_text(criteria.get("class_name"))
        if not any((
            title and candidate_title and title == candidate_title,
            class_name
            and candidate_class
            and class_name == candidate_class,
        )):
            continue
        matches.append(candidate)
    return matches[0] if matches else None


def _identity_text(value):
    return " ".join(str(value or "").casefold().split())


def _window_handle_key(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value).strip() or None


def _review_root_variants(locator_drafts, review_required):
    groups = {}
    for name, locator in (locator_drafts.get("roots") or {}).items():
        stable = (
            locator.get("backend"),
            locator.get("control_type"),
            locator.get("auto_id"),
            locator.get("class_name"),
        )
        groups.setdefault(stable, []).append((name, locator))
    for variants in groups.values():
        if len(variants) <= 1:
            continue
        titles = {locator.get("title") for _, locator in variants}
        if len(titles) > 1:
            _add_review(
                review_required,
                None,
                "dynamic_root_variants",
                "同一窗口类型因动态标题被拆成多个 Root 草稿",
                [name for name, _ in variants],
            )


def _validate_window_evidence(take_dir, take, errors):
    target_windows = take.get("target_windows") or [take.get("target_window") or {}]
    evidence = take.get("window_evidence") or []
    if len(target_windows) > 1 and len(evidence) != len(target_windows):
        errors.append(
            "多窗口 Take 的 window_evidence 数量不匹配: "
            f"windows={len(target_windows)}, evidence={len(evidence)}"
        )
    primary_count = sum(bool(item.get("primary")) for item in evidence)
    if evidence and primary_count != 1:
        errors.append(f"window_evidence 必须恰好有一个主窗口: count={primary_count}")
    for item in evidence:
        for key in ("before_tree", "after_tree", "tree_diff"):
            relative_path = item.get(key)
            if relative_path and not (take_dir / relative_path).exists():
                errors.append(f"窗口证据文件不存在: {relative_path}")
        for key in ("before_screenshot", "after_screenshot"):
            relative_path = item.get(key)
            if relative_path and not (take_dir / relative_path).exists():
                errors.append(f"窗口截图不存在: {relative_path}")
    for lifecycle in take.get("window_lifecycle") or []:
        first_seen = lifecycle.get("first_seen_evidence") or {}
        for key in ("before_tree", "before_screenshot"):
            relative_path = first_seen.get(key)
            if relative_path and not (take_dir / relative_path).exists():
                errors.append(f"窗口 first-seen 证据不存在: {relative_path}")


def _add_review(review_required, step_id, code, message, evidence, blocking=True):
    review_required.append({
        "step_id": step_id,
        "code": code,
        "message": message,
        "evidence": evidence,
        "blocking": bool(blocking),
    })


def _add_review_once(review_required, seen_codes, step_id, code, message, evidence):
    if code in seen_codes:
        return
    seen_codes.add(code)
    _add_review(review_required, step_id, code, message, evidence)


def _validate_media_paths(take_dir, media, errors):
    if not media:
        return
    video = (media.get("video") or {}).get("path")
    if video and not (take_dir / video).exists():
        errors.append(f"媒体索引引用不存在的视频: {take_dir / video}")
    contact_sheet = (media.get("contact_sheet") or {}).get("path")
    if contact_sheet and not (take_dir / contact_sheet).exists():
        errors.append(f"媒体索引引用不存在的联系表: {take_dir / contact_sheet}")
    for event in media.get("events") or []:
        screenshot = event.get("screenshot")
        if screenshot and not (take_dir / screenshot).exists():
            errors.append(
                f"媒体索引事件截图不存在: event={event.get('event_id')}, path={screenshot}"
            )


def _validate_action_media_paths(take_dir, action_media, errors):
    if not action_media:
        return
    for action in action_media.get("actions") or []:
        for key in ("before", "after_immediate", "after", "context"):
            frame = action.get(key) or {}
            path = frame.get("path")
            if path and not (take_dir / path).exists():
                errors.append(
                    f"动作媒体不存在: action={action.get('action_id')}, "
                    f"stage={key}, path={path}"
                )
    contact_sheet = (action_media.get("contact_sheet") or {}).get("path")
    if contact_sheet and not (take_dir / contact_sheet).exists():
        errors.append(f"动作联系表不存在: {contact_sheet}")


def _read_json(path, errors):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        errors.append(f"JSON 读取失败: {path}: {type(error).__name__}: {error}")
        return {}


def _read_yaml(path, errors):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as error:
        errors.append(f"YAML 读取失败: {path}: {type(error).__name__}: {error}")
        return {}


def _read_jsonl(path, errors):
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as error:
        errors.append(f"JSONL 读取失败: {path}: {type(error).__name__}: {error}")
        return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a recorder AI evidence bundle")
    parser.add_argument("session_dir", help="Path to one recording session directory")
    args = parser.parse_args(argv)
    report = validate_ai_bundle(args.session_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["bundle_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())