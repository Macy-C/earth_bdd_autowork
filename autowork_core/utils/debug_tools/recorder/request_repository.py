from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
    annotation_snapshot_is_valid,
    build_annotation_snapshot,
    current_annotation_snapshot_for_request,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    safe_segment,
    stable_digest,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


REQUEST_INDEX_VERSION = "1.0"
HASHED_ARTIFACT_KEYS = (
    "take_metadata",
    "events_effective",
    "actions_effective",
    "timeline_state",
    "tree_diff",
    "locator_candidates_effective",
    "action_media",
    "media_index",
    "evidence_graph",
    "semantic_pack",
    "pic_template_audit",
)
CURRENT_PROJECTION_ARTIFACT_KEYS = frozenset({
    "events_effective",
    "actions_effective",
    "timeline_state",
    "locator_candidates_effective",
    "action_media",
    "media_index",
    "evidence_graph",
    "semantic_pack",
    "pic_template_audit",
})
def find_latest_request(session_dir, step_ids):
    step_ids = normalize_step_ids(step_ids)
    request = _indexed_request(session_dir, step_ids)
    return request or _scan_latest_request(session_dir, step_ids)


def index_generation_request(session_dir, request):
    session_dir = Path(session_dir).resolve()
    step_ids = request_step_ids(request)
    scope = request_scope_key(step_ids)
    fingerprint = evidence_fingerprint(request)
    request["request_scope"] = scope
    request["evidence_fingerprint"] = fingerprint
    if request.get("request_version") == "3.0":
        request["request_fingerprint"] = request_fingerprint(request)
    request_path = resolve_request_path(session_dir, request["request_path"])
    existing = _read_json(request_path)
    if request.get("request_version") == "3.0" and existing:
        if existing != request:
            raise ValueError(f"禁止改写不可变 RequestV3: {request_path}")
    else:
        write_json_atomic(request_path, request)

    index_path = session_dir / "ai" / "requests" / "index.json"
    index = _read_json(index_path)
    if index.get("request_index_version") != REQUEST_INDEX_VERSION:
        index = {
            "schema_version": SCHEMA_VERSION,
            "request_index_version": REQUEST_INDEX_VERSION,
            "latest_by_scope": {},
        }
    updated_at = datetime.now().isoformat(timespec="seconds")
    index["updated_at"] = updated_at
    index.setdefault("latest_by_scope", {})[scope] = {
        "request_id": request.get("request_id"),
        "path": request["request_path"],
        "step_ids": list(step_ids),
        "evidence_fingerprint": fingerprint,
        "status": request_status(request, session_dir=session_dir),
        "updated_at": updated_at,
    }
    write_json_atomic(index_path, index)
    return request_path


def request_matches_current_evidence(session_dir, request, step_ids=None):
    if not request_identity_is_valid(request, step_ids):
        return False
    session_dir = Path(session_dir).resolve()
    if not _request_scenario_scope_matches(
            session_dir,
            request,
            normalize_step_ids(step_ids or request_step_ids(request)),
    ):
        return False
    if not _request_projection_artifacts_match(session_dir, request):
        return False
    matches, _current = request_revision_matches(
        session_dir,
        request,
        request.get("revision_snapshot"),
    )
    return matches


def request_matches_current_projection(session_dir, request):
    session_dir = Path(session_dir).resolve()
    for evidence in request.get("evidence") or ():
        artifacts = evidence.get("artifacts") or {}
        try:
            take_dir = resolve_session_path(session_dir, artifacts.get("take"))
        except (TypeError, ValueError):
            return False
        store = ProjectionStore(take_dir)
        snapshot = store.current()
        if snapshot is None:
            return False
        request_hashes = evidence.get("artifact_hashes") or {}
        if not _artifacts_match_projection_snapshot(
                session_dir,
                artifacts,
                request_hashes,
                snapshot,
        ):
            return False
    return True


