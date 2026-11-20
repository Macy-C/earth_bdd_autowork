from pathlib import Path

from autowork_core.knowledge.case_context import scan_captures
from autowork_core.knowledge.weknora_client import PublishError


def resolve_case_request(
    *,
    knowledge_base=None,
    markdown_path=None,
    capture_dir=None,
    query=None,
    feature_source=None,
    scenario_name=None,
    original_upload=False,
):
    """Resolve the user's local input only; remote history never changes upload intent."""
    if markdown_path and any(value is not None for value in (capture_dir, query, feature_source, scenario_name)):
        raise PublishError("ambiguous_input", "Provide either one Markdown artifact or a capture selection, not both", stage="resolve")
    if original_upload and not markdown_path:
        raise PublishError("invalid_input", "Original upload requires one explicit Markdown artifact", stage="resolve")
    if markdown_path:
        source = Path(markdown_path).resolve()
        if not source.is_file():
            raise PublishError("invalid_input", "The selected Markdown file does not exist", stage="resolve")
        return {
            "status": "resolved",
            "mode": "explicit_file",
            "cases": [{
                "source_path": str(source),
                "input_type": "markdown",
            }],
        }
    selection = scan_captures(capture_dir, query=query, feature_source=feature_source, scenario_name=scenario_name)
    if not selection["selected_cases"]:
        return {"status": "blocked", "code": "no_publishable_capture", "stage": "resolve", "cases": []}
    return {"status": "resolved", "mode": "capture", "cases": selection["selected_cases"], "selection": selection}
