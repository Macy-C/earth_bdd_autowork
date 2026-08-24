from __future__ import annotations

import hashlib
import json
from pathlib import Path

from behave.parser import parse_file
from behave.model import ScenarioOutline

from autowork_core.runtime.step_scope import (
    resolved_step_scope_for_scenario,
)
from autowork_core.runtime.tag_manager import STEP_SCOPE_PREFIXES, normalize_tag
from autowork_core.utils.debug_tools.recorder.feature_plan import (
    load_feature_plan,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    persistent_feature_id,
)
from autowork_core.utils.debug_tools.recorder.models import public_dict
from config.paths import Paths


SCOPE_BINDING_VERSION = "1.0"
BUSINESS_PROJECTION_VERSION = "1.0"


class ScopeBindingError(ValueError):
    pass


def bind_recording_step_scope(session_dir, *, project_root=None):
    session_dir = Path(session_dir).resolve()
    manifest = _read_json(session_dir / "manifest.json")
    recorded_feature = manifest.get("feature") or {}
    recorded_scenario = manifest.get("scenario") or {}
    recorded_source_sha256, recorded_persistent_id = (
        _validate_recorded_source_snapshot(
        session_dir,
        manifest,
        recorded_feature,
        recorded_scenario,
        )
    )
    source_relpath = str(recorded_feature.get("source_relpath") or "")
    if not source_relpath:
        raise ScopeBindingError("Recording Session 缺少 Feature source_relpath")
    source_value = Path(source_relpath)
    project_root = _resolve_project_root(
        session_dir,
        manifest,
        source_value,
        explicit=project_root,
    )
    recorded_source_path = (
        source_value.resolve()
        if source_value.is_absolute()
        else (project_root / source_value).resolve()
    )
    try:
        recorded_source_path.relative_to(project_root)
    except ValueError as exc:
        raise ScopeBindingError(
            f"Feature source path 越出项目目录: {source_relpath}"
        ) from exc
    source_path, current_feature_plan = _resolve_current_feature(
        recorded_source_path,
        project_root,
        recorded_feature,
        recorded_persistent_id=recorded_persistent_id,
    )
    current_source_relpath = source_path.relative_to(project_root).as_posix()
    current_scenario_plan = _matching_scenario(
        current_feature_plan.scenarios,
        recorded_scenario,
    )
    recorded_projection = recording_business_projection(
        recorded_feature,
        recorded_scenario,
    )
    current_projection = recording_business_projection(
        public_dict(current_feature_plan),
        public_dict(current_scenario_plan),
    )
    recorded_fingerprint = _fingerprint(recorded_projection)
    current_fingerprint = _fingerprint(current_projection)
    if current_fingerprint != recorded_fingerprint:
        raise ScopeBindingError(
            "当前 Feature 的 Scenario/Background/Examples 业务结构已变化；"
            "不能仅重新绑定 Step scope，请刷新录制计划并补录受影响 Step"
        )

    parsed_feature = parse_file(str(source_path))
    parsed_scenarios = list(parsed_feature.walk_scenarios())
    planned_scenarios = list(current_feature_plan.scenarios)
    scenario_index = planned_scenarios.index(current_scenario_plan)
    if scenario_index >= len(parsed_scenarios):
        raise ScopeBindingError("当前 Feature 的 Scenario 展开结果不一致")
    resolved = resolved_step_scope_for_scenario(
        parsed_feature,
        parsed_scenarios[scenario_index],
        project_root / "Bdd" / "steps",
        require_files=False,
    )
    resolved_scope = _workspace_scope(resolved.public_dict())
    resolved_scope["step_behavior_files"] = _step_behavior_files(
        parsed_feature,
        parsed_scenarios[scenario_index],
        recorded_scenario,
        resolved,
    )
    binding = {
        "scope_binding_version": SCOPE_BINDING_VERSION,
        "source_relpath": current_source_relpath,
        "current_source_sha256": hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest(),
        "recorded_source_sha256": recorded_source_sha256,
        "business_fingerprint": current_fingerprint,
        "scenario_id": str(recorded_scenario.get("id") or ""),
        "logical_template_id": str(
            recorded_scenario.get("logical_template_id") or ""
        ),
        "example_id": recorded_scenario.get("example_id"),
        "resolved_step_scope": resolved_scope,
    }
    binding["binding_fingerprint"] = _scope_binding_fingerprint(binding)
    return binding