def _request_projection_artifacts_match(session_dir, request):
    session_dir = Path(session_dir).resolve()
    selected_take_paths = _manifest_selected_take_paths(session_dir)
    for evidence in request.get("evidence") or ():
        artifacts = evidence.get("artifacts") or {}
        try:
            take_dir = resolve_session_path(session_dir, artifacts.get("take"))
        except (TypeError, ValueError):
            return False
        step_id = str((evidence.get("step") or {}).get("id") or "")
        if selected_take_paths.get(step_id) != take_dir:
            return False
        store = ProjectionStore(take_dir)
        snapshot = store.current()
        if snapshot is None:
            return False
        request_hashes = evidence.get("artifact_hashes") or {}
        if not _artifacts_match_projection_snapshot(
                session_dir,
                artifacts,
                request_hashes,
                snapshot,
        ):
            return False
    return True


def _artifacts_match_projection_snapshot(
        session_dir,
        artifacts,
        request_hashes,
        snapshot,
):
    relevant_keys = {
        key
        for key in set(artifacts) | set(snapshot.artifacts)
        if (
            key in CURRENT_PROJECTION_ARTIFACT_KEYS
            or key.startswith("pic_template:")
        )
    }
    for key in relevant_keys:
        current_path = snapshot.path(key)
        declared_path = artifacts.get(key)
        expected_hash = request_hashes.get(key)
        if current_path is None or not declared_path or not expected_hash:
            return False
        try:
            request_path = resolve_session_path(session_dir, declared_path)
            actual_hash = hashlib.sha256(
                current_path.read_bytes()
            ).hexdigest()
        except (OSError, TypeError, ValueError):
            return False
        if request_path != current_path.resolve():
            return False
        if actual_hash != expected_hash:
            return False
    return True


def _manifest_selected_take_paths(session_dir):
    manifest = _read_json(Path(session_dir) / "manifest.json")
    result = {}
    for entry in manifest.get("steps") or ():
        step_id = str((entry.get("plan") or {}).get("id") or "")
        selected_take_id = str(entry.get("selected_take") or "")
        selected_take = next((
            item
            for item in entry.get("takes") or ()
            if str(item.get("id") or "") == selected_take_id
        ), None)
        if not step_id or selected_take is None:
            continue
        try:
            result[step_id] = resolve_session_path(
                session_dir,
                selected_take.get("path"),
            )
        except (TypeError, ValueError):
            continue
    return result


def _request_scenario_scope_matches(session_dir, request, step_ids):
    expected = request_scenario_scope(session_dir, step_ids)
    declared = (request.get("target") or {}).get("scenario") or {}
    return bool(
        declared.get("logical_template_id")
        == expected["logical_template_id"]
        and declared.get("generation_scope")
        == expected["generation_scope"]
    )


def request_scenario_scope(session_dir, step_ids):
    manifest = _read_json(Path(session_dir) / "manifest.json")
    scenario = manifest.get("scenario") or {}
    all_step_ids = [
        str(step.get("id"))
        for step in scenario.get("steps") or []
        if step.get("id")
    ]
    if not all_step_ids:
        all_step_ids = [
            str((entry.get("plan") or {}).get("id"))
            for entry in manifest.get("steps") or []
            if (entry.get("plan") or {}).get("id")
        ]
    selected_set = set(step_ids)
    selected = [
        step_id
        for step_id in all_step_ids
        if step_id in selected_set
    ]
    return {
        "logical_template_id": (
            scenario.get("logical_template_id") or scenario.get("id")
        ),
        "generation_scope": {
            "kind": "scenario",
            "complete": (
                bool(all_step_ids)
                and selected_set == set(all_step_ids)
            ),
            "selected_step_ids": selected,
            "excluded_step_ids": [
                step_id
                for step_id in all_step_ids
                if step_id not in selected_set
            ],
        },
    }


