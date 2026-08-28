from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from autowork_core.runtime.reporting.run_result_bridge import (
    generation_provenance_from_artifacts,
    latest_matching_run_result,
    load_generation_provenance,
    verified_file_path,
)
from autowork_core.runtime.reporting.oracle_registry import (
    latest_runtime_matrix_receipt,
)
from autowork_core.utils.debug_tools.recorder.generation_quality_gate import (
    evaluate_generation_quality,
)
from autowork_core.utils.debug_tools.recorder.generation_job import (
    load_generation_job,
)
from autowork_core.utils.debug_tools.recorder.generation_job_result import (
    load_generation_job_result,
)
from autowork_core.utils.debug_tools.recorder.transaction_integrity import (
    transaction_result_fingerprint,
)
from autowork_core.utils.debug_tools.recorder.implementation_manifest import (
    IMPLEMENTATION_MANIFEST_VERSION,
    implementation_manifest_identity_is_valid,
)
from autowork_core.utils.debug_tools.recorder.implementation_validation_ledger import (
    verify_validation_ledger,
)
from config.paths import Paths
from autowork_core.utils.debug_tools.recorder.bundle_validator import (
    validate_ai_bundle,
)
from autowork_core.utils.debug_tools.recorder.diagnostics import (
    build_step_diagnostics,
    diagnostic_event_ids,
    format_user_step_diagnostic,
    user_diagnostic_title,
    user_evidence_location,
)
from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
    SYSTEM_INFERRED_INTENT,
)
from autowork_core.utils.debug_tools.recorder.dto import (
    DecisionOptionDTO,
    DecisionQuestionDTO,
    DecisionSummaryDTO,
    EvidenceSummaryDTO,
    FeedbackHistoryDTO,
    GenerationJobHistoryDTO,
    GenerationResultDTO,
    GenerationSummaryDTO,
    GenerationStageSummaryDTO,
    ImplementationSummaryDTO,
    IssueDTO,
    ObservationDTO,
    QuestionMediaDTO,
    RuntimeDiagnosticDTO,
    RuntimeResultDTO,
    ScenarioScopeDTO,
    StepWorkspaceDTO,
    StageOutcomeDTO,
    TakeDTO,
    UserTaskProjection,
    VerificationSummaryDTO,
    WorkspaceMaterializationDTO,
    WindowOwnershipSummaryDTO,
    WorkbenchDTO,
    XPathTechnicalDetailDTO,
)
from autowork_core.utils.debug_tools.recorder.decision_pack import (
    load_decision_pack,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    load_evidence_context,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    resolve_session_path,
)
from autowork_core.utils.debug_tools.recorder.project_memory import (
    find_recording_root,
    latest_transaction,
    load_memory_events,
    load_transaction_report,
)
from autowork_core.utils.debug_tools.recorder.reconciliation_repository import (
    review_source_id,
)
from autowork_core.utils.debug_tools.recorder.request_service import (
    GenerationRequestService,
)
from autowork_core.utils.debug_tools.recorder.take_query_service import (
    TakeQueryService,
)
from autowork_core.utils.debug_tools.recorder.timeline import TimelineStore
from autowork_core.utils.debug_tools.recorder.supplement_repository import (
    SupplementRepository,
)


TIMELINE_CORRECTABLE_CODES = {
    "external_process_action",
    "orphan_mouse_boundary",
    "shell_transport_action",
}


