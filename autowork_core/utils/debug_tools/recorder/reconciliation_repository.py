from __future__ import annotations

import json
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.code_reuse_index import (
    append_capability_candidates,
    build_code_reuse_index,
    build_window_asset_catalog,
    find_reuse_candidates,
)
from autowork_core.utils.debug_tools.recorder.evidence_context import (
    load_evidence_context,
)
from autowork_core.utils.debug_tools.recorder.memory_digest import (
    build_memory_digest,
)
from autowork_core.utils.debug_tools.recorder.semantic_pack import (
    SUPPORTED_SEMANTIC_PACK_VERSIONS,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    resolve_session_path,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


BRIEF_VERSION = "4.4"
SUPPORTED_BRIEF_VERSIONS = {BRIEF_VERSION}


def review_source_id(review):
    payload = {
        key: review.get(key)
        for key in (
            "step_id",
            "code",
            "message",
            "evidence",
            "blocking",
            "recovery",
        )
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    return "review-" + hashlib.sha256(encoded).hexdigest()


class ReconciliationRepository:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir).resolve()

    def load_inputs(self, request):
        return {
            "context": self._load_request_context(request),
            "action_metadata": self._load_effective_action_metadata(request),
            "memory": self._load_memory_summary(request),
            "semantics": self._load_semantic_summary(request),
        }

    @staticmethod
    def input_recovery(request):
        result = []
        for evidence in request.get("evidence") or ():
            step_id = str((evidence.get("step") or {}).get("id") or "")
            for candidate in evidence.get("input_recovery") or ():
                if not isinstance(candidate, dict):
                    continue
                value = dict(candidate)
                if step_id and not value.get("step_id"):
                    value["step_id"] = step_id
                if str(value.get("step_id") or "") != step_id:
                    raise ValueError(
                        "input recovery candidate跨Step引用: "
                        f"{value.get('step_id')} != {step_id}"
                    )
                result.append(value)
        candidate_ids = [
            str(item.get("candidate_id") or "")
            for item in result
        ]
        if not all(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("input recovery candidate ID缺失或冲突")
        return result

    def write(self, request_id, reconciliation, brief):
        reconciliation_path = (
            self.session_dir
            / "ai"
            / "reconciliation-reports"
            / f"{request_id}.json"
        )
        brief_path = (
            self.session_dir
            / "ai"
            / "generation-briefs"
            / f"{request_id}.json"
        )
        persisted_reconciliation = _reuse_identical_artifact(
            reconciliation_path,
            reconciliation,
            "reconciliation_fingerprint",
        )
        persisted_brief = _reuse_identical_artifact(
            brief_path,
            brief,
            "brief_fingerprint",
            compact=True,
        )
        return {
            "brief_path": str(brief_path),
            "reconciliation_path": str(reconciliation_path),
            "brief": persisted_brief,
            "reconciliation": persisted_reconciliation,
        }

    def _load_request_context(self, request):
        declared = request.get("evidence_context") or {}
        path = resolve_session_path(self.session_dir, declared.get("path"))
        context = load_evidence_context(path)
        expected = declared.get("context_fingerprint")
        if expected and context.get("context_fingerprint") != expected:
            raise ValueError("Evidence Context 指纹与 request 不一致")
        return context

    def _load_effective_action_metadata(self, request):
        result = {}
        for evidence in request.get("evidence") or []:
            artifacts = evidence.get("artifacts") or {}
            step_id = str((evidence.get("step") or {}).get("id") or "")
            take_value = artifacts.get("take")
            if not take_value:
                continue
            action_value = artifacts.get("actions_effective")
            if not action_value:
                raise ValueError(
                    f"Step {step_id} 缺少 current actions_effective pointer"
                )
            try:
                value = json.loads(
                    resolve_session_path(
                        self.session_dir,
                        action_value,
                    ).read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"Step {step_id} effective actions 无法读取: "
                    f"{type(error).__name__}: {error}"
                ) from error
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("actions"), list)
            ):
                raise ValueError(
                    f"Step {step_id} effective actions 必须是含actions数组的object"
                )
            for action in value["actions"]:
                if not isinstance(action, dict):
                    raise ValueError(
                        f"Step {step_id} effective action 必须是object"
                    )
                action_id = action.get("id")
                if not action_id:
                    raise ValueError(
                        f"Step {step_id} effective action 缺少id"
                    )
                correction = _compact_correction_provenance(action)
                metadata = {
                    "value_binding": action.get("value_binding"),
                    "note": action.get("note"),
                }
                if correction:
                    metadata["correction"] = correction
                result[_action_scope_key(step_id, action_id)] = metadata
        return result

    def _load_memory_summary(self, request):
        declared = request.get("memory_context") or {}
        path_value = declared.get("path")
        if not path_value:
            return build_memory_digest(
                {},
                revision=declared.get("revision"),
            )
        try:
            path = resolve_session_path(self.session_dir, path_value)
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            value = {}
        return build_memory_digest(
            value,
            revision=declared.get("revision"),
        )

    def _load_semantic_summary(self, request):
        result = {
            "available": False,
            "packs": [],
            "actions": {},
            "window_causality": [],
            "step_continuity": [],
            "reuse_candidates": [],
            "recorded_window_roots": self._load_recorded_window_roots(
                request
            ),
            "reuse_index": {
                "available": False,
                "index_fingerprint": None,
                "stats": {},
                "warnings": [],
            },
        }
        continuity_packs = []
        for evidence in request.get("evidence") or []:
            artifacts = evidence.get("artifacts") or {}
            hashes = evidence.get("artifact_hashes") or {}
            relative = artifacts.get("semantic_pack")
            if not relative:
                continue
            path = resolve_session_path(self.session_dir, relative)
            if hashes.get("semantic_pack") and _sha256(path) != hashes.get(
                    "semantic_pack"
            ):
                raise ValueError("Semantic Pack hash 与 Request 不一致")
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("semantic_pack_version") not in (
                SUPPORTED_SEMANTIC_PACK_VERSIONS
            ):
                raise ValueError(f"不支持的 Semantic Pack: {path}")
            step_id = str((value.get("step") or {}).get("id") or "")
            continuity_packs.append({
                "step_id": step_id,
                "action_effects": value.get("action_effects") or [],
                "assertion_candidates": value.get("assertion_candidates") or [],
                "binding_candidates": value.get("binding_candidates") or [],
                "window_causality": value.get("window_causality") or [],
            })
            result["packs"].append({
                "step_id": step_id,
                "take_id": value.get("take_id"),
                "semantic_fingerprint": value.get("semantic_fingerprint"),
                "pic_template_audit": value.get("pic_template_audit") or {},
                "unresolved_decisions": value.get("unresolved_decisions") or [],
            })
            effects = {
                str(item.get("action_id")): item
                for item in value.get("action_effects") or []
            }
            facts = {
                str(item.get("action_id")): item
                for item in value.get("semantic_facts") or []
            }
            assertions = {
                str(item.get("action_id")): item
                for item in value.get("assertion_candidates") or []
            }
            bindings = {
                str(item.get("action_id")): item
                for item in value.get("binding_candidates") or []
            }
            intents = {
                str(item.get("action_id")): item
                for item in value.get("intent_candidates") or []
            }
            roles = {
                str(item.get("action_id")): item
                for item in value.get("role_candidates") or []
            }
            fallbacks = {
                str(item.get("action_id")): item
                for item in value.get("locator_fallback_candidates") or []
            }
            unresolved = {}
            for item in value.get("unresolved_decisions") or []:
                action_id = str(item.get("action_id") or "")
                if action_id:
                    unresolved.setdefault(action_id, []).append(dict(item))
            action_ids = set().union(
                effects,
                facts,
                assertions,
                bindings,
                intents,
                roles,
                fallbacks,
            )
            for action_id in action_ids:
                fallback = fallbacks.get(action_id) or {}
                pic = fallback.get("pic_candidate") or {}
                result["actions"][_action_scope_key(step_id, action_id)] = {
                    "step_id": step_id,
                    "effect": _compact_effect(effects.get(action_id)),
                    "facts": _compact_semantic_facts(
                        facts.get(action_id)
                    ),
                    "assertion_candidates": _compact_candidates(
                        (assertions.get(action_id) or {}).get("candidates"),
                    ),
                    "assertion_requires_decision": bool(
                        (assertions.get(action_id) or {}).get(
                            "requires_decision"
                        )
                    ),
                    "unresolved_decisions": unresolved.get(action_id) or [],
                    "binding_candidates": _compact_candidates(
                        (bindings.get(action_id) or {}).get("candidates"),
                    ),
                    "resolved_binding": (
                        bindings.get(action_id) or {}
                    ).get("resolved_source"),
                    "intent_candidates": _compact_candidates(
                        (intents.get(action_id) or {}).get("candidates"),
                    ),
                    "role_candidates": _compact_candidates(
                        (roles.get(action_id) or {}).get("candidates"),
                    ),
                    "locator_fallback": {
                        "pos_available": bool(fallback.get("pos_candidate")),
                        "pic_candidate_id": pic.get("candidate_id"),
                        "pic_audit_status": pic.get("audit_status"),
                        "pic_authorizable": bool(
                            pic.get("audit_status") == "passed"
                            and pic.get("template_sha256")
                            and pic.get("region_locator")
                        ),
                    } if fallback else None,
                }
            result["window_causality"].extend(
                value.get("window_causality") or []
            )
            result["reuse_candidates"].extend(
                value.get("reuse_candidates") or []
            )
        result["step_continuity"] = _step_continuity_hints(
            continuity_packs,
        )
        result["available"] = bool(result["packs"])
        self._attach_reuse_candidates(request, result)
        return result

    def _load_recorded_window_roots(self, request):
        roots = {}
        for evidence in request.get("evidence") or ():
            artifacts = evidence.get("artifacts") or {}
            relative = (
                artifacts.get("locator_candidates_effective")
                or artifacts.get("locator_candidates")
            )
            if not relative:
                continue
            try:
                value = yaml.safe_load(
                    resolve_session_path(
                        self.session_dir,
                        relative,
                    ).read_text(encoding="utf-8")
                ) or {}
            except (OSError, ValueError, yaml.YAMLError):
                continue
            for root_name, criteria in (value.get("roots") or {}).items():
                if not isinstance(criteria, dict):
                    continue
                roots[str(root_name)] = dict(criteria)
        return roots

    def _attach_reuse_candidates(self, request, semantics):
        project_root, recording_root = _project_and_recording_roots(
            self.session_dir
        )
        cache_path = recording_root / "code-reuse-index.json"
        try:
            index = build_code_reuse_index(project_root, cache_path)
            candidates = find_reuse_candidates(
                index,
                request,
                semantics,
            )
            query = " ".join(
                str(step.get("text") or "")
                for step in (request.get("target") or {}).get("steps") or ()
            )
            semantics["reuse_candidates"] = append_capability_candidates(
                candidates,
                recording_root,
                query,
                project_root=project_root,
            )
            scenario_tags = {
                str(item)
                for item in (
                    ((request.get("target") or {}).get("scenario") or {}).get(
                        "tags"
                    ) or ()
                )
            }
            semantics["environment_dependencies"] = [
                {
                    key: entry.get(key)
                    for key in (
                        "candidate_id",
                        "dependency_id",
                        "path",
                        "symbol",
                        "line",
                        "file_sha256",
                        "definition_fingerprint",
                        "phase",
                        "required_tags",
                        "data_keys",
                        "delegated_calls",
                        "quality",
                        "generation_allowed",
                    )
                    if entry.get(key) not in (None, [], {})
                }
                for file_value in (index.get("files") or {}).values()
                for entry in file_value.get("entries") or ()
                if entry.get("kind") == "application_lifecycle"
                and set(entry.get("required_tags") or ()) <= scenario_tags
            ][:12]
            semantics["window_asset_catalog"] = (
                build_window_asset_catalog(index)
            )
            semantics["reuse_index"] = {
                "available": True,
                "path": str(cache_path),
                "index_fingerprint": index.get("index_fingerprint"),
                "stats": {
                    key: (index.get("stats") or {}).get(key, 0)
                    for key in ("file_count", "entry_count")
                },
                "warnings": index.get("warnings") or [],
            }
        except Exception as error:
            semantics["reuse_candidates"] = []
            semantics["environment_dependencies"] = []
            semantics["window_asset_catalog"] = {
                "catalog_version": "1.0",
                "index_fingerprint": None,
                "candidates": [],
            }
            semantics["reuse_index"] = {
                "available": False,
                "path": str(cache_path),
                "index_fingerprint": None,
                "stats": {},
                "warnings": [
                    f"代码复用索引不可用: {type(error).__name__}: {error}"
                ],
            }


