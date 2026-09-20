from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from autowork_core.common.compile import compile_locator
from autowork_core.utils.debug_tools.recorder.analysis import (
    _snapshot_locator_matches,
    _snapshot_root,
    _snapshot_target_matches,
)
from autowork_core.utils.debug_tools.recorder.evidence_graph import (
    EVIDENCE_GRAPH_VERSION,
)
from autowork_core.utils.debug_tools.recorder.request_repository import (
    resolve_session_path,
)


LOCATOR_REUSE_MATCH_VERSION = "1.0"
_SUPPORTED_LOCATOR_METHODS = {"child", "xpath"}
_MAX_SNAPSHOTS_PER_TAKE = 4
_SUPPORTED_CHILD_CRITERIA = {
    "auto_id",
    "control_type",
    "title",
    "class_name",
}


def analyze_existing_locators(
        actions,
        window_ownership,
        locator_documents=None,
        snapshot_targets=(),
        *,
        project_root=None,
        user_modified_locator_files=(),
    ):
    """Classify only proven reuse and concrete locator-key conflicts."""
    locator_documents = dict(locator_documents or {})
    project_root = Path(project_root).resolve() if project_root else None
    owner_candidates = {
        **_unique_locator_maintenance_owners(
            window_ownership,
            user_modified_locator_files,
        ),
        **_unique_reuse_owners(window_ownership),
    }
    targets_by_action = {
        _action_key(item.get("step_id"), item.get("action_id")): item
        for item in snapshot_targets or ()
        if isinstance(item, dict)
        and _action_key(item.get("step_id"), item.get("action_id"))
    }
    matches = []
    issues = []
    for action in actions or ():
        if not isinstance(action, dict):
            continue
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("action_id") or action.get("id") or "")
        target = action.get("target") or {}
        root_name = str(target.get("root_name") or "")
        recorded_locator_name = str(target.get("locator_name") or "")
        candidate = owner_candidates.get(root_name)
        snapshot_target = targets_by_action.get(_action_key(step_id, action_id))
        if candidate is None:
            continue
        matches_for_action = []
        issues_for_action = []
        for context in _locator_contexts_for_action(
                candidate,
                window_ownership,
                step_id,
                action_id,
        ):
            locator_file = context["locator_file"]
            if locator_file not in locator_documents:
                locator_documents[locator_file] = (
                    _load_project_locator_document(project_root, locator_file)
                    if project_root is not None
                    else None
                )
            document = locator_documents.get(locator_file)
            candidate_locator_sha256 = context["locator_sha256"]
            if not isinstance(document, dict):
                issue = _locator_reuse_issue(
                    step_id=step_id,
                    action_id=action_id,
                    root_name=root_name,
                    owner_candidate_id=candidate.get("candidate_id"),
                    locator_file=locator_file,
                    locator_sha256="",
                    candidate_locator_sha256=candidate_locator_sha256,
                    candidate_hash_matches=False,
                    target_fingerprint=target.get("target_fingerprint"),
                    recorded_locator_name=recorded_locator_name,
                    matching_keys=[],
                    has_same_key=False,
                    generated_suffix_base_key=None,
                )
                if issue is not None:
                    issues_for_action.append(issue)
                continue
            current_locator_sha256 = str(document.get("sha256") or "")
            candidate_hash_matches = bool(
                candidate_locator_sha256
                and current_locator_sha256
                and candidate_locator_sha256 == current_locator_sha256
            )
            root_locator = context["root_locator"]
            has_same_key = _existing_key_for_root(
                document,
                recorded_locator_name,
                root_locator,
            )
            generated_suffix_base_key = _existing_generated_suffix_base_key(
                document,
                recorded_locator_name,
                root_locator,
            )
            if snapshot_target is None:
                identity_match = _recorded_locator_identity_match(
                    document,
                    root_locator=root_locator,
                    key=recorded_locator_name,
                    target=target,
                ) if candidate_hash_matches else None
                if identity_match is not None:
                    raw_locator = (document.get("entries") or {}).get(
                        recorded_locator_name
                    ) or {}
                    matches_for_action.append((
                        context,
                        current_locator_sha256,
                        recorded_locator_name,
                        identity_match,
                        raw_locator,
                    ))
                    continue
                issue = _locator_reuse_issue(
                    step_id=step_id,
                    action_id=action_id,
                    root_name=root_name,
                    owner_candidate_id=candidate.get("candidate_id"),
                    locator_file=locator_file,
                    locator_sha256=current_locator_sha256,
                    candidate_locator_sha256=candidate_locator_sha256,
                    candidate_hash_matches=candidate_hash_matches,
                    target_fingerprint=target.get("target_fingerprint"),
                    recorded_locator_name=recorded_locator_name,
                    matching_keys=[],
                    has_same_key=has_same_key,
                    generated_suffix_base_key=generated_suffix_base_key,
                )
                if issue is not None:
                    issues_for_action.append(issue)
                continue
            matching_keys = _matching_locator_keys(
                document,
                root_locator=root_locator,
                snapshot_target=snapshot_target,
            ) if candidate_hash_matches else []
            for key, proof in matching_keys:
                raw_locator = (document.get("entries") or {}).get(key) or {}
                matches_for_action.append((
                    context,
                    current_locator_sha256,
                    key,
                    proof,
                    raw_locator,
                ))
            if matching_keys:
                continue
            identity_match = _recorded_locator_identity_match(
                document,
                root_locator=root_locator,
                key=recorded_locator_name,
                target=target,
            ) if candidate_hash_matches else None
            if identity_match is not None:
                raw_locator = (document.get("entries") or {}).get(
                    recorded_locator_name
                ) or {}
                matches_for_action.append((
                    context,
                    current_locator_sha256,
                    recorded_locator_name,
                    identity_match,
                    raw_locator,
                ))
                continue
            issue = _locator_reuse_issue(
                step_id=step_id,
                action_id=action_id,
                root_name=root_name,
                owner_candidate_id=candidate.get("candidate_id"),
                locator_file=locator_file,
                locator_sha256=current_locator_sha256,
                candidate_locator_sha256=candidate_locator_sha256,
                candidate_hash_matches=candidate_hash_matches,
                target_fingerprint=target.get("target_fingerprint"),
                recorded_locator_name=recorded_locator_name,
                matching_keys=[],
                has_same_key=has_same_key,
                generated_suffix_base_key=generated_suffix_base_key,
            )
            if issue is not None:
                issues_for_action.append(issue)
        if len(matches_for_action) == 1:
            context, locator_sha256, key, proof, raw_locator = matches_for_action[0]
            identity = {
                "step_id": step_id,
                "action_id": action_id,
                "root_name": root_name,
                "evidence_name": recorded_locator_name,
                "owner_candidate_id": candidate.get("candidate_id"),
                "locator_file": context["locator_file"],
                "locator_key": key,
                "locator_sha256": locator_sha256,
                "locator_fingerprint": locator_mapping_fingerprint(
                    raw_locator
                ),
                "target_fingerprint": target.get("target_fingerprint"),
                "snapshot_proof": proof,
            }
            matches.append({
                "match_id": "locator-reuse-" + _stable_hash(identity)[:16],
                "status": "unique_same_target",
                **identity,
            })
            continue
        if len(matches_for_action) > 1:
            first_context = matches_for_action[0][0]
            issue = _locator_reuse_issue(
                step_id=step_id,
                action_id=action_id,
                root_name=root_name,
                owner_candidate_id=candidate.get("candidate_id"),
                locator_file=first_context["locator_file"],
                locator_sha256=matches_for_action[0][1],
                candidate_locator_sha256=first_context["locator_sha256"],
                candidate_hash_matches=True,
                target_fingerprint=target.get("target_fingerprint"),
                recorded_locator_name=recorded_locator_name,
                matching_keys=[item[2] for item in matches_for_action],
                has_same_key=False,
                generated_suffix_base_key=None,
            )
            if issue is not None:
                issues.append(issue)
            continue
        issues.extend(issues_for_action)
    return {
        "matches": sorted(matches, key=lambda item: (
            item["step_id"],
            item["action_id"],
            item["locator_file"],
            item["locator_key"],
        )),
        "issues": sorted(issues, key=lambda item: (
            item["step_id"],
            item["action_id"],
            item["code"],
        )),
    }