class RecorderQueryService:
    """Builds stable read models without exposing artifact layout to views."""

    def __init__(self, session, operation_coordinator=None):
        self.session = session
        self.session_dir = Path(session.session_dir).resolve()
        self.requests = GenerationRequestService(self.session_dir)
        self.operations = operation_coordinator

    def get_workbench(self, selected_step_id=None, *, readiness=None):
        if (
                readiness is None
            or readiness is not getattr(
                self.session,
                "latest_readiness",
                None,
            )
        ):
            readiness = validate_ai_bundle(self.session_dir)
        scope = self._scenario_scope()
        generation = self._scenario_generation_summary(
            scope=scope,
            include_result=True,
        )
        generation_context = self._current_generation_context(scope)
        generation_provenance = (
            generation_context[0] if generation_context is not None else None
        )
        generation_request = (
            generation_context[1] if generation_context is not None else None
        )
        runtime_result = self._runtime_result(
            generation_provenance,
            request=generation_request,
            project_root=(
                generation_context[3].get("project_root")
                if generation_context is not None
                else None
            ),
        )
        quality = self._generation_quality(
            generation_context,
            runtime_result,
        )
        verification = _verification_summary(
            generation.result,
            runtime_result,
            transaction_valid=generation_provenance is not None,
            quality=quality,
        )
        generation = replace(
            generation,
            result=(
                generation.result
                if generation.job_id
                and generation.workflow_status in {"completed", "failed"}
                else _with_runtime_stages(
                    generation.result,
                    runtime_result,
                    quality=quality,
                )
            ),
            verification=verification,
            recommended_detail=(
                verification.detail
                if generation.workflow_status in {"completed", "failed"}
                else generation.recommended_detail
            ),
        )
        workflow = self.requests.workflow_state(scope.selected_step_ids)
        workflow = workflow if isinstance(workflow, dict) else {}
        effective_readiness = _effective_readiness(
            readiness,
            (workflow or {}).get("ambiguity"),
        )
        steps = tuple(
            self._step_workspace(
                step,
                effective_readiness,
                generation.display_status,
            )
            for step in self.session.selected_steps
        )
        if selected_step_id not in {step.step_id for step in steps}:
            selected_step_id = steps[0].step_id if steps else None
        user_task = _user_task_projection(
            scope=scope,
            generation=generation,
            steps=steps,
            selected_step_id=selected_step_id,
            readiness=effective_readiness,
        )
        return WorkbenchDTO(
            run_id=str(self.session.run_id),
            feature_name=str(self.session.feature_plan.name),
            scenario_name=str(self.session.scenario_plan.display_name),
            run_status=self._run_status(),
            selected_step_id=selected_step_id,
            steps=steps,
            user_task=user_task,
            scope=scope,
            generation=generation,
            runtime_result=runtime_result,
            active_operation_count=(
                len(self.operations.list_active())
                if self.operations is not None
                else 0
            ),
        )

    def _current_generation_context(self, scope):
        request = self.requests.latest(scope.selected_step_ids)
        if request is None:
            return None
        workflow = self.requests.workflow_state(scope.selected_step_ids)
        workflow = workflow if isinstance(workflow, dict) else {}
        job_result = (
            load_generation_job_result(
                self.session_dir,
                workflow.get("last_job_result") or {},
            )
            if workflow.get("last_job_result")
            else None
        )
        terminal_job_result = bool(
            workflow.get("last_job_result")
            and (
                not workflow.get("current_job")
                or workflow.get("status") in {"completed", "failed"}
            )
        )
        if terminal_job_result:
            if job_result is None:
                return None
            bound = _job_result_transaction(
                self.session_dir,
                job_result,
            )
            if bound is None:
                return None
            report_path, _report = bound
        elif workflow.get("current_job"):
            bound = _job_execution_transaction(
                self.session_dir,
                workflow,
            )
            if bound is None:
                return None
            report_path, _report = bound
        else:
            return None
        if _report.get("status") not in {
            "completed",
            "completed_no_changes",
        }:
            return None
        try:
            from autowork_core.utils.debug_tools.recorder.capability import (
                validate_completed_transaction_artifact_source,
                validate_accepted_transaction_capability_source,
            )
            validator = (
                validate_accepted_transaction_capability_source
                if terminal_job_result
                else validate_completed_transaction_artifact_source
            )
            _session, loaded_request, plan, _runtime = validator(
                    report_path,
                    _report,
                    project_root=_report.get("project_root") or Paths.BASE_DIR,
            )
            provenance = generation_provenance_from_artifacts(
                loaded_request,
                plan,
                _report,
            )
            return provenance, loaded_request, plan, _report
        except (OSError, ValueError):
            return None

    def _generation_quality(self, context, runtime_result):
        if context is None:
            return None
        _provenance, request, plan, report = context
        run_result = None
        if runtime_result is not None:
            try:
                run_result = json.loads(
                    Path(runtime_result.result_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                run_result = None
        matrix = latest_runtime_matrix_receipt(
            report.get("project_root"),
            report.get("transaction_id"),
        )
        return evaluate_generation_quality(
            request,
            plan,
            report,
            run_result,
            runtime_matrix=(matrix[1] if matrix is not None else None),
        )

    def _runtime_result(
            self,
            generation_provenance,
            *,
            request=None,
            project_root=None,
        ):
        if generation_provenance is None:
            return None
        target = (request or {}).get("target") or {}
        feature = target.get("feature") or {}
        scenario_target = target.get("scenario") or {}
        query_kwargs = {
            "example_id": (
                scenario_target.get("example_id")
                if scenario_target
                else self.session.scenario_plan.example_id
            ),
            "generation_provenance": generation_provenance,
        }
        if project_root is not None:
            query_kwargs["project_root"] = project_root
        match = latest_matching_run_result(
            feature.get("source_relpath")
            or self.session.feature_plan.source_relpath,
            scenario_target.get("name")
            or self.session.scenario_plan.name,
            **query_kwargs,
        )
        if match is None:
            return None
        result_path, value, scenario = match
        failed_steps = [
            step
            for step in scenario.get("steps") or ()
            if step.get("status") == "failed"
        ]
        attachments = [
            *(
                attachment
                for step in failed_steps
                for attachment in step.get("attachments") or ()
            ),
            *(scenario.get("attachments") or ()),
            *(
                attachment
                for step in scenario.get("steps") or ()
                if step not in failed_steps
                for attachment in step.get("attachments") or ()
            ),
        ]
        report = value.get("report") or {}
        first_error = next(
            (
                step.get("error_detail") or step.get("error")
                for step in failed_steps
                if step.get("error_detail") or step.get("error")
            ),
            None,
        )
        first_diagnostic = next((
            _runtime_diagnostic_dto(step.get("diagnostic"))
            for step in failed_steps
            if isinstance(step.get("diagnostic"), dict)
        ), None)
        first_attachment = next((
            reference
            for attachment in attachments
            if (reference := _verified_runtime_file(attachment)) is not None
        ), None)
        report_reference = _verified_runtime_file(report)
        return RuntimeResultDTO(
            status=str(scenario.get("status") or "unknown"),
            run_result_id=str(value.get("run_result_id") or ""),
            result_path=str(result_path),
            report_path=(
                report_reference[0] if report_reference else None
            ),
            failed_step_count=len(failed_steps),
            attachment_count=len(attachments),
            first_error=str(first_error) if first_error else None,
            first_attachment_path=(
                first_attachment[0] if first_attachment else None
            ),
            first_diagnostic=first_diagnostic,
            report_sha256=(
                report_reference[1] if report_reference else None
            ),
            report_size=(
                report_reference[2] if report_reference else None
            ),
            first_attachment_sha256=(
                first_attachment[1] if first_attachment else None
            ),
            first_attachment_size=(
                first_attachment[2] if first_attachment else None
            ),
        )

    def get_step_workspace(self, step_id):
        readiness = validate_ai_bundle(self.session_dir)
        step = next(
            (item for item in self.session.selected_steps if item.id == step_id),
            None,
        )
        if step is None:
            raise KeyError(f"录制任务中不存在 Step: {step_id}")
        scope = self._scenario_scope()
        generation = self._scenario_generation_summary(scope=scope)
        workflow = self.requests.workflow_state(scope.selected_step_ids)
        readiness = _effective_readiness(
            readiness,
            (workflow or {}).get("ambiguity"),
        )
        return self._step_workspace(
            step,
            readiness,
            generation.display_status,
        )

    def _step_workspace(self, step, readiness, scenario_generation_status):
        state = self.session.step_states[step.id]
        selected_take_id = state.get("selected_take")
        takes = tuple(
            self._take_dto(
                take,
                fallback_number=number,
                selected_take_id=selected_take_id,
            )
            for number, take in enumerate(
                state.get("takes") or (),
                start=1,
            )
        )
        diagnostics = build_step_diagnostics(
            readiness,
            self.session,
            step.id,
        )
        selected_take = next(
            (take for take in takes if take.selected),
            None,
        )
        issues = tuple(
            IssueDTO(
                code=str(item.get("code") or "unknown"),
                message="",
                blocking=bool(item.get("blocking")),
                title=user_diagnostic_title(item),
                detail=format_user_step_diagnostic(item),
                repair=item.get("repair"),
                location=user_evidence_location(item),
                evidence=item.get("evidence"),
                event_ids=tuple(diagnostic_event_ids([item])),
                evidence_path=_diagnostic_evidence_path(
                    selected_take,
                    item,
                ),
            )
            for item in diagnostics
        )
        generation_status = self._generation_status(
            state,
            scenario_generation_status,
        )
        evidence_status = _evidence_status(state, issues)
        next_action, next_label = _next_action(
            str(state.get("status") or "pending"),
            evidence_status,
            generation_status,
        )
        next_detail = ""
        if generation_status == "updating":
            next_action, next_label, next_detail = recommend_step_action(
                readiness,
                step.id,
                self.session.step_states,
            )
        return StepWorkspaceDTO(
            step_id=step.id,
            ordinal=int(step.ordinal),
            keyword=str(step.keyword),
            text=str(step.text),
            capture_status=str(state.get("status") or "pending"),
            evidence_status=evidence_status,
            generation_status=generation_status,
            selected_take_id=selected_take_id,
            timeline_revision=(
                selected_take.timeline_revision if selected_take else None
            ),
            next_action=next_action,
            next_action_label=next_label,
            next_action_detail=next_detail,
            takes=takes,
            issues=issues,
            observations=self._observation_dtos(
                step,
                takes,
            ),
        )

    def _observation_dtos(self, step, takes):
        example_values = getattr(
            self.session.scenario_plan,
            "example_values",
            {},
        )
        if not isinstance(example_values, dict):
            example_values = {}
        table = getattr(step, "table", None)
        table = table if isinstance(table, dict) else {}
        references = tuple(sorted({
            *(
                str(key)
                for key in example_values
            ),
            *(
                str(item)
                for item in (table.get("headings") or ())
            ),
        }))
        result = []
        repository = RecordingAnnotationRepository(self.session_dir)
        for take in takes:
            if take.directory_path is None:
                continue
            action_by_id, action_by_event = _observation_action_indexes(
                take.directory_path
            )
            events = {
                str(event.get("id") or ""): event
                for event in TakeQueryService(
                    take.directory_path
                ).effective_observation_events()
                if event.get("id")
            }
            intents = repository.current_observation_intents(
                step_id=step.id,
                take_id=take.take_id,
            )
            result.extend(
                _observation_dto(
                    intent,
                    events.get(str(intent.get("event_id") or "")) or {},
                    action=_observation_action(
                        intent,
                        action_by_id,
                        action_by_event,
                    ),
                    owner_take_id=take.take_id,
                    reference_options=references,
                )
                for intent in intents
                if str(intent.get("event_id") or "") in events
            )
            supplement_ids = sorted({
                str(
                    ((event.get("details") or {}).get("supplement") or {})
                    .get("supplement_id")
                    or ""
                )
                for event in events.values()
                if ((event.get("details") or {}).get("supplement") or {}).get(
                    "supplement_id"
                )
            })
            supplements = SupplementRepository(take.directory_path)
            for supplement_id in supplement_ids:
                try:
                    supplement_repository = RecordingAnnotationRepository(
                        supplements.path_for(supplement_id)
                    )
                    supplement_intents = (
                        supplement_repository.current_observation_intents(
                            step_id=step.id,
                            take_id=supplement_id,
                        )
                    )
                except (OSError, ValueError):
                    continue
                result.extend(
                    _observation_dto(
                        intent,
                        events.get(
                            str(intent.get("event_id") or "")
                        ) or {},
                        action=_observation_action(
                            intent,
                            action_by_id,
                            action_by_event,
                        ),
                        owner_take_id=take.take_id,
                        reference_options=references,
                    )
                    for intent in supplement_intents
                    if str(intent.get("event_id") or "") in events
                )
        return tuple(sorted(
            result,
            key=lambda item: (
                item.action_ordinal is None,
                item.action_ordinal or 0,
                item.event_id,
            ),
        ))

    def _take_dto(self, take, *, fallback_number, selected_take_id):
        relative_path = str(take.get("path") or "")
        directory = (self.session_dir / relative_path).resolve()
        try:
            directory.relative_to(self.session_dir)
        except ValueError:
            directory_path = None
        else:
            directory_path = str(directory) if directory.is_dir() else None
        return TakeDTO(
            take_id=str(take.get("id") or ""),
            take_number=int(
                take.get("take_number") or fallback_number
            ),
            status=str(take.get("status") or "unknown"),
            path=relative_path,
            directory_path=directory_path,
            review_text=str(
                take.get("take_summary")
                or take.get("discard_reason")
                or ""
            ).strip(),
            action_count=int(
                take.get(
                    "effective_action_count",
                    take.get("action_count", 0),
                ) or 0
            ),
            event_count=int(take.get("event_count") or 0),
            window_count=int(
                take.get("window_count")
                or len(take.get("target_windows") or ())
                or 0
            ),
            selected=take.get("id") == selected_take_id,
            timeline_revision=take.get("timeline_revision"),
            evidence_summary=_take_evidence_summary(directory_path),
            xpath_details=_take_xpath_details(directory_path),
        )

    def _scenario_step_ids(self):
        return tuple(
            step.id
            for step in self.session.selected_steps
            if (self.session.step_states.get(step.id) or {}).get("status")
            != "skipped"
        )

    def _scenario_scope(self):
        step_ids = self._scenario_step_ids()
        incomplete_step_ids = tuple(
            str(step.id)
            for step in self.session.selected_steps
            if (
                (self.session.step_states.get(step.id) or {}).get("status")
                not in {"completed", "skipped"}
                or (
                    (self.session.step_states.get(step.id) or {}).get(
                        "status"
                    ) == "completed"
                    and self.session.selected_take_entry(
                        step.id,
                        require_directory=True,
                    ) is None
                )
            )
        )
        all_step_ids = tuple(
            str(step.id)
            for step in self.session.scenario_plan.steps
        )
        selected = set(step_ids)
        excluded = tuple(
            step_id
            for step_id in all_step_ids
            if step_id not in selected
        )
        complete = bool(all_step_ids) and not excluded
        capture_generation_candidate = bool(step_ids) and not incomplete_step_ids
        return ScenarioScopeDTO(
            kind="scenario",
            complete=complete,
            selected_step_ids=step_ids,
            excluded_step_ids=excluded,
            selected_count=len(step_ids),
            total_count=len(all_step_ids),
            label=(
                "完整场景"
                if complete
                else f"部分场景 {len(step_ids)}/{len(all_step_ids)} Step"
            ),
            capture_generation_candidate=capture_generation_candidate,
            incomplete_step_ids=incomplete_step_ids,
        )

    def _scenario_generation_summary(
            self,
            *,
            scope=None,
            include_result=False,
        ):
        scope = scope or self._scenario_scope()
        step_ids = scope.selected_step_ids
        if not _scope_capture_generation_candidate(scope):
            return _generation_summary(
                workflow_status="scenario_incomplete",
                display_status="scenario_incomplete",
                next_action="complete_scenario_recording",
            )
        request = self.requests.latest(step_ids)
        if request is None:
            running = _running_transaction_for_scope(
                self.session_dir,
                step_ids,
            )
            if running is not None:
                report_path, report = running
                return GenerationSummaryDTO(
                    workflow_status="running",
                    display_status="running",
                    next_action="finish_generation_transaction",
                    request_id=report.get("request_id"),
                    request_path=report.get("request_path"),
                    risk_mode=(report.get("risk") or {}).get("mode"),
                    decision=_decision_summary({}),
                    result=_generation_result(report_path, report),
                    required_forensic_evidence=(),
                    errors=(),
                    warnings=tuple(
                        str(item) for item in report.get("warnings") or ()
                    ),
                    recommended_action="pending",
                    recommended_label="生成进行中",
                    recommended_detail="当前生成事务尚未完成。",
                )
            return _generation_summary(
                workflow_status="updating",
                display_status="updating",
                next_action="materialize_latest_request",
            )
        workflow = self.requests.workflow_state(step_ids)
        status = str((workflow or {}).get("status") or "draft")
        display_status = {
            "draft": "ready",
            "ready": "ready",
            "needs_adjustment": "needs_input",
            "forensic": "needs_input",
            "blocked": "blocked",
            "stale": "updating",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
        }.get(status, "updating")
        job = load_generation_job(
            self.session_dir,
            workflow.get("current_job") or {},
        ) if workflow.get("current_job") else None
        job_result = load_generation_job_result(
            self.session_dir,
            workflow.get("last_job_result") or {},
        ) if workflow.get("last_job_result") else None
        terminal_job_result = bool(
            workflow.get("last_job_result")
            and (
                not workflow.get("current_job")
                or status in {"completed", "failed"}
            )
        )
        result = None
        if include_result:
            if (
                terminal_job_result
                and job_result is not None
            ):
                bound = _job_result_transaction(
                    self.session_dir,
                    job_result,
                )
                result = (
                    _with_job_result(
                        _generation_result(*bound),
                        job_result,
                    )
                    if bound is not None
                    else _generation_result_from_job_result(
                        self.session_dir,
                        workflow.get("last_job_result") or {},
                        job_result,
                    )
                )
            elif workflow.get("current_job"):
                result = None
            else:
                latest = latest_transaction(
                    self.session_dir,
                    request_id=request.get("request_id"),
                )
                if latest is not None:
                    report_path, report = latest
                    result = _generation_result(report_path, report)
        decision = _decision_summary(
            workflow.get("decision") or {},
            session_dir=getattr(self, "session_dir", None),
            request=request,
        )
        request_path = request.get("request_path")
        ownership = _window_ownership_summary(
            getattr(self, "session_dir", None),
            workflow.get("brief") or {},
        )
        ambiguity = workflow.get("ambiguity") or {}
        recommended_action, recommended_label, recommended_detail = (
            _generation_recommendation(status, result, scope.label)
        )
        return GenerationSummaryDTO(
            workflow_status=status,
            display_status=display_status,
            next_action=str(
                workflow.get("next_action")
                or "inspect"
            ),
            request_id=request.get("request_id"),
            request_path=(
                str(self.session_dir / str(request_path))
                if request_path
                else None
            ),
            risk_mode=(workflow.get("risk") or {}).get("mode"),
            decision=decision,
            result=result,
            required_forensic_evidence=tuple(
                str(item)
                for item in workflow.get(
                    "required_forensic_evidence"
                ) or ()
            ),
            errors=tuple(str(item) for item in workflow.get("errors") or ()),
            warnings=tuple(
                str(item) for item in workflow.get("warnings") or ()
            ),
            window_ownership=ownership,
            pending_ai_ambiguities=int(
                ambiguity.get("pending_ai_count") or 0
            ),
            pending_user_ambiguities=int(
                ambiguity.get("pending_user_count") or 0
            ),
            pending_evidence_ambiguities=int(
                ambiguity.get("pending_evidence_count") or 0
            ),
            recommended_action=recommended_action,
            recommended_label=recommended_label,
            recommended_detail=recommended_detail,
            brief_size_bytes=int(
                (workflow.get("brief") or {}).get("size_bytes") or 0
            ),
            job_id=(job or {}).get("job_id"),
            job_phase=(workflow.get("job_execution") or {}).get("phase"),
            generation_profile_id=(
                (job.get("profile_lease") or {}).get("profile_id")
                if job is not None
                else None
            ),
            job_history=_generation_job_history(
                getattr(self, "session_dir", None),
                workflow,
                job,
            ),
            feedback_history=_feedback_history(
                getattr(self, "session_dir", None),
                request.get("request_id"),
            ),
        )

    @staticmethod
    def _generation_status(state, scenario_generation_status):
        if state.get("status") != "completed" or not state.get("selected_take"):
            return "unavailable"
        return scenario_generation_status

    def _run_status(self):
        if self.session.is_recording:
            return "recording"
        if self.session.is_finalized:
            return "finalized"
        if self.session.is_closed:
            return "closed"
        return "open"


def _diagnostic_evidence_path(selected_take, diagnostic):
    if selected_take is None or selected_take.directory_path is None:
        return None
    try:
        return str(
            TakeQueryService(
                selected_take.directory_path
            ).diagnostic_evidence_path(diagnostic)
        )
    except (OSError, ValueError):
        return None


def _observation_dto(
        intent,
        event,
        *,
    action,
        owner_take_id,
        reference_options,
    ):
    target = (event.get("target") or {}).get("element") or {}
    source = intent.get("expected_source") or {"kind": "auto"}
    focus = str(intent.get("focus") or "auto")
    source_kind = str(source.get("kind") or "auto")
    target_name = str(target.get("name") or "当前目标")
    focus_label = {
        "auto": "自动核对业务描述",
        "text": "显示文字",
        "value": "当前值",
        "visible": "是否可见",
        "enabled": "是否可用",
        "window_title": "窗口标题",
        "region_text": "显示文字",
        "collection": "列表内容",
        "property": "控件状态",
    }.get(focus, "自动核对业务描述")
    source_label = {
        "auto": "Feature",
        "feature": "Feature",
        "examples": "Examples",
        "data_table": "Data Table",
        "observed_state": "当前观察结果（尚未确认）",
    }.get(source_kind, "Feature")
    reference = str(source.get("reference") or "").strip() or None
    if reference:
        source_label = f"{source_label}.{reference}"
    needs_confirmation = bool(
        intent.get("authority") == SYSTEM_INFERRED_INTENT
        and source_kind == "observed_state"
    )
    suffix = "；需要确认业务期望" if needs_confirmation else ""
    return ObservationDTO(
        annotation_id=str(intent.get("annotation_id") or ""),
        event_id=str(intent.get("event_id") or ""),
        action_id=(str(action.get("id")) if action else None),
        action_ordinal=(
            int(action.get("ordinal"))
            if action and action.get("ordinal") is not None
            else None
        ),
        take_id=str(intent.get("take_id") or ""),
        owner_take_id=str(owner_take_id or ""),
        revision=int(intent.get("revision") or 0),
        authority=str(intent.get("authority") or ""),
        focus=focus,
        relation=str(intent.get("relation") or "auto"),
        property_name=(
            str(intent.get("property_name"))
            if intent.get("property_name")
            else None
        ),
        expected_source_kind=source_kind,
        expected_source_reference=reference,
        business_meaning=str(intent.get("business_meaning") or ""),
        target_name=target_name,
        target_control_type=str(target.get("control_type") or ""),
        summary=(
            f"检查“{target_name}”的{focus_label}；期望来自{source_label}"
            f"{suffix}"
        ),
        needs_business_confirmation=needs_confirmation,
        reference_options=reference_options,
    )


def _observation_action_indexes(take_dir):
    by_id = {}
    by_event = {}
    try:
        actions = TimelineStore(take_dir).review_actions()
    except (OSError, ValueError):
        actions = ()
    for action in actions:
        if not isinstance(action, dict) or not action.get("id"):
            continue
        for action_id in (
            action.get("id"),
            action.get("source_action_id"),
            *(action.get("source_action_ids") or ()),
        ):
            if action_id:
                by_id.setdefault(str(action_id), action)
        for event_id in action.get("event_ids") or ():
            if event_id:
                by_event.setdefault(str(event_id), action)
    return by_id, by_event


def _observation_action(intent, by_id, by_event):
    action_id = str(intent.get("action_id") or "")
    event_id = str(intent.get("event_id") or "")
    return by_id.get(action_id) or by_event.get(event_id)


def _take_evidence_summary(directory_path):
    if directory_path is None:
        return None
    try:
        graph = TakeQueryService(directory_path).evidence_graph()
    except (OSError, ValueError):
        return None
    if not isinstance(graph, dict) or not graph:
        return None
    coverage = graph.get("coverage") or {}
    events = coverage.get("events") or {}
    actions = coverage.get("actions") or {}
    source = graph.get("source") or {}
    return EvidenceSummaryDTO(
        linked_event_count=int(events.get("linked_to_actions") or 0),
        event_count=int(events.get("total") or 0),
        complete_action_count=int(actions.get("complete_envelopes") or 0),
        action_count=int(actions.get("total") or 0),
        artifact_count=int(source.get("artifact_count") or 0),
    )


def _take_xpath_details(directory_path):
    if directory_path is None:
        return ()
    try:
        graph = TakeQueryService(directory_path).evidence_graph()
    except (OSError, ValueError):
        return ()
    result = []
    for action in (graph or {}).get("actions") or ():
        target = (action or {}).get("target") or {}
        locator = target.get("locator") or {}
        if str(locator.get("by") or "").casefold() != "xpath":
            continue
        xpath = str(locator.get("value") or "")
        if not xpath:
            continue
        positional = bool(
            target.get("locator_strategy") == "positional_fallback"
            or target.get("positional_fallback") is True
        )
        unique = target.get("locator_validation") == "unique_target_match"
        result.append(XPathTechnicalDetailDTO(
            action_id=str(action.get("action_id") or ""),
            ordinal=(
                int(action["ordinal"])
                if action.get("ordinal") is not None
                else None
            ),
            locator_name=(
                str(target.get("locator_name"))
                if target.get("locator_name")
                else None
            ),
            xpath=xpath,
            validation=str(target.get("locator_validation") or "unvalidated"),
            stability=str(
                (target.get("locator_stability") or {}).get("status")
                or "unknown"
            ),
            generation_status=(
                "position_risk"
                if positional
                else "eligible"
                if unique
                else "not_eligible"
            ),
        ))
    return tuple(sorted(result, key=lambda item: (
        item.ordinal is None,
        item.ordinal or 0,
        item.action_id,
    )))


def _effective_readiness(readiness, ambiguity_projection):
    if not isinstance(ambiguity_projection, dict):
        return readiness
    known = set(ambiguity_projection.get("known_review_ids") or ())
    visible = set(ambiguity_projection.get("visible_review_ids") or ())
    filtered = []
    for review in readiness.get("review_required") or ():
        source_id = review_source_id(review)
        if source_id not in known or source_id in visible:
            filtered.append(review)
    if len(filtered) == len(readiness.get("review_required") or ()):
        return readiness
    return {**readiness, "review_required": filtered}


def _evidence_status(state, issues):
    if state.get("status") != "completed" or not state.get("selected_take"):
        return "unavailable"
    if any(issue.blocking for issue in issues):
        return "broken"
    if issues:
        return "needs_review"
    return "clean"


def _next_action(
        capture_status,
        evidence_status,
        generation_status,
):
    if capture_status == "skipped":
        return "excluded", "已排除生成"
    if capture_status != "completed":
        return "record", "录制当前 Step"
    if evidence_status == "broken":
        return "repair", "修复证据"
    if evidence_status == "needs_review":
        return "review", "检查录制内容"
    if generation_status == "scenario_incomplete":
        return "complete_scenario", "完成场景录制"
    if generation_status == "ready":
        return "generate", "交给 Copilot"
    if generation_status == "needs_input":
        return "adjust", "确认业务问题"
    if generation_status == "running":
        return "wait", "生成中"
    if generation_status == "completed":
        return "review_result", "查看本次生成"
    if generation_status == "failed":
        return "repair_generation", "修复生成结果"
    if generation_status == "blocked":
        return "repair", "校正或补录"
    return "wait", "正在更新证据"


def recommend_step_action(readiness, step_id, step_states):
    state = step_states.get(step_id) if step_id else None
    if not state or state.get("status") != "completed":
        return "pending", "等待录制", "此 Step 尚未完成录制。"
    if not readiness.get("bundle_valid"):
        return (
            "rerecord",
            "校正或补录",
            "部分录制证据无法使用，请按右侧问题提示校正或补录当前 Step。",
        )
    reviews = [
        item
        for item in readiness.get("review_required") or []
        if item.get("step_id") in (None, step_id)
    ]
    if not reviews:
        return (
            "generate",
            "交给 Copilot",
            "录制证据已准备好，Copilot 将完成实现并运行生成检查。",
        )
    codes = {item.get("code") for item in reviews}
    hard_recovery = [
        item
        for item in reviews
        if (item.get("recovery") or {}).get("hard_blocker")
    ]
    ai_recoverable = [
        item
        for item in reviews
        if (item.get("recovery") or {}).get("status") == "ai_recoverable"
    ]
    if hard_recovery:
        return (
            "rerecord",
            "补录当前 Step",
            f"下方有 {len(hard_recovery)} 项必要证据缺失。"
            "只需补录当前 Step，其他录制不会受影响。",
        )
    if codes and codes <= TIMELINE_CORRECTABLE_CODES:
        return (
            "timeline",
            "检查并忽略误录",
            "检测到可能不属于当前业务 Step 的录制动作；"
            "确认是误录时可在录制内容中忽略："
            + "、".join(sorted(codes)),
        )
    hard_evidence_codes = {
        "capture_error",
        "tree_not_comparable",
        "no_recorded_actions",
    }
    if codes & hard_evidence_codes and not ai_recoverable:
        return (
            "rerecord",
            "最小补录当前 Step",
            "核心结构化或可视证据不足，无法形成可验证计划。",
        )
    return (
        "generate",
            "交给 Copilot",
            f"有 {len(ai_recoverable)} 项录制事实需要核对；"
            "Copilot 会自动处理，只有业务含义无法确定时才会询问你。",
    )


def _user_task_projection(
        *,
        scope,
        generation,
        steps,
        selected_step_id,
        readiness,
    ):
    step_by_id = {step.step_id: step for step in steps}

    def task(
            owner,
            requires_user_action,
            action,
            action_label,
            target_view,
            reason,
            target_step_id=None,
        ):
        owner_step = step_by_id.get(target_step_id)
        return UserTaskProjection(
            owner=owner,
            requires_user_action=requires_user_action,
            action=action,
            action_label=action_label,
            target_view=target_view,
            reason=reason,
            target_step_id=target_step_id,
            owner_take_id=(
                owner_step.selected_take_id if owner_step else None
            ),
        )

    incomplete_step_ids = tuple(
        step_id
        for step_id in (getattr(scope, "incomplete_step_ids", ()) or ())
        if step_id in step_by_id
    )
    if incomplete_step_ids:
        target_step_id = incomplete_step_ids[0]
        return task(
            "evidence",
            True,
            "rerecord",
            "录制当前 Step",
            "capture",
            f"还有 {len(incomplete_step_ids)} 个目标 Step 没有可用录制。",
            target_step_id,
        )
    if not step_by_id:
        return task(
            "system",
            False,
            "pending",
            "没有可处理 Step",
            "capture",
            "当前范围没有可处理的 Step。",
        )

    selected_step_id = (
        selected_step_id
        if selected_step_id in step_by_id
        else next(iter(step_by_id))
    )
    status = generation.workflow_status
    if status == "completed":
        return task(
            "user",
            True,
            "review_result",
            "查看本次生成",
            "review",
            generation.recommended_detail,
        )
    if status == "failed":
        return task(
            "copilot",
            True,
            "review_failed_result",
            generation.recommended_label,
            "review",
            generation.recommended_detail,
        )
    reviews = [
        item
        for item in readiness.get("review_required") or ()
        if isinstance(item, dict)
        and item.get("step_id") in (None, *step_by_id)
    ]

    def review_target(items):
        return next(
            (
                str(item.get("step_id"))
                for item in items
                if item.get("step_id") in step_by_id
            ),
            selected_step_id,
        )

    hard_blockers = [
        item
        for item in reviews
        if (item.get("recovery") or {}).get("hard_blocker")
    ]
    if hard_blockers:
        target_step_id = review_target(hard_blockers)
        return task(
            "evidence",
            True,
            "rerecord",
            "补录当前 Step",
            "capture",
            f"有 {len(hard_blockers)} 项生成所必需的真实证据缺失。",
            target_step_id,
        )

    correctable = [
        item
        for item in reviews
        if item.get("code") in TIMELINE_CORRECTABLE_CODES
    ]
    if correctable:
        target_step_id = review_target(correctable)
        return task(
            "evidence",
            True,
            "timeline",
            "检查并忽略误录",
            "timeline",
            "检测到可能不属于业务流程的录制动作，请检查本次录制。",
            target_step_id,
        )

    if status == "needs_adjustment":
        target_step_id = review_target(reviews)
        return task(
            "user",
            True,
            "v3_adjust",
            "确认业务问题",
            "review",
            "有业务含义只有你能确认，确认结果只约束 Copilot 的实现。",
            target_step_id,
        )
    if status in {"draft", "ready", "forensic"}:
        return task(
            "copilot",
            True,
            generation.recommended_action,
            "交给 Copilot",
            "review",
            generation.recommended_detail,
        )
    if status == "blocked":
        target_step_id = review_target(reviews)
        return task(
            "evidence",
            True,
            "rerecord",
            "校正或补录",
            "capture",
            "缺少生成所必需的真实证据，请校正或补录当前 Step。",
            target_step_id,
        )
    return task(
        "system",
        False,
        "pending",
        generation.recommended_label,
        "review",
        generation.recommended_detail,
    )


def _generation_summary(
        *,
        workflow_status,
        display_status,
        next_action,
    ):
    return GenerationSummaryDTO(
        workflow_status=workflow_status,
        display_status=display_status,
        next_action=next_action,
        request_id=None,
        request_path=None,
        risk_mode=None,
        decision=_decision_summary({}),
        result=None,
        required_forensic_evidence=(),
        errors=(),
        warnings=(),
        recommended_action="pending",
        recommended_label=(
            "完成场景录制"
            if workflow_status == "scenario_incomplete"
            else "正在准备"
        ),
        recommended_detail=(
            "完成当前范围的录制后即可准备 Copilot 任务。"
            if workflow_status == "scenario_incomplete"
            else "证据与生成状态正在更新。"
        ),
    )


def _generation_job_history(session_dir, workflow, current_job):
    if session_dir is None:
        return ()
    session_dir = Path(session_dir).resolve()
    entries = []
    if current_job is not None:
        execution = workflow.get("job_execution") or {}
        entries.append(GenerationJobHistoryDTO(
            job_id=str(current_job.get("job_id") or ""),
            job_path=str((workflow.get("current_job") or {}).get("path") or ""),
            status=str(workflow.get("status") or "ready"),
            phase=execution.get("phase"),
            reason=None,
            profile_id=(current_job.get("profile_lease") or {}).get(
                "profile_id"
            ),
            retired_at=None,
            result_status=None,
            result_path=None,
            is_current=True,
        ))
    for retired in reversed(list(workflow.get("retired_jobs") or ())):
        pointer = (retired or {}).get("job") or {}
        job = load_generation_job(session_dir, pointer)
        if job is None:
            continue
        result_pointer = retired.get("last_job_result") or {}
        result = load_generation_job_result(session_dir, result_pointer)
        entries.append(GenerationJobHistoryDTO(
            job_id=str(job.get("job_id") or ""),
            job_path=str(pointer.get("path") or ""),
            status=str(retired.get("status") or "failed"),
            phase=retired.get("phase"),
            reason=retired.get("reason"),
            profile_id=(job.get("profile_lease") or {}).get("profile_id"),
            retired_at=retired.get("retired_at"),
            result_status=(result or {}).get("status"),
            result_path=result_pointer.get("path"),
        ))
    return tuple(entries)


def _feedback_history(session_dir, request_id):
    if session_dir is None or not request_id:
        return ()
    try:
        events, _warnings = load_memory_events(
            find_recording_root(session_dir),
        )
    except OSError:
        return ()
    matching = [
        event
        for event in events
        if (
            event.get("kind") == "transaction_feedback"
            and (event.get("source") or {}).get("request_id") == request_id
        )
    ]
    superseded = {
        str(memory_id)
        for event in matching
        for memory_id in event.get("supersedes") or ()
        if memory_id
    }
    return tuple(
        FeedbackHistoryDTO(
            memory_id=str(event.get("memory_id") or ""),
            status=str(event.get("status") or ""),
            tier=(event.get("payload") or {}).get(
                "accepted_feedback_tier"
            ),
            claim=str(event.get("claim") or ""),
            created_at=event.get("created_at"),
            transaction_id=(event.get("source") or {}).get(
                "transaction_id"
            ),
            supersedes=tuple(
                str(item) for item in event.get("supersedes") or ()
            ),
            is_effective=str(event.get("memory_id") or "") not in superseded,
        )
        for event in reversed(matching)
    )


def _scope_capture_generation_candidate(scope):
    return bool(getattr(scope, "capture_generation_candidate", False))


def _verification_summary(
    result,
    runtime_result,
    *,
    transaction_valid,
    quality=None,
):
    changed_files = tuple(
        getattr(result, "changed_files", ()) or ()
    )
    result_status = str(getattr(result, "status", "") or "")
    code_generated = bool(result is not None and (
        changed_files
        or result_status in {"completed", "completed_no_changes"}
    ))
    workspace = getattr(result, "workspace_materialization", None)
    workspace_status = str(getattr(workspace, "status", "") or "")
    workspace_matches = workspace_status in {
        "",
        "matches_report",
        "not_recorded",
        "not_applicable",
    }
    if result is not None and not workspace_matches:
        missing_count = len(getattr(workspace, "missing_files", ()) or ())
        modified_count = len(getattr(workspace, "modified_files", ()) or ())
        return VerificationSummaryDTO(
            level="workspace_materialization_stale",
            label="生成文件已过期",
            detail=(
                "历史生成报告仍然存在，但当前工作区文件已与报告快照不一致；"
                f"缺失 {missing_count}，已修改 {modified_count}。"
            ),
            code_generated=False,
            implementation_validated=False,
            runtime_verified=False,
        )
    implementation_validated = bool(
        transaction_valid
        and result is not None
        and result_status in {"completed", "completed_no_changes"}
        and getattr(result, "failure_category", None)
        != "generated_with_issues"
        and result.stages is not None
        and result.stages.implementation.status == "passed"
        and result.stages.transaction.status == "passed"
        and not tuple(getattr(result, "failed_checks", ()) or ())
        and not tuple(getattr(result, "errors", ()) or ())
    )
    runtime_verified = bool(
        implementation_validated
        and runtime_result is not None
        and str(getattr(runtime_result, "status", "") or "") == "passed"
        and (
            quality is None
            or _single_run_passed(quality) is True
        )
    )
    if runtime_verified:
        oracle_verified = bool(
            quality is not None
            and _oracle_passed(quality) is True
        )
        return VerificationSummaryDTO(
            level=(
                "oracle_verified"
                if oracle_verified
                else "single_run_passed"
            ),
            label=(
                "独立业务状态已验证"
                if oracle_verified
                else "真实运行已通过"
            ),
            detail=(
                "当前 Transaction 的风险矩阵和独立业务状态 Oracle 已通过。"
                if oracle_verified
                else "当前 Transaction 和代码快照的真实 Behave 场景已通过；"
                "尚无独立业务状态 Oracle 结论。"
            ),
            code_generated=True,
            implementation_validated=True,
            runtime_verified=True,
        )
    if implementation_validated:
        execution_status = str(
            getattr(result, "execution_status", "") or ""
        )
        if execution_status == "static_validated/runtime_not_run":
            detail = (
                "代码和证据检查已通过；当前Execution Profile未授权运行，"
                "因此运行状态为未执行。"
            )
        elif execution_status == "static_validated/runtime_pending":
            detail = (
                "代码和证据检查已通过；当前Execution Profile允许运行，"
                "正在等待绑定同一Transaction的真实结果。"
            )
        else:
            detail = (
                "代码和证据检查已通过，但当前代码快照尚无通过的真实Behave结果。"
            )
        return VerificationSummaryDTO(
            level="implementation_validated",
            label="静态安全检查通过",
            detail=detail,
            code_generated=True,
            implementation_validated=True,
            runtime_verified=False,
        )
    if code_generated:
        return VerificationSummaryDTO(
            level="code_generated",
            label="代码已生成，检查未通过",
            detail="已有生成文件，但静态安全检查或事务完成条件尚未通过。",
            code_generated=True,
            implementation_validated=False,
            runtime_verified=False,
        )
    return VerificationSummaryDTO(
        level="not_generated",
        label="尚未生成代码",
        detail="当前范围尚无生成事务结果。",
        code_generated=False,
        implementation_validated=False,
        runtime_verified=False,
    )


def _generation_recommendation(status, result, scope_label):
    if status == "draft":
        return (
            "v3_plan",
            "交给 Copilot",
            "录制证据已准备好，Copilot 将完成实现并运行生成检查。",
        )
    if status == "ready":
        return (
            "v3_fast",
            "交给 Copilot",
            f"{scope_label}已准备好，可以交给 Copilot 生成并验证代码。",
        )
    if status == "forensic":
        return (
            "v3_forensic",
            "交给 Copilot",
            "有几处录制事实需要核对；Copilot 会自动读取相关证据，"
            "不需要你处理技术细节。",
        )
    if status == "needs_adjustment":
        return (
            "v3_adjust",
            "交给 Copilot",
            "有业务含义无法自动确定；Copilot 会一次性向你说明并询问。",
        )
    if status == "blocked":
        return (
            "rerecord",
            "校正或补录",
            "缺少生成所必需的真实证据。请查看右侧具体问题，"
            "校正或补录后系统会自动重新准备。",
        )
    if status == "running":
        return "pending", "正在生成", "Copilot 生成和检查尚未完成。"
    if status == "completed":
        return (
            "review_result",
            "查看本次生成",
            "生成已完成，请查看修改内容和真实运行结果。",
        )
    if status == "failed":
        return (
            "review_failed_result",
            result.recommended_label if result else "查看失败报告",
            _generation_failure_detail(result),
        )
    if status == "stale":
        return (
            "pending",
            "正在准备",
            "录制或检查已变化，系统正在自动准备最新任务。",
        )
    return "pending", "正在准备", "证据与生成状态正在更新。"


def _generation_failure_detail(result):
    if result is None:
        return "本次生成未完成；请查看详细报告。"
    category = {
        "generated_files": "生成文件未通过代码或定位检查",
        "plan_conformance": "生成内容与已确认的业务任务不一致",
        "evidence": "生成内容缺少录制依据",
        "policy": "当前定位方式需要修复或由你确认风险",
        "transaction": "本次生成未完成",
    }.get(result.failure_category, "本次生成未完成")
    return f"{category}；请打开详细报告查看具体原因。"


def _window_ownership_summary(session_dir, brief_pointer):
    if session_dir is None:
        return None
    value = str((brief_pointer or {}).get("path") or "").strip()
    if not value:
        return None
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (
        Path(session_dir) / path
    ).resolve()
    try:
        path.relative_to((Path(session_dir) / "ai/generation-briefs").resolve())
        brief = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError):
        return None
    counts = {
        "reuse_existing": 0,
        "create_new": 0,
        "ambiguous": 0,
        "unresolved": 0,
    }
    for window in (
        (brief.get("window_ownership") or {}).get("windows") or []
    ):
        strategy = str(
            ((window.get("owner_match") or {}).get("suggested_strategy"))
            or "unresolved"
        )
        if strategy not in counts:
            strategy = "unresolved"
        counts[strategy] += 1
    return WindowOwnershipSummaryDTO(**counts)


def _decision_summary(
        decision,
        *,
        session_dir=None,
        request=None,
    ):
    pack = decision.get("pack") or {}
    questions = _decision_questions(
        decision,
        session_dir=session_dir,
        request=request,
    )
    return DecisionSummaryDTO(
        status=str(decision.get("status") or "not_available"),
        question_count=int(pack.get("question_count") or 0),
        blocking_count=int(pack.get("blocking_count") or 0),
        forensic_blocking_count=int(
            pack.get("forensic_blocking_count") or 0
        ),
        pack_path=(str(pack.get("path")) if pack.get("path") else None),
        questions=questions,
    )


def _runtime_diagnostic_dto(value):
    if not isinstance(value, dict):
        return None
    required = ("code", "category", "stage", "summary")
    if any(
        not isinstance(value.get(key), str) or not value[key]
        for key in required
    ):
        return None
    return RuntimeDiagnosticDTO(
        code=value["code"],
        category=value["category"],
        stage=value["stage"],
        summary=value["summary"],
        backend=(str(value["backend"]) if value.get("backend") else None),
        locator_name=(
            str(value["locator_name"])
            if value.get("locator_name") else None
        ),
        root_name=(
            str(value["root_name"])
            if value.get("root_name") else None
        ),
        wait_type=(
            str(value["wait_type"])
            if value.get("wait_type") else None
        ),
        timeout_seconds=_optional_float(value.get("timeout_seconds")),
        probe_count=_optional_int(value.get("probe_count")),
        candidate_count=_optional_int(value.get("candidate_count")),
        cause_type=(
            str(value["cause_type"])
            if value.get("cause_type") else None
        ),
        cause_message=(
            str(value["cause_message"])
            if value.get("cause_message") else None
        ),
    )


def _verified_runtime_file(record):
    path = verified_file_path(record)
    if path is None:
        return None
    try:
        digest = str(record.get("sha256") or "")
        size = int(record.get("size"))
    except (AttributeError, TypeError, ValueError):
        return str(path), None, None
    return str(path), digest or None, size


def _optional_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _decision_questions(decision, *, session_dir=None, request=None):
    if session_dir is None or not isinstance(request, dict):
        return ()
    try:
        loaded_pack, context = _load_request_decision_material(
            session_dir,
            request,
            decision,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ()

    return tuple(
        _decision_question_dto(
            question,
            context,
            Path(session_dir).resolve(),
        )
        for question in loaded_pack.get("questions") or ()
        if isinstance(question, dict)
    )


def query_request_decision_media(
        session_dir,
        request,
        decision,
        *,
        question_id=None,
    ):
    """Return Request-bound verified media for Decision questions."""
    loaded_pack, context = _load_request_decision_material(
        session_dir,
        request,
        decision,
        require_context_fingerprint=True,
    )
    questions = [
        item for item in loaded_pack.get("questions") or ()
        if isinstance(item, dict)
    ]
    if question_id is not None:
        question_id = str(question_id)
        questions = [
            item for item in questions
            if str(item.get("question_id") or "") == question_id
        ]
        if not questions:
            raise ValueError("Decision问题不属于当前Request")
    return {
        "decision_pack_id": str(loaded_pack.get("pack_id") or ""),
        "context_fingerprint": str(
            context.get("context_fingerprint") or ""
        ),
        "questions": [
            project_decision_question_media(
                question,
                context,
                Path(session_dir).resolve(),
            )
            for question in questions
        ],
    }


def _load_request_decision_material(
        session_dir,
        request,
        decision,
        *,
        require_context_fingerprint=False,
    ):
    session_dir = Path(session_dir).resolve()
    if not isinstance(request, dict):
        raise ValueError("Decision媒体查询缺少Request")
    loaded_pack = load_decision_pack(
        session_dir,
        (decision or {}).get("pack") or {},
        request,
    )
    if loaded_pack is None:
        raise ValueError("当前Request没有身份有效的Decision Pack")
    declared_context = request.get("evidence_context") or {}
    context_path = resolve_session_path(
        session_dir,
        declared_context.get("path"),
    )
    context = load_evidence_context(context_path)
    if context.get("request_id") != request.get("request_id"):
        raise ValueError("Decision媒体Evidence Context与Request不一致")
    declared_fingerprint = str(
        declared_context.get("context_fingerprint") or ""
    )
    if require_context_fingerprint and not declared_fingerprint:
        raise ValueError("Decision媒体Evidence Context fingerprint缺失")
    if (
            declared_fingerprint
            and context.get("context_fingerprint") != declared_fingerprint
    ):
        raise ValueError("Decision媒体Evidence Context指纹不一致")
    return loaded_pack, context


def _decision_question_dto(question, context, session_dir):
    presentation = question.get("presentation") or {}
    step = presentation.get("step") or {}
    option_effects = {
        str(item.get("option_id") or ""): str(item.get("effect") or "")
        for item in presentation.get("option_effects") or ()
        if isinstance(item, dict)
    }
    return DecisionQuestionDTO(
        question_id=str(question.get("question_id") or ""),
        question_type=str(question.get("type") or ""),
        title=str(question.get("title") or ""),
        prompt=str(question.get("prompt") or ""),
        step_id=str(question.get("step_id") or step.get("id") or ""),
        step_text=str(step.get("text") or ""),
        observed=str(presentation.get("observed") or ""),
        uncertainty=str(presentation.get("uncertainty") or ""),
        options=tuple(
            DecisionOptionDTO(
                option_id=str(option.get("option_id") or ""),
                label=str(option.get("label") or ""),
                effect=option_effects.get(
                    str(option.get("option_id") or ""),
                    "",
                ),
            )
            for option in question.get("options") or ()
            if isinstance(option, dict)
        ),
        action_ids=tuple(
            str(item) for item in question.get("action_ids") or ()
            if item
        ),
        evidence_ids=tuple(
            str(item) for item in question.get("evidence_ids") or ()
            if item
        ),
        media=_question_media(
            question,
            context,
            session_dir,
        ),
        blocking=bool(question.get("blocking")),
    )


def _question_media(question, context, session_dir):
    step_id = str(question.get("step_id") or "")
    action_ids = {
        str(item) for item in question.get("action_ids") or () if item
    }
    if not action_ids:
        return ()
    take_dir = _question_take_dir(session_dir, context, step_id)
    if take_dir is None:
        return ()

    target_rectangles = {}
    for item in context.get("items") or ():
        if any((
            not isinstance(item, dict),
            item.get("kind") != "target",
            str(item.get("step_id") or "") != step_id,
        )):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        action_id = evidence_id.rsplit(":", 1)[-1]
        rectangle = _target_rectangle(item.get("payload") or {})
        if rectangle is not None:
            target_rectangles[action_id] = rectangle

    result = []
    for item in context.get("items") or ():
        if any((
            not isinstance(item, dict),
            item.get("kind") != "action_media",
            str(item.get("step_id") or "") != step_id,
        )):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        action_id = evidence_id.rsplit(":", 1)[-1]
        if action_id not in action_ids:
            continue
        payload = item.get("payload") or {}
        before_frame = payload.get("before") or {}
        after_frame = payload.get("after") or {}
        context_frame = payload.get("context") or {}
        before_media = _trusted_media_reference(
            session_dir,
            take_dir,
            before_frame,
        )
        after_media = _trusted_media_reference(
            session_dir,
            take_dir,
            after_frame,
        )
        context_media = _trusted_media_reference(
            session_dir,
            take_dir,
            context_frame,
        )
        result.append(QuestionMediaDTO(
            action_id=action_id,
            before_path=before_media[0] if before_media else None,
            after_path=after_media[0] if after_media else None,
            context_path=context_media[0] if context_media else None,
            before_highlight_box=_relative_highlight_box(
                target_rectangles.get(action_id),
                before_frame.get("monitor"),
            ),
            after_highlight_box=_relative_highlight_box(
                target_rectangles.get(action_id),
                after_frame.get("monitor"),
            ),
            context_highlight_box=_relative_highlight_box(
                target_rectangles.get(action_id),
                context_frame.get("monitor"),
            ),
            stability=str(
                (payload.get("stability") or {}).get("status") or ""
            ),
            outcome=str(
                (payload.get("outcome") or {}).get("result") or ""
            ),
            before_sha256=before_media[1] if before_media else None,
            before_size=before_media[2] if before_media else None,
            after_sha256=after_media[1] if after_media else None,
            after_size=after_media[2] if after_media else None,
            context_sha256=context_media[1] if context_media else None,
            context_size=context_media[2] if context_media else None,
        ))
    return tuple(result)


def project_decision_question_media(question, context, session_dir):
    """Project verified question media without persisting paths in Decisions."""
    question = dict(question or {})
    context = dict(context or {})
    session_dir = Path(session_dir).resolve()
    step_id = str(question.get("step_id") or "")
    action_ids = {
        str(item) for item in question.get("action_ids") or () if item
    }
    take_dir = _question_take_dir(session_dir, context, step_id)
    media = _question_media(question, context, session_dir)
    action_items, target_items, media_items = _question_media_items(
        context,
        step_id,
        action_ids,
    )
    frames = []
    overlays = []
    candidates = []
    statuses = []
    for entry in media:
        action_id = str(entry.action_id)
        media_item = media_items.get(action_id) or {}
        media_payload = media_item.get("payload") or {}
        action_item = action_items.get(action_id) or {}
        target_item = target_items.get(action_id) or {}
        target_payload = target_item.get("payload") or {}
        target_rectangle = _target_rectangle(target_payload)
        evidence_id = str(
            media_item.get("evidence_id")
            or f"media:{step_id}:{action_id}"
        )
        target_evidence_id = str(
            target_item.get("evidence_id")
            or f"target:{step_id}:{action_id}"
        )
        frames_by_role = (
            (
                "before",
                entry.before_path,
                entry.before_sha256,
                entry.before_size,
                entry.before_highlight_box,
                media_payload.get("before") or {},
            ),
            (
                "after",
                entry.after_path,
                entry.after_sha256,
                entry.after_size,
                entry.after_highlight_box,
                media_payload.get("after") or {},
            ),
            (
                "context",
                entry.context_path,
                entry.context_sha256,
                entry.context_size,
                entry.context_highlight_box,
                media_payload.get("context") or {},
            ),
        )
        for (
                frame_role,
                path,
                sha256,
                size,
                highlight_box,
                frame,
        ) in frames_by_role:
            reference, status = _trusted_media_reference(
                session_dir,
                take_dir,
                frame,
                include_status=True,
            )
            if frame.get("path"):
                statuses.append(status)
            if reference is None:
                continue
            path, sha256, size = reference
            frames.append({
                "frame_role": frame_role,
                "evidence_id": evidence_id,
                "action_id": action_id,
                "path": path,
                "sha256": sha256,
                "size": size,
                "monitor": dict(frame.get("monitor") or {}),
                "captured_ms": _optional_int(frame.get("captured_ms")),
                "stage": str(frame.get("stage") or ""),
                "source": str(frame.get("source") or ""),
                "verification_status": "verified",
            })
            if highlight_box is not None:
                overlays.append({
                    "kind": "observed_target",
                    "frame_role": frame_role,
                    "action_id": action_id,
                    "coordinates": list(highlight_box),
                    "coordinate_space": "frame",
                    "source_evidence_id": target_evidence_id,
                    "status": "verified",
                })
            click_point = _relative_click_point(
                action_item.get("payload") or {},
                target_rectangle,
                frame.get("monitor"),
            )
            if click_point is not None:
                overlays.append({
                    "kind": "click_point",
                    "frame_role": frame_role,
                    "action_id": action_id,
                    "coordinates": list(click_point),
                    "coordinate_space": "frame",
                    "coordinate_source": "control_relative_offset",
                    "source_evidence_id": str(
                        action_item.get("evidence_id")
                        or f"action:{step_id}:{action_id}"
                    ),
                    "status": "verified",
                })
        for candidate in target_payload.get("locator_candidates") or ():
            if not isinstance(candidate, dict):
                continue
            candidates.append({
                "action_id": action_id,
                "candidate_id": candidate.get("candidate_id"),
                "locator": dict(candidate.get("locator") or {}),
                "validation": dict(candidate.get("validation") or {}),
                "overlay_status": "coordinates_unavailable",
            })
    media_status = (
        "tampered"
        if "tampered" in statuses
        else "available" if frames else "unavailable"
    )
    reasons = []
    if not frames:
        reasons.append("no_trusted_action_media")
    if media_status == "tampered":
        reasons.append("media_integrity_mismatch")
    if frames and not any(
            item.get("kind") == "click_point" for item in overlays
    ):
        reasons.append("click_point_unavailable")
    if candidates:
        reasons.append("candidate_coordinates_unavailable")
    return {
        "question_id": str(question.get("question_id") or ""),
        "question_type": str(question.get("type") or ""),
        "step_id": step_id,
        "action_ids": sorted(action_ids),
        "evidence_ids": [
            str(item) for item in question.get("evidence_ids") or () if item
        ],
        "frames": frames,
        "overlays": overlays,
        "candidates": candidates,
        "degradation": {
            "media_status": media_status,
            "reasons": reasons,
        },
    }


def _question_media_items(context, step_id, action_ids):
    actions = {}
    targets = {}
    media = {}
    for item in context.get("items") or ():
        if any((
                not isinstance(item, dict),
                str(item.get("step_id") or "") != step_id,
        )):
            continue
        payload = item.get("payload") or {}
        action_id = str(
            payload.get("action_id")
            or str(item.get("evidence_id") or "").rsplit(":", 1)[-1]
        )
        if action_id not in action_ids:
            continue
        kind = str(item.get("kind") or "")
        if kind == "action":
            actions[action_id] = item
        elif kind == "target":
            targets[action_id] = item
        elif kind == "action_media":
            media[action_id] = item
    return actions, targets, media


def _question_take_dir(session_dir, context, step_id):
    take_path = next((
        str(item.get("take_path") or "")
        for item in context.get("source_graphs") or ()
        if isinstance(item, dict)
        and str(item.get("step_id") or "") == step_id
    ), "")
    if not take_path:
        return None
    try:
        take_dir = (Path(session_dir).resolve() / take_path).resolve()
        take_dir.relative_to(Path(session_dir).resolve())
    except ValueError:
        return None
    return take_dir


def _rectangle(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        rectangle = tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None
    return rectangle if rectangle[2] > rectangle[0] and rectangle[3] > rectangle[1] else None


def _target_rectangle(payload):
    if not isinstance(payload, dict):
        return None
    rectangle = _rectangle(payload.get("rectangle"))
    if rectangle is not None:
        return rectangle
    element = payload.get("element") or {}
    return _rectangle(element.get("rectangle"))


def _relative_click_point(action, rectangle, monitor):
    if rectangle is None or not isinstance(monitor, dict):
        return None
    parameters = action.get("parameters") or {}
    try:
        offset_x = int(parameters["offset_x"])
        offset_y = int(parameters["offset_y"])
        left = int(monitor.get("left") or 0)
        top = int(monitor.get("top") or 0)
        width = int(monitor.get("width") or 0)
        height = int(monitor.get("height") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    point = rectangle[0] + offset_x, rectangle[1] + offset_y
    relative = point[0] - left, point[1] - top
    if not (0 <= relative[0] < width and 0 <= relative[1] < height):
        return None
    return relative


def _relative_highlight_box(rectangle, monitor):
    if rectangle is None or not isinstance(monitor, dict):
        return None
    try:
        left = int(monitor.get("left") or 0)
        top = int(monitor.get("top") or 0)
        width = _optional_int(monitor.get("width"))
        height = _optional_int(monitor.get("height"))
    except (TypeError, ValueError):
        return None
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    relative = (
        rectangle[0] - left,
        rectangle[1] - top,
        rectangle[2] - left,
        rectangle[3] - top,
    )
    if any((
        relative[2] <= relative[0],
        relative[3] <= relative[1],
        width is not None and (
            relative[0] < 0 or relative[2] > width
        ),
        height is not None and (
            relative[1] < 0 or relative[3] > height
        ),
    )):
        return None
    return relative


def _trusted_media_reference(
        session_dir,
        take_dir,
        frame,
        *,
        include_status=False,
    ):
    def result(reference, status):
        return (reference, status) if include_status else reference

    if not isinstance(frame, dict):
        return result(None, "unavailable")
    value = str(frame.get("path") or "").strip()
    if not value:
        return result(None, "unavailable")
    path = (take_dir / value).resolve()
    project_root = Path(Paths.BASE_DIR).resolve()
    try:
        path.relative_to(take_dir)
        path.relative_to(session_dir)
        relative = path.relative_to(project_root)
    except ValueError:
        return result(None, "unavailable")
    expected_sha256 = str(frame.get("sha256") or "")
    expected_size = frame.get("size")
    if not path.is_file() or not expected_sha256 or expected_size is None:
        return result(None, "unavailable")
    try:
        content = path.read_bytes()
        if any((
            len(content) != int(expected_size),
            hashlib.sha256(content).hexdigest() != expected_sha256,
        )):
            return result(None, "tampered")
    except (OSError, TypeError, ValueError):
        return result(None, "unavailable")
    return result(
        (relative.as_posix(), expected_sha256, int(expected_size)),
        "verified",
    )


def _generation_result(report_path, report):
    validations = report.get("validations") or {}
    report_status = str(report.get("status") or "unknown")
    failed = [
        name
        for name, value in validations.items()
        if (value or {}).get("status") not in {"passed", "not_applicable"}
        and not (
            report_status == "running"
            and (value or {}).get("status") in {
                "pending",
                "not_started",
                "running",
            }
        )
    ]
    for name in (
        "generation_policy_audit",
        "pic_authorization_audit",
        "pic_usage_audit",
        "plan_conformance_audit",
        "evidence_audit",
    ):
        value = report.get(name) or {}
        status = value.get("status")
        if (
            status
            and status not in {"passed", "not_applicable"}
            and not (
                report_status == "running"
                and status in {"pending", "not_started", "running"}
            )
        ):
            failed.append(name)
    decision_coverage = (report.get("evidence_audit") or {}).get(
        "decision_coverage"
    )
    category, action, label = _result_recovery(
        str(report.get("status") or "unknown"),
        failed,
    )
    implementation = report.get("implementation_summary") or {}
    return GenerationResultDTO(
        status=report_status,
        transaction_id=report.get("transaction_id"),
        report_path=str(report_path),
        changed_files=tuple(
            str(item) for item in report.get("changed_files") or ()
        ),
        validation_count=len(validations),
        failed_checks=tuple(dict.fromkeys(failed)),
        errors=tuple(str(item) for item in report.get("errors") or ()),
        warnings=tuple(str(item) for item in report.get("warnings") or ()),
        plan_conformance_status=(
            (report.get("plan_conformance_audit") or {}).get("status")
        ),
        evidence_decision_coverage=(
            float(decision_coverage)
            if decision_coverage is not None
            else None
        ),
        failure_category=category,
        recommended_action=action,
        recommended_label=label,
        implementation=(
            ImplementationSummaryDTO(
                reuse=int(implementation.get("reuse") or 0),
                modify=int(implementation.get("modify") or 0),
                create=int(implementation.get("create") or 0),
            )
            if implementation
            else None
        ),
        stages=_generation_stages(report, report_path=report_path),
        execution_status=(
            (report.get("execution_outcome") or {}).get("status")
        ),
        workspace_materialization=_workspace_materialization(report_path, report),
    )


def _workspace_materialization(report_path, report):
    snapshot = report.get("implementation_snapshot")
    if snapshot is None:
        return WorkspaceMaterializationDTO(status="not_recorded")
    if not isinstance(snapshot, list):
        return WorkspaceMaterializationDTO(status="report_unavailable")
    project_root = Path(report.get("project_root") or Paths.BASE_DIR).resolve()
    missing = []
    modified = []
    extra = []
    checked = 0
    for item in snapshot:
        if not isinstance(item, dict):
            return WorkspaceMaterializationDTO(status="report_unavailable")
        relative = str(item.get("path") or "")
        if not relative:
            return WorkspaceMaterializationDTO(status="report_unavailable")
        path = (project_root / relative).resolve()
        try:
            path.relative_to(project_root)
        except ValueError:
            return WorkspaceMaterializationDTO(status="report_unavailable")
        expected_exists = item.get("exists") is True
        actual_exists = path.is_file()
        if not expected_exists:
            if actual_exists:
                extra.append(relative)
            checked += 1
            continue
        if not actual_exists:
            missing.append(relative)
            checked += 1
            continue
        try:
            content = path.read_bytes()
        except OSError:
            missing.append(relative)
            checked += 1
            continue
        expected_size = item.get("size")
        expected_sha256 = str(item.get("sha256") or "")
        if (
            expected_size is None
            or not expected_sha256
            or len(content) != int(expected_size)
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            modified.append(relative)
        checked += 1
    if missing and modified:
        status = "mixed_mismatch"
    elif missing:
        status = "missing_files"
    elif modified:
        status = "modified_files"
    elif extra:
        status = "extra_generation_files"
    else:
        status = "matches_report"
    return WorkspaceMaterializationDTO(
        status=status,
        expected_count=len(snapshot),
        checked_count=checked,
        missing_files=tuple(missing),
        modified_files=tuple(modified),
        extra_generation_files=tuple(extra),
    )


def _job_result_transaction(session_dir, job_result):
    transaction_stage = (job_result.get("stages") or {}).get(
        "transaction"
    ) or {}
    owner = transaction_stage.get("owner") or {}
    if not isinstance(owner, dict) or owner.get(
            "owner"
    ) != "generation_transaction":
        return None
    return _load_job_transaction_owner(session_dir, owner)


def _job_execution_transaction(session_dir, workflow):
    owner = (workflow.get("job_execution") or {}).get(
        "transaction"
    ) or {}
    if not isinstance(owner, dict):
        return None
    return _load_job_transaction_owner(session_dir, owner)


def _load_job_transaction_owner(session_dir, owner):
    path_value = owner.get("path")
    transaction_id = str(owner.get("transaction_id") or "")
    if not path_value or not transaction_id:
        return None
    session_dir = Path(session_dir).resolve()
    path = Path(str(path_value))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    loaded = load_transaction_report(path, session_dir)
    if loaded is None:
        return None
    report_path, report = loaded
    result_fingerprint = owner.get("result_fingerprint")
    if any((
        report.get("transaction_id") != transaction_id,
        owner.get("status")
        and report.get("status") != owner.get("status"),
        result_fingerprint
        and report.get("result_fingerprint") != result_fingerprint,
        result_fingerprint
        and transaction_result_fingerprint(report) != result_fingerprint,
        owner.get("completion_fingerprint")
        and report.get("completion_fingerprint")
        != owner.get("completion_fingerprint"),
    )):
        return None
    return report_path, report


def _generation_result_from_job_result(
        session_dir,
        pointer,
        job_result,
    ):
    session_dir = Path(session_dir).resolve()
    path = Path(str(pointer.get("path") or ""))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    status = str(job_result.get("status") or "failed")
    category = str(job_result.get("category") or "job_failed")
    return GenerationResultDTO(
        status=status,
        transaction_id=None,
        report_path=str(path),
        changed_files=(),
        validation_count=0,
        failed_checks=(),
        errors=((category,) if status == "failed" else ()),
        warnings=(),
        failure_category=category,
        recommended_action=str(
            job_result.get("next_action") or "review_generation_failure"
        ),
        recommended_label=_job_result_label(status, category),
        stages=_job_result_stages(job_result),
        execution_status=None,
    )


def _generation_stages(report, *, report_path=None):
    status = str(report.get("status") or "unknown")
    manifest = report.get("implementation_manifest") or {}
    timings = _stage_timings(report)
    validation_pointer = report.get("implementation_validation_ledger") or {}
    ledger = None
    if validation_pointer and report_path is not None:
        ledger, _ledger_errors = verify_validation_ledger(
            validation_pointer.get("path") or "",
            transaction_id=report.get("transaction_id"),
            manifest_fingerprint=manifest.get(
                "implementation_manifest_fingerprint"
            ),
        )
        if ledger is not None and any((
            validation_pointer.get("head_fingerprint")
            != ledger.get("head_fingerprint"),
            validation_pointer.get("fingerprint")
            != ledger.get("fingerprint"),
            validation_pointer.get("attempt_count")
            != len(ledger.get("attempts") or ()),
        )):
            ledger = None
    latest = (
        (ledger.get("attempts") or [])[-1]
        if ledger and ledger.get("attempts")
        else {}
    )
    latest_status = str(latest.get("status") or "")
    implementation_status = (
        "passed"
        if latest_status == "valid"
        else "failed"
        if latest_status == "invalid"
        else "not_evaluated"
        if status in {"completed", "completed_no_changes"}
        else "pending"
        if status == "running"
        else "failed"
    )
    transaction_status = _stage_transaction_status(report)
    return GenerationStageSummaryDTO(
        semantic_selection=StageOutcomeDTO(
            status="not_evaluated",
            source="independent_oracle_required",
            detail="Design acceptance does not prove semantic correctness.",
            **timings.get("semantic_selection", {}),
        ),
        design=StageOutcomeDTO(
            status=(
                "passed"
                if implementation_manifest_identity_is_valid(manifest)
                and manifest.get("status") == "ready"
                else "not_evaluated"
                if not manifest
                else "failed"
            ),
            source="implementation_manifest",
            detail="Validated Design compiled to the bound Plan and Manifest.",
            **timings.get("design", {}),
        ),
        implementation=StageOutcomeDTO(
            status=implementation_status,
            source=(
                "implementation_validation_ledger"
                if latest_status
                else "implementation_validation_missing"
            ),
            detail=(
                str(validation_pointer.get("head_fingerprint") or "")
                if latest_status
                else ""
            ),
            **timings.get("implementation", {}),
        ),
        transaction=StageOutcomeDTO(
            status=transaction_status,
            source="generation_transaction",
            detail=status,
            **timings.get("transaction", {}),
        ),
        runtime=StageOutcomeDTO(
            status="pending",
            source="bound_run_result",
            detail="No bound runtime result was projected with this report.",
            **timings.get("runtime", {}),
        ),
        oracle=StageOutcomeDTO(
            status="not_evaluated",
            source="independent_business_oracle",
            detail="Runtime assertions are not an independent business oracle.",
            **timings.get("oracle", {}),
        ),
    )


def _with_job_result(result, job_result):
    category = str(job_result.get("category") or "")
    status = str(job_result.get("status") or result.status)
    return replace(
        result,
        status=status,
        failure_category=category or None,
        recommended_action=str(
            job_result.get("next_action") or result.recommended_action
        ),
        recommended_label=_job_result_label(status, category),
        stages=_job_result_stages(job_result),
    )


def _job_result_stages(job_result):
    stages = job_result.get("stages") or {}

    def outcome(name):
        value = stages.get(name) or {}
        owner = value.get("owner")
        if isinstance(owner, dict):
            source = str(owner.get("type") or owner.get("owner") or name)
            detail = str(
                owner.get("fingerprint")
                or owner.get("result_fingerprint")
                or owner.get("run_result_id")
                or owner.get("transaction_id")
                or ""
            )
        else:
            source = str(owner or "generation_job_result")
            detail = ""
        return StageOutcomeDTO(
            status=str(value.get("status") or "not_evaluated"),
            source=source,
            detail=detail,
        )

    return GenerationStageSummaryDTO(
        semantic_selection=outcome("semantic_selection"),
        design=outcome("design"),
        implementation=outcome("implementation"),
        transaction=outcome("transaction"),
        runtime=outcome("runtime"),
        oracle=outcome("oracle"),
    )


def _job_result_label(status, category):
    if status == "completed":
        return (
            "真实运行已验证"
            if category == "runtime_validated"
            else "代码已生成，存在待处理项"
            if category == "generated_with_issues"
            else "静态生成已完成"
        )
    return {
        "aborted": "生成已停止",
        "runtime_failed": "真实运行未通过",
        "oracle_failed": "独立业务验证未通过",
        "stale_during_generation": "生成依据已变化",
    }.get(category, "查看生成失败")


def _stage_timings(report):
    ledger = report.get("stage_timing_ledger") or {}
    if not isinstance(ledger, dict):
        return {}
    stages = ledger.get("stages") or {}
    if not isinstance(stages, dict):
        return {}
    values = {}
    for name, timing in stages.items():
        if not isinstance(timing, dict):
            continue
        projected = {}
        if timing.get("started_at"):
            projected["started_at"] = str(timing.get("started_at"))
        if timing.get("finished_at"):
            projected["finished_at"] = str(timing.get("finished_at"))
        duration_ms = _optional_int(timing.get("duration_ms"))
        if duration_ms is not None:
            projected["duration_ms"] = duration_ms
        if projected:
            values[str(name)] = projected
    return values


def _stage_transaction_status(report):
    status = str(report.get("status") or "unknown")
    if status == "running":
        return "pending"
    if status not in {"completed", "completed_no_changes"}:
        return "failed"
    if report.get("errors"):
        return "failed"
    validations = report.get("validations") or {}
    for value in validations.values():
        check_status = (value or {}).get("status")
        if check_status == "failed":
            return "failed"
        if check_status not in {
            "passed",
            "not_applicable",
            "pending",
            "not_started",
            "running",
        }:
            return "failed"
    for name in report.get("required_validations") or ():
        check_status = (validations.get(name) or {}).get("status")
        if check_status == "failed":
            return "failed"
        if check_status != "passed":
            return "not_evaluated"
    for field in (
        "generation_policy_audit",
        "plan_conformance_audit",
        "evidence_audit",
        "lease_revision_audit",
    ):
        audit_status = (report.get(field) or {}).get("status")
        if audit_status == "failed":
            return "failed"
        if audit_status not in {"passed", "not_applicable"}:
            return "not_evaluated"
    if (
        (report.get("implementation_manifest") or {}).get(
            "implementation_manifest_version"
        ) == IMPLEMENTATION_MANIFEST_VERSION
        and (report.get("terminal_snapshot_audit") or {}).get("status")
        != "passed"
    ):
        return (
            "failed"
            if (report.get("terminal_snapshot_audit") or {}).get("status")
            == "failed"
            else "not_evaluated"
        )
    return "passed"


def _with_runtime_stages(result, runtime_result, *, quality=None):
    if result is None or result.stages is None:
        return result
    runtime_status = (
        "passed"
        if quality is not None and _single_run_passed(quality) is True
        else "failed"
        if quality is not None
        and _single_run_passed(quality) is False
        else "passed"
        if quality is None
        and runtime_result is not None
        and runtime_result.status == "passed"
        else "failed"
        if runtime_result is not None and runtime_result.status == "failed"
        else "pending"
    )
    return replace(
        result,
        stages=replace(
            result.stages,
            runtime=StageOutcomeDTO(
                status=runtime_status,
                source="bound_run_result",
                detail=(
                    runtime_result.run_result_id
                    if runtime_result is not None
                    else ""
                ),
                started_at=result.stages.runtime.started_at,
                finished_at=result.stages.runtime.finished_at,
                duration_ms=result.stages.runtime.duration_ms,
            ),
            oracle=StageOutcomeDTO(
                status=(
                    "passed"
                    if quality is not None
                    and _oracle_passed(quality) is True
                    else "failed"
                    if quality is not None
                    and quality.get("runtime_matrix_required") is True
                    and _oracle_passed(quality) is False
                    else "not_required"
                    if quality is not None
                    and quality.get("runtime_matrix_required") is False
                    else "not_evaluated"
                ),
                source="independent_business_oracle",
                detail="",
                started_at=result.stages.oracle.started_at,
                finished_at=result.stages.oracle.finished_at,
                duration_ms=result.stages.oracle.duration_ms,
            ),
        ),
    )


def _single_run_passed(quality):
    if not isinstance(quality, dict):
        return None
    return quality.get("single_run_passed")


def _oracle_passed(quality):
    if not isinstance(quality, dict):
        return None
    return quality.get("oracle_passed")


def _result_recovery(status, failed_checks):
    failed = set(failed_checks)
    if status in {"completed", "completed_no_changes"} and not failed:
        return None, "review_result", "查看生成结果"
    if status == "running":
        return None, "wait", "生成进行中"
    if failed & {"python_compile", "locator_compile", "step_scope"}:
        return (
            "generated_files",
            "review_generated_files",
            "检查生成文件",
        )
    if "plan_conformance_audit" in failed:
        return (
            "plan_conformance",
            "regenerate_from_plan",
            "按计划重新生成",
        )
    if "evidence_audit" in failed:
        return (
            "evidence",
            "repair_evidence_trace",
            "修复证据引用",
        )
    if failed & {
        "generation_policy_audit",
        "pic_authorization_audit",
        "pic_usage_audit",
    }:
        return "policy", "review_policy", "处理策略阻塞"
    return "transaction", "review_transaction", "查看失败报告"


def _running_transaction_for_scope(session_dir, step_ids):
    expected = tuple(sorted({str(step_id) for step_id in step_ids}))
    root = (
        Path(session_dir) / "ai" / "generation-transactions"
    ).resolve()
    matches = []
    for candidate in root.glob("transaction-*/report.json"):
        loaded = load_transaction_report(candidate, session_dir)
        if loaded is None:
            continue
        path, report = loaded
        if report.get("status") != "running":
            continue
        actual = tuple(sorted({
            str(step.get("id"))
            for step in (report.get("target") or {}).get("steps") or ()
            if step.get("id")
        }))
        if actual == expected:
            matches.append((str(report.get("started_at") or ""), path, report))
    if not matches:
        return None
    _started_at, path, report = max(matches, key=lambda item: item[0])
    return path, report
