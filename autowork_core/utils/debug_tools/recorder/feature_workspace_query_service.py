from __future__ import annotations

import json
from pathlib import Path

from autowork_core.runtime.step_scope import collect_feature_files
from autowork_core.utils.debug_tools.recorder.catalog import (
    load_recording_catalog,
)
from autowork_core.utils.debug_tools.recorder.dto import (
    FeatureDirectoryDTO,
    FeatureScenarioDTO,
    FeatureWorkspaceDTO,
)
from autowork_core.utils.debug_tools.recorder.feature_plan import (
    load_feature_plan,
)
from autowork_core.utils.debug_tools.recorder.feature_delivery import (
    exportable_feature_recording_run,
    load_feature_delivery_index,
)
from autowork_core.utils.debug_tools.recorder.models import public_dict
from autowork_core.utils.debug_tools.recorder.scope_binding import (
    recording_business_fingerprint,
)


class FeatureWorkspaceQueryService:
    """Projects recording, export, and issue facts for a Feature directory."""

    def __init__(self, feature_root, recording_root):
        self.feature_root = Path(feature_root).resolve()
        self.recording_root = Path(recording_root).resolve()

    def get_workspace(self):
        catalog = load_recording_catalog(self.recording_root)
        warnings = []
        try:
            delivery_index = load_feature_delivery_index(
                self.recording_root
            )
        except Exception as error:
            delivery_index = {"features": {}}
            warnings.append(
                f"Feature录制资料记录不可用: {type(error).__name__}: {error}"
            )
        entries = tuple(
            item
            for item in catalog.get("sessions") or ()
            if isinstance(item, dict)
        )
        features = []
        seen_ids = {}
        for source_path in collect_feature_files(self.feature_root):
            try:
                source_path = _safe_workspace_feature_path(
                    source_path,
                    self.feature_root,
                )
                plan = load_feature_plan(source_path)
                if plan.id in seen_ids:
                    raise ValueError(
                        "Recorder Feature ID 与其他文件重复: "
                        f"{seen_ids[plan.id]}"
                    )
                seen_ids[plan.id] = str(source_path)
                features.append(self._feature_dto(
                    plan,
                    entries,
                    (delivery_index.get("features") or {}).get(plan.id),
                ))
            except Exception as error:
                features.append(FeatureWorkspaceDTO(
                    feature_id="",
                    name=source_path.stem,
                    source_path=str(source_path),
                    source_relpath=_display_path(
                        source_path,
                        self.feature_root,
                    ),
                    source_hash="",
                    recording_label="—",
                    recorded_scenario_count=0,
                    scenario_count=0,
                    exportable_recording_count=0,
                    outdated_recording_count=0,
                    last_recording_at=None,
                    last_export_at=None,
                    export_label="—",
                    export_outdated=False,
                    scenarios=(),
                    issues=(f"{type(error).__name__}: {error}",),
                ))
        features.sort(key=lambda item: (
            item.source_relpath.casefold(),
            item.name.casefold(),
        ))
        return FeatureDirectoryDTO(
            feature_root=str(self.feature_root),
            recording_root=str(self.recording_root),
            features=tuple(features),
            warnings=tuple(warnings),
        )

    def _feature_dto(self, plan, entries, delivery):
        matching = [
            entry
            for entry in entries
            if str((entry.get("feature") or {}).get("id") or "")
            == plan.id
        ]
        scenarios = []
        for scenario in plan.scenarios:
            candidates = [
                (
                    entry,
                    self._entry_business_fingerprint(entry),
                )
                for entry in matching
                if str((entry.get("scenario") or {}).get("id") or "")
                == scenario.id
            ]
            candidates.sort(
                key=lambda item: str(item[0].get("updated_at") or ""),
                reverse=True,
            )
            expected_step_ids = {step.id for step in scenario.steps}
            expected_fingerprint = recording_business_fingerprint(
                public_dict(plan),
                public_dict(scenario),
            )
            export_entry = next((
                entry
                for entry, _fingerprint in candidates
                if exportable_feature_recording_run(
                    entry,
                    self.recording_root,
                    expected_step_ids=expected_step_ids,
                    expected_business_fingerprint=expected_fingerprint,
                ) is not None
            ), None)
            scenarios.append(_scenario_dto(
                scenario,
                candidates,
                expected_step_ids=expected_step_ids,
                expected_business_fingerprint=expected_fingerprint,
                export_entry=export_entry,
            ))
        current_ids = {scenario.id for scenario in plan.scenarios}
        obsolete = [
            entry
            for entry in matching
            if str((entry.get("scenario") or {}).get("id") or "")
            not in current_ids
        ]
        last_recording_at = max(
            (str(entry.get("updated_at") or "") for entry in matching),
            default="",
        ) or None
        recorded_count = sum(
            scenario.recording_state in {"partial", "recorded"}
            for scenario in scenarios
        )
        outdated_count = sum(
            scenario.recording_state == "outdated"
            for scenario in scenarios
        ) + len(obsolete)
        exportable_count = sum(
            scenario.exportable for scenario in scenarios
        )
        issues = tuple(
            dict.fromkeys(
                scenario.issue
                for scenario in scenarios
                if scenario.issue
            )
        )
        export_at, export_label, export_outdated = _export_status(
            plan,
            scenarios,
            delivery,
        )
        return FeatureWorkspaceDTO(
            feature_id=plan.id,
            name=plan.name,
            source_path=str(plan.source_path),
            source_relpath=_display_path(
                plan.source_path,
                self.feature_root,
            ),
            source_hash=plan.source_hash,
            recording_label=_recording_summary(
                recorded_count,
                len(scenarios),
                outdated_count,
            ),
            recorded_scenario_count=recorded_count,
            scenario_count=len(scenarios),
            exportable_recording_count=exportable_count,
            outdated_recording_count=outdated_count,
            last_recording_at=last_recording_at,
            last_export_at=export_at,
            export_label=export_label,
            export_outdated=export_outdated,
            scenarios=tuple(scenarios),
            issues=issues,
        )

    def _entry_business_fingerprint(self, entry):
        declared = str(entry.get("business_fingerprint") or "")
        if declared:
            return declared
        raw_path = str(entry.get("path") or "")
        candidate = (self.recording_root / raw_path).resolve()
        try:
            candidate.relative_to(self.recording_root)
        except ValueError:
            return None
        manifest_path = candidate / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return recording_business_fingerprint(
                manifest.get("feature") or {},
                manifest.get("scenario") or {},
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None


def _scenario_dto(
        scenario,
        candidates,
        *,
        expected_step_ids,
        expected_business_fingerprint,
        export_entry=None,
    ):
    latest, business_fingerprint = (
        candidates[0] if candidates else (None, None)
    )
    if latest is None:
        state = "none"
        label = "无录制"
        completed_count = 0
        issue = None
    else:
        readiness = latest.get("readiness") or {}
        steps = latest.get("steps") or ()
        recorded_step_ids = {
            str(item.get("id") or "")
            for item in steps
            if isinstance(item, dict) and item.get("id")
        }
        completed_step_ids = {
            str(item.get("id") or "")
            for item in steps
            if isinstance(item, dict)
            and item.get("id")
            and item.get("status") == "completed"
        }
        completed_count = len(completed_step_ids & expected_step_ids)
        if business_fingerprint != expected_business_fingerprint:
            state = "outdated"
            label = "有旧录制"
            issue = None
        elif not recorded_step_ids <= expected_step_ids:
            state = "invalid"
            label = "录制需检查"
            issue = "录制Step与当前Feature不一致"
        elif readiness.get("bundle_valid") is not True:
            state = "invalid"
            label = "录制需检查"
            issue = "录制文件损坏或不完整"
        elif readiness.get("semantic_ready") is not True:
            state = "invalid"
            label = "录制需检查"
            issue = "录制证据需要检查"
        elif (
            recorded_step_ids == expected_step_ids
            and
            readiness.get("recording_complete") is True
            and all(
                (item or {}).get("status") == "completed"
                for item in steps
            )
        ):
            state = "recorded"
            label = "已录制"
            issue = None
        else:
            state = "partial"
            label = f"已录制 {completed_count}/{len(expected_step_ids)} Step"
            issue = None
    return FeatureScenarioDTO(
        scenario_id=scenario.id,
        name=scenario.display_name,
        example_id=scenario.example_id,
        recording_state=state,
        recording_label=label,
        recorded_step_count=(completed_count if latest else 0),
        total_step_count=len(expected_step_ids),
        exportable=export_entry is not None,
        session_id=(str(latest.get("session_id")) if latest else None),
        updated_at=(str(latest.get("updated_at")) if latest else None),
        export_session_id=(
            str(export_entry.get("session_id")) if export_entry else None
        ),
        export_updated_at=(
            str(export_entry.get("updated_at")) if export_entry else None
        ),
        issue=issue if latest else None,
    )


def _display_path(path, root):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def _safe_workspace_feature_path(path, root):
    root = Path(root).resolve()
    candidate = Path(path).absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Feature文件越出所选目录") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Feature目录不能包含符号链接")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("Feature文件解析后越出所选目录") from error
    return resolved


