from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IssueDTO:
    code: str
    message: str
    blocking: bool
    title: str = ""
    detail: str = ""
    repair: str | None = None
    location: str | None = None
    evidence: object | None = None
    event_ids: tuple[str, ...] = ()
    evidence_path: str | None = None


@dataclass(frozen=True)
class EvidenceSummaryDTO:
    linked_event_count: int
    event_count: int
    complete_action_count: int
    action_count: int
    artifact_count: int


@dataclass(frozen=True)
class ObservationDTO:
    annotation_id: str
    event_id: str
    action_id: str | None
    action_ordinal: int | None
    take_id: str
    owner_take_id: str
    revision: int
    authority: str
    focus: str
    relation: str
    property_name: str | None
    expected_source_kind: str
    expected_source_reference: str | None
    business_meaning: str
    target_name: str
    target_control_type: str
    summary: str
    needs_business_confirmation: bool
    reference_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class TakeDTO:
    take_id: str
    take_number: int
    status: str
    path: str
    directory_path: str | None
    review_text: str
    action_count: int
    event_count: int
    window_count: int
    selected: bool
    timeline_revision: str | None = None
    evidence_summary: EvidenceSummaryDTO | None = None


@dataclass(frozen=True)
class PortabilityActivityDTO:
    operation_id: str
    kind: str
    status: str
    started_at: str
    finished_at: str | None
    package_name: str
    package_path: str
    run_count: int
    ready_count: int
    package_sha256: str | None
    error: str | None


@dataclass(frozen=True)
class PortabilityOverviewDTO:
    active: bool
    active_kind: str | None
    last_export: PortabilityActivityDTO | None
    last_import: PortabilityActivityDTO | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LibraryRunDTO:
    session_id: str
    feature_name: str
    scenario_name: str
    progress: str
    next_action: str
    updated_at: str
    path: str
    directory_path: str | None
    search_text: str


@dataclass(frozen=True)
class LibraryCapabilityDTO:
    capability_id: str
    status: str
    status_label: str
    feature_name: str
    scenario_name: str
    step_text: str
    published_at: str
    path: str
    detail_path: str | None
    search_text: str


@dataclass(frozen=True)
class LibraryOverviewDTO:
    output_root: str
    runs: tuple[LibraryRunDTO, ...]
    capabilities: tuple[LibraryCapabilityDTO, ...]


@dataclass(frozen=True)
class FeatureScenarioDTO:
    scenario_id: str
    name: str
    example_id: str | None
    recording_state: str
    recording_label: str
    recorded_step_count: int
    total_step_count: int
    exportable: bool
    session_id: str | None
    updated_at: str | None
    export_session_id: str | None
    export_updated_at: str | None
    issue: str | None = None