def load_generation_brief(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("brief_version") not in SUPPORTED_BRIEF_VERSIONS:
        raise ValueError(f"不支持的 generation brief: {path}")
    if value.get("brief_fingerprint") != _brief_fingerprint(value):
        raise ValueError(f"generation brief fingerprint 无效: {path}")
    value["brief_path"] = str(path)
    return value


def _reuse_identical_artifact(path, value, fingerprint_key, *, compact=False):
    path = Path(path)
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, dict)
            and existing.get(fingerprint_key) == value.get(fingerprint_key)
            and existing.get(fingerprint_key)
            == _artifact_fingerprint(existing, fingerprint_key)
        ):
            return existing
    write_json_atomic(path, value, compact=compact)
    return value


def _artifact_fingerprint(value, fingerprint_key):
    if fingerprint_key == "brief_fingerprint":
        return _brief_fingerprint(value)
    if fingerprint_key == "reconciliation_fingerprint":
        return _reconciliation_fingerprint(value)
    raise ValueError(f"不支持的 artifact fingerprint: {fingerprint_key}")


def load_reconciliation_reviews(session_dir, workflow):
    session_dir = Path(session_dir).resolve()
    request_id = str((workflow or {}).get("request_id") or "")
    brief_pointer = (workflow or {}).get("brief") or {}
    if not request_id or not brief_pointer.get("path"):
        raise ValueError("workflow state 缺少 reconciliation 指针")
    brief = load_generation_brief(
        resolve_session_path(session_dir, brief_pointer["path"])
    )
    if any((
        brief.get("request_id") != request_id,
        brief.get("brief_fingerprint")
        != brief_pointer.get("brief_fingerprint"),
    )):
        raise ValueError("Generation Brief 与 workflow state 不一致")
    path = (
        session_dir
        / "ai"
        / "reconciliation-reports"
        / f"{request_id}.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_fingerprint = brief.get("reconciliation_fingerprint")
    actual_fingerprint = _reconciliation_fingerprint(value)
    if any((
        value.get("request_id") != request_id,
        value.get("reconciliation_fingerprint") != expected_fingerprint,
        value.get("reconciliation_fingerprint") != actual_fingerprint,
    )):
        raise ValueError(f"reconciliation report fingerprint 无效: {path}")
    reviews = value.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError(f"reconciliation report reviews 无效: {path}")
    return tuple(dict(item) for item in reviews if isinstance(item, dict))


def _compact_effect(value):
    if not value:
        return None
    return {
        "information_class": "frozen_observation_facts",
        "effect_id": value.get("effect_id"),
        "result": value.get("result"),
        "changes": (value.get("changes") or [])[:4],
        "after_state": value.get("after_state") or {},
        "windows_opened": (value.get("windows_opened") or [])[:2],
        "windows_closed": (value.get("windows_closed") or [])[:2],
        "visual_stability": value.get("visual_stability"),
        "evidence_ids": value.get("evidence_ids") or [],
    }


def _compact_semantic_facts(value):
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in (
            "step_role",
            "declared_step_text",
            "example_values",
            "observation_note",
            "observed_text_values",
            "observed_window_titles",
            "runtime_value_sources",
            "value_binding",
            "authority",
        )
        if value.get(key) not in (None, "", [], {})
    }