def request_identity_is_valid(request, step_ids=None):
    if not request or request.get("request_version") != "3.0":
        return False
    expected_step_ids = normalize_step_ids(step_ids or request_step_ids(request))
    if request_step_ids(request) != expected_step_ids:
        return False
    if not request.get("revision_snapshot"):
        return False
    declared_annotation_snapshot = _declared_annotation_snapshot(request)
    if declared_annotation_snapshot is None:
        return False
    identity_basis = request.get("identity_basis") or {}
    if (
            identity_basis.get("request_identity_profile") != "business-v1"
            or "framework_contract" in request
    ):
        return False
    specification_fingerprint = identity_basis.get(
        "specification_fingerprint"
    )
    if (
            not specification_fingerprint
            or specification_fingerprint
            != _request_specification_fingerprint(request)
    ):
        return False
    fingerprint = request.get("evidence_fingerprint")
    if not (
        fingerprint
        and fingerprint == evidence_fingerprint(request)
    ):
        return False
    declared = request.get("request_fingerprint")
    if not declared:
        return False
    return bool(
        _seal_less_request_identity_is_valid(request)
        and declared == request_fingerprint(request)
    )


def generation_request_id(
        feature_id,
        scenario_id,
        evidence,
        *,
    scenario_scope=None,
    evidence_context_version=None,
        contract_hash,
        api_signature_hash,
        reviews,
        memory_revision,
        specification_fingerprint=None,
        annotation_fingerprint=None,
        execution_profile_fingerprint=None,
        identity_profile=None,
):
    identity = {
        "request_version": "3.0",
        "feature_id": feature_id,
        "scenario_id": scenario_id,
        "evidence": [
            {
                "step": item.get("step") or {},
                "selected_take": item.get("selected_take") or {},
                "timeline_revision": item.get("timeline_revision"),
                "evidence_graph": item.get("evidence_graph") or {},
                "artifact_hashes": item.get("artifact_hashes") or {},
            }
            for item in evidence
        ],
        "reviews": [
            {
                "step_id": item.get("step_id"),
                "code": item.get("code"),
                "hard_blocker": bool(
                    (item.get("recovery") or {}).get("hard_blocker")
                ),
            }
            for item in reviews
        ],
        "memory_revision": memory_revision,
    }
    if identity_profile:
        identity["identity_profile"] = str(identity_profile)
    else:
        identity["contract_hash"] = contract_hash
        identity["api_signature_hash"] = api_signature_hash
    if specification_fingerprint:
        identity["specification_fingerprint"] = str(
            specification_fingerprint
        )
    if annotation_fingerprint:
        identity["annotation_fingerprint"] = str(annotation_fingerprint)
    if execution_profile_fingerprint:
        identity["execution_profile_fingerprint"] = str(
            execution_profile_fingerprint
        )
    if scenario_scope:
        identity["scenario_scope"] = scenario_scope
    if evidence_context_version:
        identity["evidence_context_version"] = evidence_context_version
    digest = stable_digest(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        length=12,
    )
    label = safe_segment(
        "_".join(
            str((item.get("step") or {}).get("id") or "")
            for item in evidence
        ),
        20,
        "steps",
    )
    return f"request_{label}_{digest}"


def request_fingerprint(request):
    value = {
        key: item
        for key, item in request.items()
        if key != "request_fingerprint"
    }
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _seal_less_request_identity_is_valid(request):
    request_id = str(request.get("request_id") or "")
    target = request.get("target") or {}
    identity_basis = request.get("identity_basis") or {}
    if identity_basis.get("request_identity_profile") != "business-v1":
        return False
    declared_execution_fingerprint = identity_basis.get(
        "execution_profile_fingerprint"
    )
    if declared_execution_fingerprint:
        from autowork_core.utils.debug_tools.recorder.execution_profile import (
            execution_profile_fingerprint,
        )

        try:
            actual_execution_fingerprint = execution_profile_fingerprint(
                request.get("execution")
            )
        except (TypeError, ValueError):
            return False
        if actual_execution_fingerprint != declared_execution_fingerprint:
            return False
    elif request.get("execution") is not None:
        return False
    scenario = target.get("scenario") or {}
    scenario_scope = {
        key: scenario.get(key)
        for key in (
            "logical_template_id",
            "generation_scope",
        )
        if scenario.get(key) is not None
    }
    expected = generation_request_id(
        (target.get("feature") or {}).get("id"),
        scenario.get("id"),
        request.get("evidence") or [],
        scenario_scope=scenario_scope or None,
        evidence_context_version=(
            identity_basis.get(
                "evidence_context_version"
            )
        ),
        contract_hash=None,
        api_signature_hash=None,
        reviews=(request.get("readiness") or {}).get(
            "target_review_required"
        ) or [],
        memory_revision=(request.get("memory_context") or {}).get(
            "revision"
        ),
        specification_fingerprint=(
            identity_basis.get(
                "specification_fingerprint"
            )
        ),
        annotation_fingerprint=(
            identity_basis.get(
                "annotation_fingerprint"
            )
        ),
        execution_profile_fingerprint=(
            identity_basis.get(
                "execution_profile_fingerprint"
            )
        ),
        identity_profile=identity_basis.get("request_identity_profile"),
    )
    return request_id == expected