def _resolve_current_feature(
        recorded_path,
        project_root,
        recorded_feature,
        *,
        recorded_persistent_id,
    ):
    recorded_id = str(recorded_feature.get("id") or "")
    if recorded_path.is_file():
        current = load_feature_plan(recorded_path)
        if (
                recorded_persistent_id is None
                or not recorded_id
                or current.id == recorded_id
        ):
            return recorded_path, current

    candidates = []
    feature_root = project_root / "Bdd"
    if recorded_id and feature_root.is_dir():
        for candidate in sorted(feature_root.rglob("*.feature")):
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if not _is_relative_to(resolved, feature_root):
                continue
            try:
                source_text = resolved.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            if persistent_feature_id(source_text) == recorded_id:
                candidates.append(resolved)
    if len(candidates) > 1:
        paths = ", ".join(
            path.relative_to(project_root).as_posix()
            for path in candidates
        )
        raise ScopeBindingError(
            f"Recorder Feature ID 对应多个当前文件: {paths}"
        )
    if len(candidates) == 1:
        current = load_feature_plan(candidates[0])
        if current.id == recorded_id:
            return candidates[0], current
    if recorded_path.is_file():
        raise ScopeBindingError(
            "当前路径的 Feature 身份与录制不一致，且未找到唯一的移动目标"
        )
    raise ScopeBindingError(f"当前 Feature 不存在: {recorded_path}")


def _validate_recorded_source_snapshot(
        session_dir,
        manifest,
        recorded_feature,
        recorded_scenario,
):
    source_path = Path(session_dir) / str(
        manifest.get("source_feature") or "source.feature"
    )
    if not source_path.is_file():
        raise ScopeBindingError(
            f"录制 Feature 快照不存在: {source_path.name}"
        )
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_plan = load_feature_plan(source_path)
    source_identity = persistent_feature_id(
        source_bytes.decode("utf-8-sig")
    )
    if source_identity and source_identity != str(
            recorded_feature.get("id") or ""
    ):
        raise ScopeBindingError(
            "录制 source.feature 的持久ID与manifest不一致"
        )
    declared_sha256 = str(manifest.get("source_hash") or "")
    if declared_sha256 and source_plan.source_hash != declared_sha256:
        raise ScopeBindingError("录制 source.feature 已被修改")

    source_scenario = _matching_scenario(
        source_plan.scenarios,
        recorded_scenario,
    )
    manifest_projection = recording_business_projection(
        recorded_feature,
        recorded_scenario,
    )
    source_projection = recording_business_projection(
        public_dict(source_plan),
        public_dict(source_scenario),
    )
    if _fingerprint(source_projection) != _fingerprint(manifest_projection):
        raise ScopeBindingError(
            "录制 source.feature 与 manifest 业务快照不一致"
        )
    return source_sha256, source_identity


def _resolve_project_root(session_dir, manifest, source_value, *, explicit):
    if explicit is not None:
        return Path(explicit).resolve()
    root = _session_project_root(session_dir)
    if root is not None:
        return root
    root = _recording_project_root(session_dir, manifest)
    if root is not None:
        return root
    if source_value.is_absolute():
        candidate = source_value.resolve().parent
        if candidate != Path(candidate.anchor) and _is_relative_to(
                session_dir,
                candidate,
        ):
            return candidate
    return Path(Paths.BASE_DIR).resolve()


def _session_project_root(session_dir):
    for candidate in (session_dir, *session_dir.parents):
        if (
                candidate.name.casefold() == "recording_sessions"
                and candidate.parent.name.casefold() == "artifacts"
        ):
            return candidate.parent.parent.resolve()
    return None


def _recording_project_root(session_dir, manifest):
    output_root = Path(str(
        (manifest.get("capture_config") or {}).get("output_root") or ""
    ))
    if (
            not output_root.is_absolute()
            or output_root.name.casefold() != "recording_sessions"
            or output_root.parent.name.casefold() != "artifacts"
    ):
        return None
    output_root = output_root.resolve()
    if not _is_relative_to(session_dir, output_root):
        return None
    return output_root.parent.parent.resolve()


def _is_relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def validate_request_scope_binding(request, *, project_root=None):
    declared = (
        ((request.get("target") or {}).get("scenario") or {}).get(
            "step_scope_binding"
        )
        or {}
    )
    if not declared and not _request_requires_scope_binding(request):
        return []
    if declared.get("scope_binding_version") != SCOPE_BINDING_VERSION:
        return ["Request 缺少有效的 ScopeBindingV1"]
    session_dir = Path(
        (request.get("session") or {}).get("absolute_path") or ""
    )
    try:
        current = bind_recording_step_scope(
            session_dir,
            project_root=project_root,
        )
    except (OSError, TypeError, ValueError) as error:
        return [f"当前 Step scope 无法解析: {type(error).__name__}: {error}"]
    if current.get("binding_fingerprint") != declared.get(
            "binding_fingerprint"
    ):
        return ["Feature 或 Step scope 在 Request 创建后已变化"]
    return []


def _request_requires_scope_binding(request):
    if (request.get("identity_basis") or {}).get("step_scope_fingerprint"):
        return True
    version = str(
        (request.get("framework_contract") or {}).get(
            "generation_contract_version"
        )
        or ""
    )
    try:
        major, minor = (
            int(value)
            for value in version.split(".", 1)
        )
    except (TypeError, ValueError):
        return False
    return (major, minor) >= (6, 13)