def match_existing_locators(
        actions,
        window_ownership,
        locator_documents=None,
        snapshot_targets=(),
        *,
        project_root=None,
    ):
    """Return only existing locator keys proven to hit recorded targets."""
    return analyze_existing_locators(
        actions,
        window_ownership,
        locator_documents,
        snapshot_targets,
        project_root=project_root,
    )["matches"]


def _locator_reuse_issue(
        *,
        step_id,
        action_id,
        root_name,
        owner_candidate_id,
        locator_file,
        locator_sha256,
        candidate_locator_sha256,
        candidate_hash_matches,
        target_fingerprint,
        recorded_locator_name,
        matching_keys,
        has_same_key,
        generated_suffix_base_key,
    ):
    if not candidate_hash_matches:
        code = "locator_reuse_asset_drift"
        existing_keys = (
            [recorded_locator_name]
            if has_same_key and recorded_locator_name
            else []
        )
    elif len(matching_keys) > 1:
        code = "locator_reuse_multiple_existing_keys"
        existing_keys = sorted(matching_keys)
    elif has_same_key:
        code = "locator_reuse_existing_key_unverified"
        existing_keys = [recorded_locator_name]
    elif generated_suffix_base_key:
        code = "locator_reuse_generated_suffix_conflict"
        existing_keys = [generated_suffix_base_key]
    else:
        return None
    identity = {
        "code": code,
        "step_id": step_id,
        "action_id": action_id,
        "root_name": root_name,
        "owner_candidate_id": owner_candidate_id,
        "locator_file": locator_file,
        "locator_sha256": locator_sha256,
        "candidate_locator_sha256": candidate_locator_sha256,
        "target_fingerprint": target_fingerprint,
        "recorded_locator_name": recorded_locator_name,
        "existing_keys": existing_keys,
    }
    return {
        "issue_id": "locator-reuse-issue-" + _stable_hash(identity)[:16],
        "status": "maintenance_required",
        **identity,
    }