def request_revision_snapshot(session_dir, request):
    """Read only revision files; do not recursively re-hash immutable media."""
    session_dir = Path(session_dir).resolve()
    manifest = _read_json(session_dir / "manifest.json")
    manifest_steps = {
        (entry.get("plan") or {}).get("id"): entry
        for entry in manifest.get("steps") or []
    }
    annotations = RecordingAnnotationRepository(session_dir)
    takes = []
    for evidence in request.get("evidence") or []:
        step_id = (evidence.get("step") or {}).get("id")
        state = manifest_steps.get(step_id) or {}
        artifacts = evidence.get("artifacts") or {}
        take_path = artifacts.get("take")
        try:
            take_dir = resolve_session_path(session_dir, take_path)
        except ValueError:
            take_dir = session_dir / "__invalid_take__"
        try:
            timeline_path = resolve_session_path(
                session_dir,
                artifacts.get("timeline_state"),
            )
        except ValueError:
            timeline_path = take_dir / "timeline-state.json"
        try:
            graph_path = resolve_session_path(
                session_dir,
                artifacts.get("evidence_graph"),
            )
        except ValueError:
            graph_path = take_dir / "evidence" / "graph.json"
        timeline = _read_json(timeline_path)
        graph = _read_json(graph_path)
        try:
            structured_hashes = artifact_hashes(
                session_dir,
                artifacts,
            )
        except ValueError:
            structured_hashes = {}
        take_snapshot = {
            "step_id": step_id,
            "selected_take_id": state.get("selected_take"),
            "timeline_revision": timeline.get("timeline_revision"),
            "graph_fingerprint": graph.get("graph_fingerprint"),
            "source_fingerprint": (
                graph.get("source") or {}
            ).get("artifact_fingerprint"),
            "structured_artifact_hashes": structured_hashes,
            "table_fingerprint": _table_fingerprint(
                (state.get("plan") or {}).get("table")
            ),
            "step_user_context_revision": (
                annotations.step_context_revision(step_id)
            ),
        }
        takes.append(take_snapshot)
    annotation_snapshot = current_annotation_snapshot_for_request(
        session_dir,
        request,
    )
    value = {
        "request_id": request.get("request_id"),
        "evidence_fingerprint": request.get("evidence_fingerprint"),
        "takes": takes,
        "source_feature_sha256": (
            hashlib.sha256(
                (session_dir / "source.feature").read_bytes()
            ).hexdigest()
            if (session_dir / "source.feature").is_file()
            else None
        ),
        "annotation_snapshot_fingerprint": annotation_snapshot.get(
            "snapshot_fingerprint"
        ),
        "required_annotation_count": sum(
            len(annotation_ids)
            for annotation_ids in annotation_snapshot.get(
                "required_annotation_ids_by_step"
            ).values()
        ),
    }
    if (
        (request.get("identity_basis") or {}).get(
            "request_identity_profile"
        )
        != "business-v1"
    ):
        contract_path = session_dir / "ai" / "generation-contract.json"
        contract = _read_json(contract_path)
        value.update({
            "contract_hash": contract.get("contract_hash"),
            "api_signature_hash": (
                contract.get("framework_contract") or {}
            ).get("api_signature_hash"),
            "contract_file_sha256": (
                hashlib.sha256(contract_path.read_bytes()).hexdigest()
                if contract_path.is_file()
                else None
            ),
        })
    value["seal"] = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return value