def _compact_candidates(values):
    result = []
    for item in (values or [])[:5]:
        if not isinstance(item, dict):
            continue
        result.append({
            key: item.get(key)
            for key in (
                "candidate_id",
                "operation",
                "target",
                "parameters",
                "source",
                "value",
                "intent",
                "recommended_operation",
                "semantic_operation",
                "declared_values",
                "requires_value_evidence",
                "range_minimum",
                "range_maximum",
                "role",
                "confidence",
                "reason",
                "evidence_ids",
                "implementation_constraint",
            )
            if item.get(key) is not None
        })
    return result


def _step_continuity_hints(packs, maximum=12):
    hints = []
    for previous, current in zip(packs or [], (packs or [])[1:]):
        from_step = str(previous.get("step_id") or "")
        to_step = str(current.get("step_id") or "")
        if not from_step or not to_step:
            continue
        previous_windows = {
            _window_identity(item.get("window") or {}): item
            for item in previous.get("window_causality") or []
            if not item.get("closed_during_take")
            and _window_identity(item.get("window") or {})
        }
        current_windows = {
            _window_identity(item.get("window") or {}): item
            for item in current.get("window_causality") or []
            if _window_identity(item.get("window") or {})
        }
        for identity in sorted(previous_windows.keys() & current_windows.keys()):
            first = previous_windows[identity]
            second = current_windows[identity]
            hints.append({
                "kind": "shared_window",
                "from_step_id": from_step,
                "to_step_id": to_step,
                "confidence": round(min(
                    float(first.get("confidence") or 0.0),
                    float(second.get("confidence") or 0.0),
                ), 4),
                "window_ref": _value_hash(identity)[:16],
                "advisory_only": True,
            })
        previous_values = _effect_values(previous.get("action_effects") or [])
        current_values = _candidate_values(current)
        for value_hash in sorted(previous_values.keys() & current_values.keys()):
            source = previous_values[value_hash]
            target = current_values[value_hash]
            hints.append({
                "kind": "value_match",
                "from_step_id": from_step,
                "to_step_id": to_step,
                "from_action_id": source["action_id"],
                "to_action_id": target["action_id"],
                "evidence_refs": [{
                    "step_id": from_step,
                    "evidence_ids": source["evidence_ids"],
                }, {
                    "step_id": to_step,
                    "evidence_ids": target["evidence_ids"],
                }],
                "confidence": round(min(
                    source["confidence"],
                    target["confidence"],
                ), 4),
                "advisory_only": True,
            })
    return hints[:maximum]


