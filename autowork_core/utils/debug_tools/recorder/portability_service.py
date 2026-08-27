from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.dto import (
    PortabilityActivityDTO,
    PortabilityOverviewDTO,
)
from autowork_core.utils.debug_tools.recorder.feature_delivery import (
    export_feature_deliveries,
    export_feature_delivery,
    import_feature_delivery,
    record_feature_deliveries,
    record_feature_delivery,
)
from autowork_core.utils.debug_tools.recorder.recording_portability import (
    export_recording_package,
    import_recording_package,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


ACTIVITY_VERSION = "1.0"
MAX_ACTIVITIES = 20
_LOCKS_GUARD = threading.Lock()
_LOCKS = {}


class RecordingPortabilityService:
    def __init__(self, recording_root, *, recover_interrupted=True):
        self.recording_root = Path(recording_root).resolve()
        self.activity_path = (
            self.recording_root / ".portability" / "activities.json"
        )
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(
                str(self.activity_path).casefold(),
                threading.RLock(),
            )
        if recover_interrupted:
            self._recover_interrupted()

    def export(self, source, package_path):
        return self._run(
            "export",
            package_path,
            export_recording_package,
            source,
            package_path,
        )

    def import_package(self, package_path):
        return self._run(
            "import",
            package_path,
            import_recording_package,
            package_path,
            self.recording_root,
        )

    def export_feature(self, feature_path, package_path):
        return self._run(
            "feature_export",
            package_path,
            self._export_feature,
            feature_path,
            package_path,
        )

    def export_feature_scenarios(
            self,
            feature_path,
            scenario_ids,
            package_path,
    ):
        return self._run(
            "feature_export",
            package_path,
            self._export_feature_scenarios,
            feature_path,
            scenario_ids,
            package_path,
        )

    def export_features(self, feature_paths, output_dir):
        return self._run(
            "feature_export",
            output_dir,
            self._export_features,
            feature_paths,
            output_dir,
        )

    def import_feature(self, package_path, project_root):
        return self._run(
            "feature_import",
            package_path,
            self._import_feature,
            package_path,
            project_root,
        )

    def _export_feature(self, feature_path, package_path):
        result = export_feature_delivery(
            feature_path,
            self.recording_root,
            package_path,
        )
        try:
            record_feature_delivery(self.recording_root, "export", result)
        except Exception as error:
            return _with_warning(result, _delivery_index_warning(error))
        return result

    def _export_feature_scenarios(
            self,
            feature_path,
            scenario_ids,
            package_path,
    ):
        result = export_feature_delivery(
            feature_path,
            self.recording_root,
            package_path,
            scenario_ids=scenario_ids,
        )
        return result

    def _export_features(self, feature_paths, output_dir):
        result = export_feature_deliveries(
            feature_paths,
            self.recording_root,
            output_dir,
        )
        try:
            record_feature_deliveries(
                self.recording_root,
                "export",
                result["packages"],
            )
        except Exception as error:
            return _with_warning(result, _delivery_index_warning(error))
        return result

    def _import_feature(self, package_path, project_root):
        result = import_feature_delivery(
            package_path,
            project_root,
            self.recording_root,
        )
        try:
            record_feature_delivery(self.recording_root, "import", result)
        except Exception as error:
            return _with_warning(result, _delivery_index_warning(error))
        return result

    def overview(self, *, active=False, active_kind=None):
        activities, warnings = self._load()
        return PortabilityOverviewDTO(
            active=bool(active),
            active_kind=str(active_kind) if active_kind else None,
            last_export=_latest(activities, "export"),
            last_import=_latest(activities, "import"),
            warnings=tuple(warnings),
        )

    def _run(self, kind, package_path, function, *args):
        activity = {
            "activity_version": ACTIVITY_VERSION,
            "operation_id": "portability-" + uuid.uuid4().hex,
            "kind": kind,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "package_name": Path(package_path).name,
            "package_path": str(Path(package_path).resolve()),
            "run_count": 0,
            "ready_count": 0,
            "package_sha256": None,
            "error": None,
        }
        self._append(activity)
        try:
            result = function(*args)
        except Exception as error:
            self._finish(
                activity["operation_id"],
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
            raise
        self._finish(
            activity["operation_id"],
            status="completed",
            run_count=int(result.get("run_count") or 0),
            ready_count=sum(
                bool(item.get("request_path"))
                for item in ((
                    result.get("imported_runs")
                    if kind == "feature_import"
                    else result.get("runs")
                ) or ())
            ) if kind in {"import", "feature_import"} else 0,
            package_sha256=result.get("package_sha256"),
        )
        return result

    def _append(self, activity):
        with self._lock:
            activities, _warnings = self._load_unlocked()
            activities.append(dict(activity))
            self._write_unlocked(activities[-MAX_ACTIVITIES:])

    def _finish(self, operation_id, **updates):
        with self._lock:
            activities, _warnings = self._load_unlocked()
            activity = next(
                (
                    item
                    for item in reversed(activities)
                    if item.get("operation_id") == operation_id
                ),
                None,
            )
            if activity is None:
                return
            activity.update(updates)
            activity["finished_at"] = _now()
            self._write_unlocked(activities[-MAX_ACTIVITIES:])

    def _recover_interrupted(self):
        with self._lock:
            activities, _warnings = self._load_unlocked()
            changed = False
            for activity in activities:
                if activity.get("status") != "running":
                    continue
                activity.update({
                    "status": "interrupted",
                    "finished_at": _now(),
                    "error": "上次进程在任务完成前退出",
                })
                changed = True
            if changed:
                self._write_unlocked(activities[-MAX_ACTIVITIES:])

    def _load(self):
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self):
        if not self.activity_path.is_file():
            return [], []
        try:
            value = json.loads(self.activity_path.read_text(encoding="utf-8"))
            if value.get("activity_store_version") != ACTIVITY_VERSION:
                raise ValueError("activity store version 不匹配")
            activities = [
                item
                for item in value.get("activities") or ()
                if isinstance(item, dict)
                and item.get("activity_version") == ACTIVITY_VERSION
                and item.get("operation_id")
                and item.get("kind") in {
                    "export",
                    "import",
                    "feature_export",
                    "feature_import",
                }
            ]
            return activities[-MAX_ACTIVITIES:], []
        except Exception as error:
            return [], [
                f"迁移活动记录无法读取: {type(error).__name__}: {error}"
            ]

    def _write_unlocked(self, activities):
        write_json_atomic(self.activity_path, {
            "activity_store_version": ACTIVITY_VERSION,
            "updated_at": _now(),
            "activities": activities[-MAX_ACTIVITIES:],
        }, compact=True)


def _latest(activities, kind):
    kinds = {kind, f"feature_{kind}"}
    value = next(
        (
            item
            for item in reversed(activities)
            if item.get("kind") in kinds
        ),
        None,
    )
    if value is None:
        return None
    return PortabilityActivityDTO(
        operation_id=str(value.get("operation_id") or ""),
        kind=str(value.get("kind") or kind),
        status=str(value.get("status") or "unknown"),
        started_at=str(value.get("started_at") or ""),
        finished_at=value.get("finished_at"),
        package_name=str(value.get("package_name") or ""),
        package_path=str(value.get("package_path") or ""),
        run_count=int(value.get("run_count") or 0),
        ready_count=int(value.get("ready_count") or 0),
        package_sha256=value.get("package_sha256"),
        error=value.get("error"),
    )


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _delivery_index_warning(error):
    return (
        "Feature已交付，但最近交付记录未更新: "
        f"{type(error).__name__}: {error}"
    )


def _with_warning(result, warning):
    value = dict(result)
    value["warnings"] = tuple([
        *(value.get("warnings") or ()),
        str(warning),
    ])
    return value


__all__ = ["ACTIVITY_VERSION", "RecordingPortabilityService"]