def request_revision_matches(session_dir, request, expected):
    current = request_revision_snapshot(session_dir, request)
    expected = expected or {}
    declared_annotation_snapshot = _declared_annotation_snapshot(request)
    if (
            declared_annotation_snapshot is None
            or current.get("annotation_snapshot_fingerprint")
            != declared_annotation_snapshot.get("snapshot_fingerprint")
    ):
        return False, current
    try:
        current_annotation_snapshot = current_annotation_snapshot_for_request(
            session_dir,
            request,
        )
    except (OSError, TypeError, ValueError):
        return False, current
    if current_annotation_snapshot != declared_annotation_snapshot:
        return False, current
    return current == expected, current


def _declared_annotation_snapshot(request):
    snapshot = request.get("annotation_snapshot")
    if snapshot is not None:
        if not annotation_snapshot_is_valid(snapshot):
            return None
        basis = request.get("identity_basis") or {}
        if any((
            basis.get("annotation_fingerprint")
            != snapshot.get("snapshot_fingerprint"),
            basis.get("annotation_snapshot_version")
            not in {None, snapshot.get("annotation_snapshot_version")},
        )):
            return None
        return snapshot
    try:
        return build_annotation_snapshot(
            (request.get("target") or {}).get("steps") or []
        )
    except (TypeError, ValueError):
        return None


def _request_specification_fingerprint(request):
    target = request.get("target") or {}
    feature = target.get("feature") or {}
    scenario = target.get("scenario") or {}
    value = {
        "feature": {
            key: feature.get(key)
            for key in (
                "id",
                "name",
                "description",
                "tags",
                "source_relpath",
            )
        },
        "scenario": {
            key: scenario.get(key)
            for key in (
                "id",
                "name",
                "kind",
                "logical_template_id",
                "example_id",
                "example_values",
                "specification",
                "step_scope_binding",
            )
        },
        "steps": [
            {
                key: step.get(key)
                for key in (
                    "id",
                    "keyword",
                    "text",
                    "table",
                    "text_block",
                )
            }
            for step in (target.get("steps") or ())
            if isinstance(step, dict)
        ],
    }
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def evidence_fingerprint(request):
    evidence = []
    for entry in request.get("evidence") or []:
        evidence_entry = {
            "step_id": (entry.get("step") or {}).get("id"),
            "selected_take_id": (entry.get("selected_take") or {}).get("id"),
            "timeline_revision": entry.get("timeline_revision"),
            "evidence_graph": entry.get("evidence_graph") or {},
            "artifact_hashes": entry.get("artifact_hashes") or {},
        }
        evidence.append(evidence_entry)
    value = {
        "session_id": (request.get("session") or {}).get("id"),
        "step_ids": list(request_step_ids(request)),
        "evidence": evidence,
        "evidence_context_fingerprint": (
            request.get("evidence_context") or {}
        ).get("context_fingerprint"),
    }
    if (
        (request.get("identity_basis") or {}).get(
            "request_identity_profile"
        )
        != "business-v1"
    ):
        value.update({
            "generation_contract": request.get("generation_contract"),
            "framework_contract": request.get("framework_contract"),
        })
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _table_fingerprint(table):
    if not isinstance(table, dict) or not table:
        return None
    payload = json.dumps(
        table,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def request_scope_key(step_ids):
    normalized = normalize_step_ids(step_ids)
    return f"steps-{stable_digest(*normalized, length=16)}"


def request_status(request, *, session_dir=None):
    if request.get("request_version") != "3.0":
        return "request_rematerialization_required"
    reviews = (request.get("readiness") or {}).get("target_review_required") or []
    if (request.get("readiness") or {}).get("bundle_valid") is False:
        return "blocked"
    if any((item.get("recovery") or {}).get("hard_blocker") for item in reviews):
        return "blocked"
    if session_dir is None:
        return "draft"
    from autowork_core.utils.debug_tools.recorder.workflow_state import (
        workflow_status_for_request,
    )

    return workflow_status_for_request(session_dir, request)


def artifact_hashes(session_dir, artifacts):
    session_dir = Path(session_dir).resolve()
    result = {}
    keys = [
        *HASHED_ARTIFACT_KEYS,
        *sorted(
            key
            for key in artifacts
            if str(key).startswith("pic_template:")
        ),
    ]
    for key in dict.fromkeys(keys):
        relative = artifacts.get(key)
        if not relative:
            continue
        path = resolve_session_path(session_dir, relative)
        if not path.is_file():
            continue
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def resolve_request_path(session_dir, value):
    session_dir = Path(session_dir).resolve()
    root = (session_dir / "ai" / "requests").resolve()
    path = resolve_session_path(session_dir, value)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"AI 请求路径越界: {value}") from error
    if (
        path.parent != root
        or not path.name.startswith(("request_", "request-"))
        or path.suffix != ".json"
    ):
        raise ValueError(
            f"AI 请求必须是 ai/requests/request_*.json 或历史 request-*.json: {value}"
        )
    return path