def _recording_summary(recorded, total, outdated):
    if recorded:
        label = f"录制覆盖 {recorded}/{total}"
    else:
        label = "无录制"
    if outdated:
        label += f" · 旧录制 {outdated}"
    return label


def _export_status(plan, scenarios, delivery):
    export = _last_export(delivery)
    if not isinstance(export, dict):
        return None, "—", False
    delivered_runs = {
        (
            str(item.get("session_id") or ""),
            str(item.get("updated_at") or ""),
        )
        for item in export.get("runs") or ()
    }
    current_runs = {
        (
            str(item.export_session_id or ""),
            str(item.export_updated_at or ""),
        )
        for item in scenarios
        if item.exportable and item.export_session_id
    }
    current = bool(current_runs) and all((
        export.get("source_hash") == plan.source_hash,
        delivered_runs == current_runs,
    ))
    exported_at = export.get("finished_at")
    label = _short_time(exported_at)
    if not current:
        label += " · 后有变化"
    return exported_at, label, not current


def _last_export(delivery):
    if not isinstance(delivery, dict):
        return None
    value = delivery.get("last_export")
    if isinstance(value, dict):
        return value
    return delivery if delivery.get("kind") == "export" else None


def _short_time(value):
    text = str(value or "")
    return text.replace("T", " ")[:16] or "已导出"


__all__ = ["FeatureWorkspaceQueryService"]