def _window_identity(window):
    handle = window.get("handle")
    process_id = window.get("process_id")
    class_name = str(window.get("class_name") or "").strip()
    if handle is not None and process_id is not None:
        return f"pid:{process_id}:hwnd:{handle}"
    if process_id is not None and class_name:
        return f"pid:{process_id}:class:{class_name}"
    return ""


def _effect_values(effects):
    result = {}
    for effect in effects or []:
        action_id = str(effect.get("action_id") or "")
        for change in effect.get("changes") or []:
            value = change.get("after")
            if value is None or not action_id:
                continue
            result[_value_hash(value)] = {
                "action_id": action_id,
                "evidence_ids": list(change.get("evidence_ids") or []),
                "confidence": 0.85,
            }
    return result


def _candidate_values(pack):
    result = {}
    groups = [
        *(pack.get("binding_candidates") or []),
        *(pack.get("assertion_candidates") or []),
    ]
    for group in groups:
        action_id = str(group.get("action_id") or "")
        for candidate in group.get("candidates") or []:
            value = candidate.get("value")
            if value is None:
                value = (candidate.get("parameters") or {}).get("expected")
            confidence = float(candidate.get("confidence") or 0.0)
            if value is None or not action_id or confidence < 0.8:
                continue
            digest = _value_hash(value)
            current = result.get(digest)
            item = {
                "action_id": action_id,
                "evidence_ids": list(candidate.get("evidence_ids") or []),
                "confidence": confidence,
            }
            if current is None or item["confidence"] > current["confidence"]:
                result[digest] = item
    return result