@dataclass(frozen=True)
class FeatureWorkspaceDTO:
    feature_id: str
    name: str
    source_path: str
    source_relpath: str
    source_hash: str
    recording_label: str
    recorded_scenario_count: int
    scenario_count: int
    exportable_recording_count: int
    outdated_recording_count: int
    last_recording_at: str | None
    last_export_at: str | None
    export_label: str
    export_outdated: bool
    scenarios: tuple[FeatureScenarioDTO, ...]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureDirectoryDTO:
    feature_root: str
    recording_root: str
    features: tuple[FeatureWorkspaceDTO, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetirementStatusDTO:
    eligible: bool
    detail: str


@dataclass(frozen=True)
class UserTaskProjection:
    owner: str
    requires_user_action: bool
    action: str
    action_label: str
    target_view: str
    reason: str
    target_step_id: str | None
    owner_take_id: str | None


@dataclass(frozen=True)
class StepWorkspaceDTO:
    step_id: str
    ordinal: int
    keyword: str
    text: str
    capture_status: str
    evidence_status: str
    generation_status: str
    selected_take_id: str | None
    timeline_revision: str | None
    next_action: str
    next_action_label: str
    next_action_detail: str
    takes: tuple[TakeDTO, ...]
    issues: tuple[IssueDTO, ...]
    observations: tuple[ObservationDTO, ...] = ()


@dataclass(frozen=True)
class ScenarioScopeDTO:
    kind: str
    complete: bool
    selected_step_ids: tuple[str, ...]
    excluded_step_ids: tuple[str, ...]
    selected_count: int
    total_count: int
    label: str
    capture_generation_candidate: bool
    incomplete_step_ids: tuple[str, ...]


@dataclass(frozen=True)
class DecisionOptionDTO:
    option_id: str
    label: str
    effect: str


@dataclass(frozen=True)
class QuestionMediaDTO:
    action_id: str
    before_path: str | None
    after_path: str | None
    context_path: str | None
    before_highlight_box: tuple[int, int, int, int] | None
    after_highlight_box: tuple[int, int, int, int] | None
    context_highlight_box: tuple[int, int, int, int] | None
    stability: str
    outcome: str
    before_sha256: str | None = None
    before_size: int | None = None
    after_sha256: str | None = None
    after_size: int | None = None
    context_sha256: str | None = None
    context_size: int | None = None


@dataclass(frozen=True)
class DecisionQuestionDTO:
    question_id: str
    question_type: str
    title: str
    prompt: str
    step_id: str
    step_text: str
    observed: str
    uncertainty: str
    options: tuple[DecisionOptionDTO, ...]
    action_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    media: tuple[QuestionMediaDTO, ...]
    blocking: bool = True


@dataclass(frozen=True)
class DecisionSummaryDTO:
    status: str
    question_count: int
    blocking_count: int
    forensic_blocking_count: int
    pack_path: str | None = None
    questions: tuple[DecisionQuestionDTO, ...] = ()


@dataclass(frozen=True)
class ImplementationSummaryDTO:
    reuse: int = 0
    modify: int = 0
    create: int = 0


@dataclass(frozen=True)
class StageOutcomeDTO:
    status: str
    source: str
    detail: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class GenerationStageSummaryDTO:
    semantic_selection: StageOutcomeDTO
    design: StageOutcomeDTO
    implementation: StageOutcomeDTO
    transaction: StageOutcomeDTO
    runtime: StageOutcomeDTO
    oracle: StageOutcomeDTO


@dataclass(frozen=True)
class WorkspaceMaterializationDTO:
    status: str
    expected_count: int = 0
    checked_count: int = 0
    missing_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    extra_generation_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class GenerationResultDTO:
    status: str
    transaction_id: str | None
    report_path: str
    changed_files: tuple[str, ...]
    validation_count: int
    failed_checks: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    plan_conformance_status: str | None = None
    evidence_decision_coverage: float | None = None
    failure_category: str | None = None
    recommended_action: str = "review_result"
    recommended_label: str = "查看生成结果"
    implementation: ImplementationSummaryDTO | None = None
    stages: GenerationStageSummaryDTO | None = None
    execution_status: str | None = None
    workspace_materialization: WorkspaceMaterializationDTO | None = None


@dataclass(frozen=True)
class WindowOwnershipSummaryDTO:
    reuse_existing: int = 0
    create_new: int = 0
    ambiguous: int = 0
    unresolved: int = 0


@dataclass(frozen=True)
class RuntimeDiagnosticDTO:
    code: str
    category: str
    stage: str
    summary: str
    backend: str | None = None
    locator_name: str | None = None
    root_name: str | None = None
    wait_type: str | None = None
    timeout_seconds: float | None = None
    probe_count: int | None = None
    candidate_count: int | None = None
    cause_type: str | None = None
    cause_message: str | None = None


@dataclass(frozen=True)
class RuntimeResultDTO:
    status: str
    run_result_id: str
    result_path: str
    report_path: str | None
    failed_step_count: int
    attachment_count: int
    first_error: str | None = None
    first_attachment_path: str | None = None
    first_diagnostic: RuntimeDiagnosticDTO | None = None
    report_sha256: str | None = None
    report_size: int | None = None
    first_attachment_sha256: str | None = None
    first_attachment_size: int | None = None


@dataclass(frozen=True)
class VerificationSummaryDTO:
    level: str
    label: str
    detail: str
    code_generated: bool
    implementation_validated: bool
    runtime_verified: bool


@dataclass(frozen=True)
class GenerationJobHistoryDTO:
    job_id: str
    job_path: str
    status: str
    phase: str | None
    reason: str | None
    profile_id: str | None
    retired_at: str | None
    result_status: str | None
    result_path: str | None
    is_current: bool = False


@dataclass(frozen=True)
class FeedbackHistoryDTO:
    memory_id: str
    status: str
    tier: str | None
    claim: str
    created_at: str | None
    transaction_id: str | None
    supersedes: tuple[str, ...]
    is_effective: bool


@dataclass(frozen=True)
class GenerationSummaryDTO:
    workflow_status: str
    display_status: str
    next_action: str
    request_id: str | None
    request_path: str | None
    risk_mode: str | None
    decision: DecisionSummaryDTO
    result: GenerationResultDTO | None
    required_forensic_evidence: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    window_ownership: WindowOwnershipSummaryDTO | None = None
    pending_ai_ambiguities: int = 0
    pending_user_ambiguities: int = 0
    pending_evidence_ambiguities: int = 0
    recommended_action: str = "pending"
    recommended_label: str = "正在准备"
    recommended_detail: str = "证据与生成状态正在更新。"
    brief_size_bytes: int = 0
    verification: VerificationSummaryDTO | None = None
    job_id: str | None = None
    job_phase: str | None = None
    generation_profile_id: str | None = None
    job_history: tuple[GenerationJobHistoryDTO, ...] = ()
    feedback_history: tuple[FeedbackHistoryDTO, ...] = ()


@dataclass(frozen=True)
class WorkbenchDTO:
    run_id: str
    feature_name: str
    scenario_name: str
    run_status: str
    selected_step_id: str | None
    steps: tuple[StepWorkspaceDTO, ...]
    user_task: UserTaskProjection
    scope: ScenarioScopeDTO | None = None
    generation: GenerationSummaryDTO | None = None
    runtime_result: RuntimeResultDTO | None = None
    active_operation_count: int = 0