def session_dir_for_request_path(request_path, request=None):
    request_path = Path(request_path).resolve()
    if request_path.parent.name != "requests" or request_path.parent.parent.name != "ai":
        raise ValueError(f"AI 请求不在会话 ai/requests 目录: {request_path}")
    session_dir = request_path.parent.parent.parent.resolve()
    expected = resolve_request_path(
        session_dir,
        request_path.relative_to(session_dir),
    )
    if expected != request_path:
        raise ValueError(f"AI 请求路径无效: {request_path}")
    if request:
        declared = Path(
            (request.get("session") or {}).get("absolute_path") or ""
        ).resolve()
        if declared != session_dir:
            raise ValueError(
                f"请求声明的会话目录与文件位置不一致: {declared} != {session_dir}"
            )
        relative = request.get("request_path")
        if relative and resolve_request_path(session_dir, relative) != request_path:
            raise ValueError("request_path 与当前请求文件不一致")
        absolute = request.get("request_path_absolute")
        if absolute and Path(absolute).resolve() != request_path:
            raise ValueError("request_path_absolute 与当前请求文件不一致")
    return session_dir


def resolve_session_path(session_dir, value):
    session_dir = Path(session_dir).resolve()
    if value is None or str(value).strip() == "":
        raise ValueError("会话相对路径不能为空")
    candidate = Path(str(value))
    path = (
        candidate.resolve()
        if candidate.is_absolute()
        else (session_dir / candidate).resolve()
    )
    try:
        path.relative_to(session_dir)
    except ValueError as error:
        raise ValueError(f"会话路径越界: {value}") from error
    return path


def request_step_ids(request):
    return normalize_step_ids(
        entry.get("id")
        for entry in (request.get("target") or {}).get("steps") or []
    )


def normalize_step_ids(step_ids):
    if isinstance(step_ids, str):
        step_ids = [step_ids]
    return tuple(sorted({
        str(step_id)
        for step_id in step_ids or ()
        if step_id
    }))


def _indexed_request(session_dir, step_ids):
    index = _read_json(Path(session_dir) / "ai" / "requests" / "index.json")
    entry = (index.get("latest_by_scope") or {}).get(request_scope_key(step_ids))
    if not entry:
        return None
    try:
        request_path = resolve_request_path(session_dir, entry.get("path"))
    except ValueError:
        return None
    request = _read_json(request_path)
    if request.get("evidence_fingerprint") != entry.get("evidence_fingerprint"):
        return None
    return request


def _scan_latest_request(session_dir, step_ids):
    request_dir = Path(session_dir) / "ai" / "requests"
    candidates = []
    paths = list(request_dir.glob("request_*.json"))
    paths.extend(request_dir.glob("request-*.json"))
    for path in paths:
        request = _read_json(path)
        if request_step_ids(request) != step_ids or request.get("stale"):
            continue
        candidates.append(request)
    return max(candidates, key=lambda item: item.get("created_at") or "", default=None)


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}