def _matching_scenario(scenarios, recorded):
    recorded_id = str(recorded.get("id") or "")
    exact = [scenario for scenario in scenarios if scenario.id == recorded_id]
    if len(exact) == 1:
        return exact[0]
    matches = [
        scenario
        for scenario in scenarios
        if scenario.name == str(recorded.get("name") or "")
        and scenario.kind == str(recorded.get("kind") or "scenario")
        and scenario.example_id == recorded.get("example_id")
    ]
    if len(matches) != 1:
        raise ScopeBindingError(
            "当前 Feature 无法唯一匹配录制 Scenario: "
            f"{recorded.get('name')!r}"
        )
    return matches[0]


def recording_business_projection(feature, scenario):
    return {
        "business_projection_version": BUSINESS_PROJECTION_VERSION,
        "feature": {
            "name": str(feature.get("name") or ""),
            "description": list(feature.get("description") or ()),
            "tags": _business_tags(feature.get("tags")),
        },
        "scenario": {
            "name": str(scenario.get("name") or ""),
            "kind": str(scenario.get("kind") or "scenario"),
            "example_id": scenario.get("example_id"),
            "example_values": dict(scenario.get("example_values") or {}),
            "tags": _business_tags(scenario.get("tags")),
            "steps": [
                {
                    key: step.get(key)
                    for key in (
                        "keyword",
                        "semantic_type",
                        "text",
                        "is_background",
                        "text_block",
                        "table",
                    )
                }
                for step in scenario.get("steps") or ()
            ],
            "specification": _strip_source_metadata(
                scenario.get("specification") or {}
            ),
        },
    }


def recording_business_fingerprint(feature, scenario):
    return _fingerprint(recording_business_projection(feature, scenario))


def _strip_source_metadata(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "line":
                continue
            if key == "tags":
                result[key] = _business_tags(item)
            else:
                result[key] = _strip_source_metadata(item)
        return result
    if isinstance(value, list):
        return [_strip_source_metadata(item) for item in value]
    return value


def _business_tags(tags):
    return sorted(
        str(tag)
        for tag in tags or ()
        if not _is_step_scope_tag(tag)
    )


def _is_step_scope_tag(tag):
    normalized = normalize_tag(tag)
    return any(
        normalized.startswith(normalize_tag(prefix))
        for prefix in STEP_SCOPE_PREFIXES
    )


def _workspace_scope(value):
    value = dict(value)
    runtime_files = list(value.get("files") or ())
    value["runtime_files"] = runtime_files
    value["files"] = [_behavior_path(path) for path in runtime_files]
    value["entry_file"] = _behavior_path(value.get("entry_file"))
    value["file_statuses"] = {
        _behavior_path(path): status
        for path, status in (value.get("file_statuses") or {}).items()
    }
    declarations = []
    for declaration in value.get("declarations") or ():
        declaration = dict(declaration)
        declaration["step_file"] = _behavior_path(
            declaration.get("step_file")
        )
        declarations.append(declaration)
    value["declarations"] = declarations
    return value


def _step_behavior_files(
        feature,
        scenario,
    recorded_scenario,
        resolved,
):
    declarations = list(resolved.declarations)
    feature_file = declarations[0].step_file
    rule_file = feature_file
    scenario_file = feature_file
    for declaration in declarations[1:]:
        if declaration.owner == "Rule":
            rule_file = declaration.step_file
            scenario_file = declaration.step_file
        elif declaration.owner in {"Scenario", "Scenario Outline"}:
            scenario_file = declaration.step_file

    template = (
        scenario.parent
        if isinstance(getattr(scenario, "parent", None), ScenarioOutline)
        else scenario
    )
    container = getattr(template, "parent", None)
    rule = container if getattr(container, "keyword", None) == "Rule" else None
    feature_background_count = len(
        getattr(getattr(feature, "background", None), "steps", None) or ()
    )
    rule_background_count = len(
        getattr(getattr(rule, "background", None), "steps", None) or ()
    )
    steps = list(recorded_scenario.get("steps") or ())
    expanded_steps = list(scenario.all_steps)
    if len(steps) != len(expanded_steps):
        raise ScopeBindingError(
            "当前 Feature 的 Step 展开结果与 Recorder 计划不一致"
        )
    result = {}
    for index, step in enumerate(steps):
        if index < feature_background_count:
            behavior_file = feature_file
        elif index < feature_background_count + rule_background_count:
            behavior_file = rule_file
        else:
            behavior_file = scenario_file
        result[str(step.get("id") or "")] = _behavior_path(behavior_file)
    return result


def _behavior_path(value):
    value = str(value or "").replace("\\", "/").lstrip("/")
    return f"Bdd/steps/{value}"


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _scope_binding_fingerprint(value):
    identity = json.loads(json.dumps(value, ensure_ascii=False))
    scope = identity.get("resolved_step_scope") or {}
    scope.pop("file_statuses", None)
    return _fingerprint(identity)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))