def _existing_key_for_root(document, key, root_locator):
    locator = (document.get("entries") or {}).get(str(key) or "")
    return bool(
        isinstance(locator, dict)
        and str(locator.get("root") or "") == root_locator
    )


def _existing_generated_suffix_base_key(document, key, root_locator):
    key = str(key or "")
    match = re.fullmatch(r"(.+)_([2-9][0-9]*)", key)
    if match is None:
        return None
    base_key = match.group(1)
    return (
        base_key
        if _existing_key_for_root(document, base_key, root_locator)
        else None
    )


def _sorted_matches(matches):
    return sorted(matches, key=lambda item: (
        item["step_id"],
        item["action_id"],
        item["locator_file"],
        item["locator_key"],
    ))


def analyze_request_locator_reuse(
        session_dir,
        request,
        actions,
        window_ownership,
        *,
        project_root,
    user_modified_locator_files=(),
    ):
    """Match only Actions whose owning Page already has one safe reuse target."""
    session_dir = Path(session_dir).resolve()
    project_root = Path(project_root).resolve()
    owners = {
        **_unique_locator_maintenance_owners(
            window_ownership,
            user_modified_locator_files,
        ),
        **_unique_reuse_owners(window_ownership),
    }
    action_keys = {
        _action_key(
            action.get("step_id"),
            action.get("action_id") or action.get("id"),
        )
        for action in actions or ()
        if isinstance(action, dict)
        and str((action.get("target") or {}).get("root_name") or "")
        in owners
    }
    action_keys.discard(None)
    if not action_keys:
        return {"matches": [], "issues": []}
    return analyze_existing_locators(
        actions,
        window_ownership,
        snapshot_targets=_load_snapshot_targets(
            session_dir,
            request,
            action_keys=action_keys,
        ),
        project_root=project_root,
        user_modified_locator_files=user_modified_locator_files,
    )


def _unique_reuse_owners(window_ownership):
    result = {}
    for window in (window_ownership or {}).get("windows") or ():
        if not isinstance(window, dict):
            continue
        owner_match = window.get("owner_match") or {}
        candidates = [
            item
            for item in owner_match.get("candidates") or ()
            if isinstance(item, dict)
            and item.get("kind") == "canonical_window"
            and item.get("candidate_id")
            and item.get("root_locator_file")
            and item.get("root_locator")
        ]
        if owner_match.get("suggested_strategy") == "reuse_existing":
            strong = [
                item for item in candidates
                if item.get("strength") == "strong"
            ]
            if len(strong) == 1:
                result[str(window.get("root_name") or "")] = strong[0]
    return result


def _unique_locator_maintenance_owners(window_ownership, user_modified_locator_files):
    modified = {
        str(path or "").replace("\\", "/")
        for path in user_modified_locator_files or ()
    }
    if not modified:
        return {}
    result = {}
    for window in (window_ownership or {}).get("windows") or ():
        if not isinstance(window, dict):
            continue
        root_name = str(window.get("root_name") or "")
        owner_match = window.get("owner_match") or {}
        if owner_match.get("suggested_strategy") == "reuse_existing":
            continue
        candidates = [
            item
            for item in owner_match.get("candidates") or ()
            if isinstance(item, dict)
            and item.get("kind") == "canonical_window"
            and item.get("candidate_id")
            and str(item.get("root_locator_file") or "") in modified
            and str(item.get("root_locator") or "") == root_name
        ]
        if len(candidates) == 1:
            result[root_name] = candidates[0]
    return result


