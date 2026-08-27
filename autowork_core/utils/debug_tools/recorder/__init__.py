__all__ = [
	"FeatureRecordingSession",
	"RecordingSessionConfig",
	"build_generation_request",
	"extract_video_frame",
	"load_feature_plan",
	"validate_ai_bundle",
	"load_recording_catalog",
	"load_capability_catalog",
	"search_capabilities",
	"write_request_memory_context",
	"record_transaction_feedback",
	"load_memory_events",
	"search_memory_events",
	"inspect_knowledge_store",
	"inspect_run_retirement",
	"retire_recording_session",
	"export_recording_package",
	"import_recording_package",
	"export_feature_delivery",
	"export_feature_deliveries",
	"preview_feature_delivery",
	"import_feature_delivery",
	"build_evidence_graph",
	"build_evidence_context",
	"load_evidence_context",
	"build_generation_brief",
	"inspect_workflow",
]


def __getattr__(name):
	if name == "load_feature_plan":
		from autowork_core.utils.debug_tools.recorder.feature_plan import load_feature_plan

		return load_feature_plan
	if name in ("FeatureRecordingSession", "RecordingSessionConfig"):
		from autowork_core.utils.debug_tools.recorder.session import (
			FeatureRecordingSession,
			RecordingSessionConfig,
		)

		return {
			"FeatureRecordingSession": FeatureRecordingSession,
			"RecordingSessionConfig": RecordingSessionConfig,
		}[name]
	if name == "build_generation_request":
		from autowork_core.utils.debug_tools.recorder.generation_request import (
			build_generation_request,
		)

		return build_generation_request
	if name == "extract_video_frame":
		from autowork_core.utils.debug_tools.recorder.media import extract_video_frame

		return extract_video_frame
	if name == "build_evidence_graph":
		from autowork_core.utils.debug_tools.recorder.evidence_graph import (
			build_evidence_graph,
		)

		return build_evidence_graph
	if name in ("build_evidence_context", "load_evidence_context"):
		from autowork_core.utils.debug_tools.recorder.evidence_context import (
			build_evidence_context,
			load_evidence_context,
		)

		return {
			"build_evidence_context": build_evidence_context,
			"load_evidence_context": load_evidence_context,
		}[name]
	if name == "build_generation_brief":
		from autowork_core.utils.debug_tools.recorder.semantic_reconciler import (
			build_generation_brief,
		)

		return build_generation_brief
	if name == "inspect_workflow":
		from autowork_core.utils.debug_tools.recorder.workflow_service import (
			inspect_workflow,
		)

		return inspect_workflow
	if name == "validate_ai_bundle":
		from autowork_core.utils.debug_tools.recorder.bundle_validator import validate_ai_bundle

		return validate_ai_bundle
	if name == "load_recording_catalog":
		from autowork_core.utils.debug_tools.recorder.catalog import load_recording_catalog

		return load_recording_catalog
	if name in ("load_capability_catalog", "search_capabilities"):
		from autowork_core.utils.debug_tools.recorder.capability import (
			load_capability_catalog,
			search_capabilities,
		)

		return {
			"load_capability_catalog": load_capability_catalog,
			"search_capabilities": search_capabilities,
		}[name]
	if name in (
		"write_request_memory_context",
		"record_transaction_feedback",
		"load_memory_events",
		"search_memory_events",
	):
		from autowork_core.utils.debug_tools.recorder.project_memory import (
			load_memory_events,
			record_transaction_feedback,
			search_memory_events,
			write_request_memory_context,
		)

		return {
			"write_request_memory_context": write_request_memory_context,
			"record_transaction_feedback": record_transaction_feedback,
			"load_memory_events": load_memory_events,
			"search_memory_events": search_memory_events,
		}[name]
	if name == "inspect_knowledge_store":
		from autowork_core.utils.debug_tools.recorder.knowledge_audit import (
			inspect_knowledge_store,
		)

		return inspect_knowledge_store
	if name in ("inspect_run_retirement", "retire_recording_session"):
		from autowork_core.utils.debug_tools.recorder.run_retirement import (
			inspect_run_retirement,
			retire_recording_session,
		)

		return {
			"inspect_run_retirement": inspect_run_retirement,
			"retire_recording_session": retire_recording_session,
		}[name]
	if name in ("export_recording_package", "import_recording_package"):
		from autowork_core.utils.debug_tools.recorder.recording_portability import (
			export_recording_package,
			import_recording_package,
		)

		return {
			"export_recording_package": export_recording_package,
			"import_recording_package": import_recording_package,
		}[name]
	if name in (
		"export_feature_delivery",
		"export_feature_deliveries",
		"preview_feature_delivery",
		"import_feature_delivery",
	):
		from autowork_core.utils.debug_tools.recorder.feature_delivery import (
			export_feature_deliveries,
			export_feature_delivery,
			import_feature_delivery,
			preview_feature_delivery,
		)

		return {
			"export_feature_delivery": export_feature_delivery,
			"export_feature_deliveries": export_feature_deliveries,
			"preview_feature_delivery": preview_feature_delivery,
			"import_feature_delivery": import_feature_delivery,
		}[name]
	raise AttributeError(name)