def _value_hash(value):
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _brief_fingerprint(value):
    import hashlib

    keys = [
        "brief_version",
        "reconciliation_fingerprint",
        "risk",
        "revision",
        "target",
        "story",
        "actions",
        "ambiguities",
        "conflicts",
        "required_forensic_evidence",
        "adjustment",
        "coverage",
        "generation",
    ]
    if value.get("brief_version") in {
        "3.4",
        "3.5",
        "3.6",
        "3.7",
        "3.8",
        "3.9",
        "4.0",
        "4.1",
        "4.2",
        "4.3",
        "4.4",
    }:
        keys.append("scenario_intelligence")
    if "agent_tasks" in value:
        keys.append("agent_tasks")
    if "draft_plan" in value:
        keys.append("draft_plan")
    if "semantics" in value:
        keys.append("semantics")
    if "memory_digest" in value:
        keys.append("memory_digest")
    if "window_ownership" in value:
        keys.append("window_ownership")
    if "annotation_snapshot" in value:
        keys.append("annotation_snapshot")
    normalized = {
        key: (
            value.get(key) or []
            if key == "ambiguities"
            else value.get(key)
        )
        for key in keys
    }
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reconciliation_fingerprint(value):
    import hashlib

    keys = [
        "reconciler_version",
        "risk",
        "revision",
        "target",
        "story",
        "actions",
        "reviews",
        "conflicts",
        "required_forensic_evidence",
        "adjustment",
        "memory",
        "semantics",
        "window_ownership",
        "coverage",
        "generation",
    ]
    if "agent_tasks" in value:
        keys.append("agent_tasks")
    if "draft_plan" in value:
        keys.append("draft_plan")
    if "annotation_snapshot" in value:
        keys.append("annotation_snapshot")
    payload = json.dumps(
        {key: value[key] for key in keys},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _project_and_recording_roots(session_dir):
    session_dir = Path(session_dir).resolve()
    recording_root = next(
        (
            parent
            for parent in (session_dir, *session_dir.parents)
            if parent.name == "recording_sessions"
        ),
        None,
    )
    if recording_root is not None and recording_root.parent.name == "artifacts":
        return recording_root.parent.parent.resolve(), recording_root.resolve()
    project_root = next(
        (
            parent.parent
            for parent in session_dir.parents
            if parent.name == "artifacts"
        ),
        None,
    )
    if project_root is None:
        from config.paths import Paths

        project_root = Paths.BASE_DIR.resolve()
    return (
        Path(project_root).resolve(),
        Path(project_root).resolve() / "artifacts" / "recording_sessions",
    )


def _compact_correction_provenance(action, *, max_merge_sources=12):
    source = action.get("source") or {}
    source_kind = str(source.get("kind") or "").strip()
    source_action_id = str(action.get("source_action_id") or "").strip()
    merge_source_ids = [
        str(action_id).strip()
        for action_id in action.get("source_action_ids") or ()
        if action_id is not None and str(action_id).strip()
    ]
    result = {}
    if source_kind == "supplement":
        result["source_kind"] = "supplement"
        if source_action_id:
            result["source_action_id"] = source_action_id
    if merge_source_ids:
        result["merge_source_ids"] = merge_source_ids[:max_merge_sources]
        result["merge_source_count"] = len(merge_source_ids)
        result["merge_sources_truncated"] = (
            len(merge_source_ids) > max_merge_sources
        )
    return result


def _action_scope_key(step_id, action_id):
    return f"{str(step_id)}\x1f{str(action_id)}"