def _locator_contexts_for_action(candidate, window_ownership, step_id, action_id):
    view_bound = any(
        isinstance(item, dict)
        and item.get("kind") == "child_window_view"
        and str(item.get("step_id") or "") == str(step_id or "")
        and str(action_id or "") in {
            str(value) for value in item.get("action_ids") or ()
        }
        for item in (window_ownership or {}).get("ownership_candidates") or ()
    )
    if view_bound:
        contexts = [
            {
                "locator_file": str(view.get("locator_file") or ""),
                "root_locator": str(
                    view.get("root_locator")
                    or candidate.get("root_locator")
                    or ""
                ),
                "locator_sha256": str(view.get("locator_sha256") or ""),
            }
            for view in candidate.get("views") or ()
            if isinstance(view, dict)
            and view.get("locator_file")
        ]
        if contexts:
            return contexts
    return [{
        "locator_file": str(candidate.get("root_locator_file") or ""),
        "root_locator": str(candidate.get("root_locator") or ""),
        "locator_sha256": str(candidate.get("locator_sha256") or ""),
    }]


def _matching_locator_keys(document, *, root_locator, snapshot_target):
    target = snapshot_target.get("target") or {}
    snapshots = [
        _snapshot_root(item)
        for item in snapshot_target.get("snapshots") or ()
        if isinstance(item, dict)
    ]
    snapshots = [item for item in snapshots if item is not None]
    if not snapshots or not target:
        return []
    matches = []
    for key, raw_locator in (document.get("entries") or {}).items():
        if str(key) == root_locator or not isinstance(raw_locator, dict):
            continue
        if str(raw_locator.get("root") or "") != root_locator:
            continue
        if str(raw_locator.get("by") or "child").casefold() not in (
                _SUPPORTED_LOCATOR_METHODS
        ):
            continue
        try:
            compiled = compile_locator(raw_locator, name=str(key))
        except (TypeError, ValueError):
            continue
        locator = _snapshot_supported_locator(compiled)
        if locator is None:
            continue
        if compiled.root_name:
            locator["root"] = compiled.root_name
        if str(locator.get("by") or "child").casefold() not in (
                _SUPPORTED_LOCATOR_METHODS
        ):
            continue
        proof = _match_snapshot_target(snapshots, target, locator)
        if proof is not None:
            matches.append((str(key), proof))
    return matches


def _recorded_locator_identity_match(document, *, root_locator, key, target):
    if not _target_has_recorded_unique_locator(target):
        return None
    raw_locator = (document.get("entries") or {}).get(str(key) or "")
    if not isinstance(raw_locator, dict):
        return None
    if str(raw_locator.get("root") or "") != str(root_locator or ""):
        return None
    if str(raw_locator.get("by") or "child").casefold() != "child":
        return None
    if str(raw_locator.get("auto_id") or "") != str(target.get("auto_id") or ""):
        return None
    if str(raw_locator.get("control_type") or "") != str(target.get("control_type") or ""):
        return None
    return {
        "source": "recorded_locator_identity",
        "status": "recorded_unique_locator_match",
        "recorded_locator_validation": target.get("locator_validation"),
        "recorded_locator_strategy": target.get("locator_strategy"),
    }


def _target_has_recorded_unique_locator(target):
    return bool(
        isinstance(target, dict)
        and target.get("locator_validation") == "unique_target_match"
        and (target.get("locator_stability") or {}).get("status")
        == "single_unique"
        and target.get("locator_strategy") == "stable_auto_id"
        and target.get("auto_id")
        and target.get("control_type")
    )


def _snapshot_supported_locator(compiled):
    if compiled.prefix == "xpath":
        return {
            "by": "xpath",
            "value": str(compiled.criteria),
        }
    if compiled.prefix != "child" or not isinstance(compiled.criteria, dict):
        return None
    criteria = dict(compiled.criteria)
    if not criteria or set(criteria) - _SUPPORTED_CHILD_CRITERIA:
        return None
    if "title" in criteria:
        criteria["name"] = criteria.pop("title")
    return criteria


