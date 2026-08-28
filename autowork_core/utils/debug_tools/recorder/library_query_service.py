from __future__ import annotations

from pathlib import Path

from autowork_core.utils.debug_tools.recorder.capability import (
    load_capability,
    load_capability_catalog,
    resolve_capability_path,
)
from autowork_core.utils.debug_tools.recorder.catalog import (
    load_recording_catalog,
)
from autowork_core.utils.debug_tools.recorder.dto import (
    LibraryCapabilityDTO,
    LibraryOverviewDTO,
    LibraryRunDTO,
    RetirementStatusDTO,
)
from autowork_core.utils.debug_tools.recorder.run_retirement import (
    inspect_run_retirement,
)


class RecorderLibraryQueryService:
    """Builds offline Library read models without exposing artifact layouts."""

    def __init__(self, output_root):
        self.output_root = Path(output_root).resolve()

    def get_library(self):
        catalog = load_recording_catalog(self.output_root)
        capability_catalog = load_capability_catalog(self.output_root)
        runs = tuple(
            self._run_dto(entry)
            for entry in catalog.get("sessions") or ()
            if isinstance(entry, dict) and entry.get("session_id")
        )
        capabilities = tuple(
            self._capability_dto(entry)
            for entry in capability_catalog.get("capabilities") or ()
            if isinstance(entry, dict) and entry.get("capability_id")
        )
        return LibraryOverviewDTO(
            output_root=str(self.output_root),
            runs=runs,
            capabilities=capabilities,
        )

    def retirement_status(self, run):
        if not isinstance(run, LibraryRunDTO) or run.directory_path is None:
            return RetirementStatusDTO(
                eligible=False,
                detail="退役：Run 路径无效，不能执行退役检查",
            )
        try:
            inspection = inspect_run_retirement(run.directory_path)
        except Exception as error:
            return RetirementStatusDTO(
                eligible=False,
                detail=(
                    "退役：检查失败；"
                    f"{type(error).__name__}: {error}"
                ),
            )
        blockers = inspection.get("blockers") or ()
        if blockers:
            return RetirementStatusDTO(
                eligible=False,
                detail="退役：已阻塞；" + "；".join(
                    str(item) for item in blockers
                ),
            )
        knowledge = inspection.get("knowledge") or {}
        if knowledge.get("durable"):
            return RetirementStatusDTO(
                eligible=True,
                detail=(
                    "退役：可安全退役；将保留 "
                    f"{len(knowledge.get('memory_ids') or ())} 条经验、"
                    f"{len(knowledge.get('capability_ids') or ())} 个确认能力"
                ),
            )
        return RetirementStatusDTO(
            eligible=True,
            detail="退役：Run 可删除，但没有持久经验；需要明确确认直接丢弃",
        )

    def _run_dto(self, entry):
        feature = entry.get("feature") or {}
        scenario = entry.get("scenario") or {}
        steps = entry.get("steps") or ()
        directory = _safe_child(self.output_root, entry.get("path"))
        searchable = [
            entry.get("session_id"),
            entry.get("path"),
            feature.get("id"),
            feature.get("key"),
            feature.get("name"),
            scenario.get("id"),
            scenario.get("key"),
            scenario.get("name"),
            scenario.get("example_id"),
            *(item.get("text") for item in steps if isinstance(item, dict)),
        ]
        completed = sum(
            item.get("status") == "completed"
            for item in steps
            if isinstance(item, dict)
        )
        return LibraryRunDTO(
            session_id=str(entry.get("session_id")),
            feature_name=str(
                feature.get("name") or feature.get("key") or ""
            ),
            scenario_name=_scenario_label(scenario),
            progress=f"{completed}/{len(steps)}",
            next_action=(
                _catalog_next_action(entry)
                if directory is not None
                else "Run 路径无效"
            ),
            updated_at=str(entry.get("updated_at") or ""),
            path=str(entry.get("path") or ""),
            directory_path=str(directory) if directory is not None else None,
            search_text=" ".join(
                str(item or "") for item in searchable
            ).casefold(),
        )

    def _capability_dto(self, entry):
        capability_id = str(entry.get("capability_id") or "")
        detail_path = None
        try:
            value = load_capability(self.output_root, capability_id)
            detail_path = str(resolve_capability_path(self.output_root, entry))
        except (OSError, ValueError, KeyError):
            value = dict(entry)
            value["status"] = "invalid"
        feature = value.get("feature") or {}
        scenario = value.get("scenario") or {}
        step = value.get("step") or {}
        status = str(value.get("status") or "unknown")
        searchable = (
            capability_id,
            entry.get("path"),
            status,
            feature.get("name"),
            scenario.get("name"),
            step.get("text"),
            step.get("key"),
        )
        return LibraryCapabilityDTO(
            capability_id=capability_id,
            status=status,
            status_label={
                "confirmed": "已确认",
                "stale": "已失效",
                "candidate": "待确认",
                "invalid": "文件无效",
            }.get(status, status),
            feature_name=str(feature.get("name") or ""),
            scenario_name=_scenario_label(scenario),
            step_text=str(step.get("text") or step.get("key") or ""),
            published_at=str(value.get("published_at") or ""),
            path=str(entry.get("path") or ""),
            detail_path=detail_path,
            search_text=" ".join(
                str(item or "") for item in searchable
            ).casefold(),
        )


def _safe_child(root, value):
    raw = str(value or "")
    path = Path(raw)
    if not raw or path.is_absolute():
        return None
    resolved = (Path(root) / path).resolve()
    try:
        resolved.relative_to(Path(root).resolve())
    except ValueError:
        return None
    return resolved if resolved.is_dir() else None


def _scenario_label(scenario):
    name = str(scenario.get("name") or scenario.get("key") or "")
    example_id = scenario.get("example_id")
    return f"{name} [Examples {example_id}]" if example_id else name


def _catalog_next_action(entry):
    readiness = entry.get("readiness") or {}
    if readiness.get("capture_generation_candidate"):
        return "待 AI 理解"
    if not readiness.get("bundle_valid"):
        return "证据损坏"
    if readiness.get("recording_complete") is False:
        return "继续录制"
    if readiness.get("semantic_ready") is False:
        return "需要审阅"
    return "打开检查"