def _match_snapshot_target(snapshots, target, locator):
    if not _has_stable_target_identity(target):
        return None
    target_count = 0
    unique_count = 0
    for root in snapshots:
        targets = [
            item
            for item in [root, *root.descendants()]
            if _snapshot_target_matches(item, target)
        ]
        if not targets:
            continue
        if len(targets) != 1:
            return None
        try:
            matches = _snapshot_locator_matches(root, locator)
        except (LookupError, TypeError, ValueError):
            return None
        if len(matches) != 1 or matches[0] is not targets[0]:
            return None
        target_count += 1
        unique_count += 1
    if not target_count:
        return None
    return {
        "source": "complete_tree_snapshot",
        "status": (
            "cross_snapshot_unique"
            if target_count >= 2 else "single_snapshot_unique"
        ),
        "snapshot_target_count": target_count,
        "snapshot_unique_count": unique_count,
    }


def _has_stable_target_identity(target):
    return bool(
        tuple((target or {}).get("runtime_id") or ())
        or (
            str((target or {}).get("auto_id") or "")
            and str((target or {}).get("control_type") or "")
        )
    )


def _load_snapshot_targets(session_dir, request, *, action_keys):
    result = []
    for evidence in request.get("evidence") or ():
        if not isinstance(evidence, dict):
            continue
        step_id = str((evidence.get("step") or {}).get("id") or "")
        artifacts = evidence.get("artifacts") or {}
        take_path = artifacts.get("take")
        graph_path = artifacts.get("evidence_graph")
        if not step_id or not take_path or not graph_path:
            continue
        take_dir = resolve_session_path(session_dir, take_path)
        graph = _load_verified_graph(
            session_dir,
            graph_path,
            (evidence.get("artifact_hashes") or {}).get("evidence_graph"),
        )
        if graph is None:
            continue
        snapshots = _load_verified_snapshots(take_dir, graph)
        if not snapshots:
            continue
        for action in graph.get("actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("action_id") or "")
            target = (action.get("target") or {}).get("element") or {}
            if (
                    action_id
                    and target
                    and _action_key(step_id, action_id) in action_keys
            ):
                result.append({
                    "step_id": step_id,
                    "action_id": action_id,
                    "target": target,
                    "snapshots": snapshots,
                })
    return result


def _load_verified_graph(session_dir, relative_path, expected_sha256):
    try:
        path = resolve_session_path(session_dir, relative_path)
        content = path.read_bytes()
        if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
            return None
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or any((
            value.get("evidence_graph_version") != EVIDENCE_GRAPH_VERSION,
            value.get("graph_fingerprint") != _stable_hash({
                "source": (value.get("source") or {}).get(
                    "artifact_fingerprint"
                ),
                "actions": value.get("actions") or [],
                "policy": value.get("policy") or {},
            }),
    )):
        return None
    return value


def _load_verified_snapshots(take_dir, graph):
    artifacts = {
        str(item.get("path") or ""): item
        for item in ((graph.get("source") or {}).get("artifacts") or [])
        if isinstance(item, dict)
        and item.get("kind") == "ui_tree"
        and item.get("path")
    }
    snapshots = []
    for relative_path, artifact in artifacts.items():
        try:
            path = (take_dir / relative_path).resolve()
            path.relative_to(take_dir)
            content = path.read_bytes()
            if any((
                hashlib.sha256(content).hexdigest() != artifact.get("sha256"),
                len(content) != int(artifact.get("size")),
            )):
                continue
            value = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and _snapshot_root(value) is not None:
            snapshots.append(value)
        if len(snapshots) >= _MAX_SNAPSHOTS_PER_TAKE:
            break
    return snapshots


def _load_project_locator_document(project_root, relative_path):
    project_root = Path(project_root).resolve()
    locator_root = (project_root / "Bdd" / "locators").resolve()
    path = (project_root / str(relative_path or "")).resolve()
    try:
        path.relative_to(locator_root)
        content = path.read_bytes()
        entries = yaml.safe_load(content.decode("utf-8")) or {}
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(entries, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in entries.items()
    ):
        return None
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "entries": entries,
    }


def _action_key(step_id, action_id):
    step_id = str(step_id or "")
    action_id = str(action_id or "")
    return (step_id, action_id) if step_id and action_id else None


def locator_mapping_fingerprint(locator):
    if not isinstance(locator, dict):
        raise ValueError("Locator mapping必须是object")
    return _stable_hash(locator)


def _stable_hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
