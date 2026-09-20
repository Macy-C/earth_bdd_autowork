from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.action_knowledge import (
    action_operation_qualifications,
    operation_compatibility,
)
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
    operations_for_recorded_action,
    plan_operation_names,
)
from autowork_core.utils.debug_tools.recorder.generation_design import (
    compact_generation_design_contract,
    qualify_action_relationship_source,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    operation_choice_key,
)
from autowork_core.utils.debug_tools.recorder.value_authority import (
    qualify_value_sources,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


GENERATION_TASK_BUNDLE_VERSION = "1.4"
GENERATION_TASK_FRAGMENT_VERSION = "1.4"
MAX_ACTIONS_PER_FRAGMENT = 20
MAX_FRAGMENT_BYTES = 12 * 1024
MAX_BUNDLE_INDEX_BYTES = 8 * 1024
DEFAULT_FRAGMENT_PAGE_SIZE = 12
_FRAGMENT_DEPENDENCY_SIZE_PROBE = (
    "generation-task-fragment-" + "0" * 16
)


def build_generation_task_bundle(brief, job, *, brief_path):
    brief = copy.deepcopy(dict(brief or {}))
    job = copy.deepcopy(dict(job or {}))
    steps = [
        dict(item)
        for item in (brief.get("target") or {}).get("steps") or ()
        if isinstance(item, dict) and item.get("id")
    ]
    actions = [
        dict(item)
        for item in brief.get("actions") or ()
        if isinstance(item, dict) and item.get("id") and item.get("step_id")
    ]
    _validate_scoped_action_identities(actions)
    actions_by_step = {
        str(step["id"]): sorted(
            [
                action
                for action in actions
                if str(action.get("step_id") or "") == str(step["id"])
            ],
            key=_action_order,
        )
        for step in steps
    }
    fragments = []
    previous_fragment_by_step = {}
    for step in steps:
        step_id = str(step["id"])
        cards = [
            _action_card(brief, step, action)
            for action in actions_by_step.get(step_id, ())
        ]
        for chunk in _bounded_chunks(
                brief,
                job,
                step,
                cards,
                actions_by_step.get(step_id, ()),
        ):
            dependencies = []
            previous = previous_fragment_by_step.get(step_id)
            if previous:
                dependencies.append(previous)
            fragment = _fragment_value(
                brief,
                job,
                step,
                chunk,
                actions_by_step.get(step_id, ()),
                dependency_fragment_ids=dependencies,
            )
            fragment = _seal_fragment(fragment)
            if fragment["input_bytes"] > MAX_FRAGMENT_BYTES:
                raise ValueError(
                    "Generation Task Fragment exceeds the 12 KiB limit: "
                    f"{fragment['fragment_id']}={fragment['input_bytes']}"
                )
            fragments.append(fragment)
            previous_fragment_by_step[step_id] = fragment["fragment_id"]

    fragment_refs = [
        {
            "fragment_id": fragment["fragment_id"],
            "fragment_fingerprint": fragment["fragment_fingerprint"],
            "step_ids": list(fragment["step_ids"]),
            "action_ids": list(fragment["action_ids"]),
            "action_count": fragment["action_count"],
            "input_bytes": fragment["input_bytes"],
            "dependency_fragment_ids": list(
                fragment.get("dependency_fragment_ids") or ()
            ),
            "path": (
                f"ai/generation-task-bundles/{job['job_id']}/"
                f"fragment-{fragment['fragment_fingerprint']}.json"
            ),
            "query": (
                "job-task-bundle --fragment-id "
                + fragment["fragment_id"]
            ),
        }
        for fragment in fragments
    ]
    contract = compact_generation_design_contract()
    memory = brief.get("memory_digest") or {}
    manifest = {
        "generation_task_bundle_version": GENERATION_TASK_BUNDLE_VERSION,
        "request": {
            "request_id": (job.get("request") or {}).get("request_id"),
            "request_fingerprint": (
                job.get("request") or {}
            ).get("request_fingerprint"),
            "revision_seal": (job.get("request") or {}).get("revision_seal"),
            "brief_fingerprint": brief.get("brief_fingerprint"),
        },
        "job": {
            "job_id": job.get("job_id"),
            "job_fingerprint": job.get("job_fingerprint"),
            "profile_id": (job.get("profile_lease") or {}).get("profile_id"),
            "profile_fingerprint": (
                job.get("profile_lease") or {}
            ).get("profile_fingerprint"),
            "generation_contract_lease_fingerprint": (
                job.get("generation_contract_lease") or {}
            ).get("lease_fingerprint"),
        },
        "partition_policy": {
            "max_actions_per_fragment": MAX_ACTIONS_PER_FRAGMENT,
            "max_input_bytes_per_fragment": MAX_FRAGMENT_BYTES,
            "order": "feature_step_then_action",
        },
        "design_contract": {
            "query": "design-contract",
            "fingerprint": _fingerprint(contract),
        },
        "operation_registry": {
            "query": "job-action-knowledge",
            "operation_count": len(plan_operation_names()),
            "fingerprint": _operation_registry_fingerprint(),
        },
        "memory_trace": {
            "required": bool(memory.get("items")),
            "digest_fingerprint": memory.get("digest_fingerprint"),
            "available_memory_ids": sorted(
                str(item.get("memory_id"))
                for item in memory.get("items") or ()
                if isinstance(item, dict) and item.get("memory_id")
            ),
        },
        "artifacts": {
            "brief": {
                "path": str(brief_path).replace("\\", "/"),
                "fingerprint": brief.get("brief_fingerprint"),
                "query": "job-design-context",
            },
        },
        "fragment_count": len(fragment_refs),
        "action_count": len(actions),
        "fragments": fragment_refs,
    }
    manifest["bundle_fingerprint"] = _fingerprint(manifest)
    manifest["bundle_id"] = (
        "generation-task-bundle-" + manifest["bundle_fingerprint"][:16]
    )
    return manifest, fragments


def persist_generation_task_bundle(session_dir, brief, job, *, brief_path):
    session_dir = Path(session_dir).resolve()
    manifest, fragments = build_generation_task_bundle(
        brief,
        job,
        brief_path=brief_path,
    )
    root = (
        session_dir
        / "ai"
        / "generation-task-bundles"
        / str(job.get("job_id") or "")
    )
    root.mkdir(parents=True, exist_ok=True)
    for fragment in fragments:
        _write_content_addressed(
            root / f"fragment-{fragment['fragment_fingerprint']}.json",
            fragment,
        )
    manifest_path = root / f"bundle-{manifest['bundle_fingerprint']}.json"
    manifest_path = root / (
        f"bundle-{manifest['bundle_fingerprint']}.json"
    )
    _write_content_addressed(manifest_path, manifest)
    return _bundle_pointer(session_dir, manifest_path, manifest, job)


def load_generation_task_bundle(session_dir, job, pointer=None):
    session_dir = Path(session_dir).resolve()
    if pointer:
        path = _resolve_bundle_path(session_dir, pointer.get("path"))
        value = _read_json(path)
        return (
            value
            if _bundle_pointer(session_dir, path, value, job) == pointer
            else None
        )
    root = (
        session_dir
        / "ai"
        / "generation-task-bundles"
        / str(job.get("job_id") or "")
    )
    matches = []
    for path in root.glob("bundle-*.json") if root.is_dir() else ():
        try:
            value = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
                path.name == f"bundle-{value.get('bundle_fingerprint')}.json"
                and _bundle_matches_job(value, job)
        ):
            matches.append((path, value))
    if len(matches) != 1:
        return None
    return matches[0][1]


def generation_task_bundle_pointer(session_dir, job):
    session_dir = Path(session_dir).resolve()
    bundle = load_generation_task_bundle(session_dir, job)
    if bundle is None:
        return None
    path = (
        session_dir
        / "ai"
        / "generation-task-bundles"
        / str(job.get("job_id") or "")
        / f"bundle-{bundle['bundle_fingerprint']}.json"
    )
    return _bundle_pointer(session_dir, path, bundle, job)


def load_generation_task_fragment(session_dir, bundle, fragment_id):
    session_dir = Path(session_dir).resolve()
    reference = next((
        item
        for item in bundle.get("fragments") or ()
        if str(item.get("fragment_id") or "") == str(fragment_id or "")
    ), None)
    if reference is None:
        raise KeyError(f"Generation Task Fragment not found: {fragment_id}")
    path = _resolve_bundle_path(session_dir, reference.get("path"))
    fragment = _read_json(path)
    actual_bytes = path.stat().st_size
    if any((
        path.name
        != f"fragment-{reference.get('fragment_fingerprint')}.json",
        fragment.get("fragment_id") != reference.get("fragment_id"),
        fragment.get("fragment_fingerprint")
        != reference.get("fragment_fingerprint"),
        _fingerprint({
            key: value
            for key, value in fragment.items()
            if key not in {"fragment_id", "fragment_fingerprint", "input_bytes"}
        }) != fragment.get("fragment_fingerprint"),
        fragment.get("input_bytes") != actual_bytes,
        reference.get("input_bytes") != actual_bytes,
        reference.get("step_ids") != fragment.get("step_ids"),
        reference.get("action_ids") != fragment.get("action_ids"),
        reference.get("action_count") != fragment.get("action_count"),
        reference.get("dependency_fragment_ids")
        != fragment.get("dependency_fragment_ids"),
    )):
        raise ValueError("Generation Task Fragment identity is invalid")
    return fragment


def validate_generation_task_bundle_closure(session_dir, job, bundle):
    references = list(bundle.get("fragments") or ())
    fragment_ids = [
        str(item.get("fragment_id") or "")
        for item in references
    ]
    paths = [str(item.get("path") or "") for item in references]
    if any((
        bundle.get("fragment_count") != len(references),
        bundle.get("action_count")
        != (job.get("workload") or {}).get("action_count"),
        not all(fragment_ids),
        len(fragment_ids) != len(set(fragment_ids)),
        not all(paths),
        len(paths) != len(set(paths)),
    )):
        raise ValueError("Generation Task Bundle fragment index is invalid")
    known = set(fragment_ids)
    action_identities = []
    for reference in references:
        step_ids = [str(item) for item in reference.get("step_ids") or ()]
        dependencies = [
            str(item)
            for item in reference.get("dependency_fragment_ids") or ()
        ]
        if any((
            len(step_ids) != 1,
            str(reference.get("fragment_id")) in dependencies,
            not set(dependencies) <= known,
        )):
            raise ValueError("Generation Task Bundle dependency is invalid")
        fragment = load_generation_task_fragment(
            session_dir,
            bundle,
            reference["fragment_id"],
        )
        if any((
            fragment.get("generation_task_fragment_version")
            != GENERATION_TASK_FRAGMENT_VERSION,
            (fragment.get("job") or {}).get("job_id") != job.get("job_id"),
            (fragment.get("job") or {}).get("job_fingerprint")
            != job.get("job_fingerprint"),
            (fragment.get("request") or {}).get("request_id")
            != (job.get("request") or {}).get("request_id"),
            (fragment.get("request") or {}).get("brief_fingerprint")
            != (job.get("brief") or {}).get("brief_fingerprint"),
        )):
            raise ValueError("Generation Task Fragment binding is invalid")
        action_identities.extend(
            (step_ids[0], str(action_id))
            for action_id in fragment.get("action_ids") or ()
        )
    if any((
        len(action_identities) != len(set(action_identities)),
        bundle.get("action_count") != len(action_identities),
    )):
        raise ValueError("Generation Task Bundle Action closure is invalid")
    _validate_dependency_dag(references)
    return True


def project_generation_task_bundle_index(
        bundle,
        *,
        transition,
        offset=0,
        limit=DEFAULT_FRAGMENT_PAGE_SIZE,
    ):
    references = list(bundle.get("fragments") or ())
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or DEFAULT_FRAGMENT_PAGE_SIZE), 50))
    page = []
    base = {
        "generation_task_bundle_version": bundle.get(
            "generation_task_bundle_version"
        ),
        "bundle_id": bundle.get("bundle_id"),
        "bundle_fingerprint": bundle.get("bundle_fingerprint"),
        "job_transition": copy.deepcopy(transition or {}),
        "partition_policy": copy.deepcopy(bundle.get("partition_policy") or {}),
        "memory_trace": copy.deepcopy(bundle.get("memory_trace") or {}),
        "fragment_count": bundle.get("fragment_count"),
        "action_count": bundle.get("action_count"),
        "artifacts": copy.deepcopy(bundle.get("artifacts") or {}),
        "design_contract": copy.deepcopy(bundle.get("design_contract") or {}),
        "operation_registry": copy.deepcopy(
            bundle.get("operation_registry") or {}
        ),
    }
    for reference in references[offset:offset + limit]:
        candidate = [*page, copy.deepcopy(reference)]
        probe = {
            **base,
            "fragment_page": {
                "offset": offset,
                "count": len(candidate),
                "total": len(references),
                "next_offset": offset + len(candidate),
            },
            "fragments": candidate,
        }
        if page and _encoded_size(probe) > MAX_BUNDLE_INDEX_BYTES:
            break
        page = candidate
    next_offset = offset + len(page)
    projected = {
        **base,
        "fragment_page": {
            "offset": offset,
            "count": len(page),
            "total": len(references),
            "next_offset": (
                next_offset if next_offset < len(references) else None
            ),
        },
        "fragments": page,
    }
    if _encoded_size(projected) > MAX_BUNDLE_INDEX_BYTES:
        raise ValueError("Generation Task Bundle index exceeds 8 KiB")
    return projected


def _bounded_chunks(brief, job, step, cards, source_actions):
    if not cards:
        return [[]]
    chunks = []
    current = []
    for card in cards:
        candidate = [*current, card]
        probe = _fragment_value(
            brief,
            job,
            step,
            candidate,
            source_actions,
            dependency_fragment_ids=[_FRAGMENT_DEPENDENCY_SIZE_PROBE],
        )
        if current and (
                len(candidate) > MAX_ACTIONS_PER_FRAGMENT
                or _encoded_size(_seal_fragment(probe)) > MAX_FRAGMENT_BYTES
        ):
            chunks.append(current)
            current = [card]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _fragment_value(
        brief,
        job,
        step,
        cards,
        source_actions,
        *,
        dependency_fragment_ids,
    ):
    action_ids = [str(card["action_id"]) for card in cards]
    selected = set(action_ids)
    relationships = []
    for index in range(1, len(source_actions)):
        source = source_actions[index - 1]
        consumer = source_actions[index]
        if str(consumer.get("id") or "") not in selected:
            continue
        relationships.append(
            _relationship_qualification(brief, source, consumer)
        )
    roots = {
        str(card.get("root_name") or "")
        for card in cards
        if card.get("root_name")
    }
    value_sources = {}
    for card in cards:
        for candidate in card.get("operation_candidates") or ():
            if not candidate.get("requires_value"):
                continue
            operation = str(candidate["operation"])
            value_sources.setdefault(
                operation,
                _compact_value_sources(
                    qualify_value_sources(brief, step["id"], operation)
                ),
            )
    memory = brief.get("memory_digest") or {}
    return {
        "generation_task_fragment_version": GENERATION_TASK_FRAGMENT_VERSION,
        "request": {
            "request_id": brief.get("request_id"),
            "brief_fingerprint": brief.get("brief_fingerprint"),
        },
        "job": {
            "job_id": job.get("job_id"),
            "job_fingerprint": job.get("job_fingerprint"),
        },
        "step_ids": [str(step["id"])],
        "action_ids": action_ids,
        "action_count": len(cards),
        "dependency_fragment_ids": list(dependency_fragment_ids),
        "step": {
            key: copy.deepcopy(step.get(key))
            for key in (
                "id", "keyword", "semantic_type", "text", "text_block",
                "table", "step_user_context", "observation_intents",
            )
            if step.get(key) not in (None, "", [], {})
        },
        "business_transition_context": _business_transition_context(
            brief,
            step,
        ),
        "actions": cards,
        "relationships": relationships,
        "value_source_qualification_groups": (
            _group_value_source_qualifications(value_sources)
        ),
        "window_ownership": _fragment_ownership(
            brief.get("window_ownership") or {},
            roots,
            selected,
            {str(step["id"])},
        ),
        "memory_trace": {
            "required": bool(memory.get("items")),
            "available_memory_ids": sorted(
                str(item.get("memory_id"))
                for item in memory.get("items") or ()
                if isinstance(item, dict) and item.get("memory_id")
            ),
        },
        "rules": {
            "operation_selection": (
                "AI chooses among registered operations; compatible is not "
                "runtime proof and unknown is not rejection."
            ),
            "relationship_selection": (
                "Use only relationships marked allowed or conditional and "
                "preserve Action order."
            ),
            "output": "Submit only GenerationDesign fields, never Plan proof.",
        },
    }


def _action_card(brief, step, action):
    action_type = str(action.get("type") or "")
    direct = set(operations_for_recorded_action(action_type))
    target = action.get("target") or {}
    candidates = []
    qualifications = action_operation_qualifications(action)
    for name in sorted(plan_operation_names()):
        capability = capability_by_name(name)
        assessment = operation_compatibility(name, action)
        if assessment["status"] == "incompatible":
            continue
        include = any((
            assessment["status"] == "compatible",
            name in direct,
            str(step.get("semantic_type") or "") == "then"
            and capability.category == "assertion",
            action_type == "observe"
            and capability.category in {"assertion", "scenario_state"},
        ))
        if not include:
            continue
        candidate = {
            "operation": name,
            "status": assessment["status"],
            "basis": assessment["basis"],
            "requires_value": bool(capability.requires_value_action),
            "action_type": action_type,
            "target_fingerprint": target.get("target_fingerprint"),
            "control_type": target.get("control_type"),
            "category": capability.category,
            "value_source_status": _operation_value_source_status(
                brief,
                action,
                name,
                capability.requires_value_action,
            ),
        }
        candidate["choice_key"] = operation_choice_key({
            **candidate,
            "step_id": str(action.get("step_id") or ""),
            "action_id": str(action.get("id") or ""),
            "requires_value_action": candidate["requires_value"],
        })
        candidates.append(_operation_candidate_summary(candidate))
    canonical = action.get("canonical_action") or {}
    semantics = action.get("semantics") or {}
    return {
        "step_id": str(action.get("step_id") or ""),
        "action_id": str(action.get("id") or ""),
        "ordinal": _action_order(action),
        "action_type": action_type,
        "role": action.get("role"),
        "root_name": target.get("root_name"),
        "target": {
            key: copy.deepcopy(target.get(key))
            for key in (
                "control_type", "name", "auto_id", "locator_name",
                "locator_validation", "interaction_confidence",
                "target_fingerprint",
            )
            if target.get(key) not in (None, "", [], {})
        },
        "canonical_action": {
            key: copy.deepcopy(canonical.get(key))
            for key in ("command", "observed_after", "business_expectation")
            if canonical.get(key) not in (None, "", [], {})
        },
        "effect": copy.deepcopy(semantics.get("effect") or {}),
        "operation_candidates": candidates,
        "operation_qualifications": qualifications,
    }


def _operation_candidate_summary(candidate):
    return _copy_present_fields(
        candidate,
        (
            "operation",
            "status",
            "basis",
            "requires_value",
            "category",
            "value_source_status",
            "choice_key",
        ),
    )


def _operation_value_source_status(brief, action, operation, requires_value):
    if not requires_value:
        return "forbidden"
    qualification = qualify_value_sources(
        brief,
        action.get("step_id"),
        operation,
    )
    available = [
        item for item in qualification.get("sources") or ()
        if item.get("status") == "available"
    ]
    if len(available) == 1:
        return "unique_available"
    if len(available) > 1:
        return "choice_required"
    return "unavailable"


def _relationship_qualification(brief, source, consumer):
    source_id = str(source.get("id") or "")
    consumer_id = str(consumer.get("id") or "")
    same_target = _same_target(source, consumer)
    source_qualification = qualify_action_relationship_source(
        source,
        brief=brief,
    )
    transport = copy.deepcopy(source_qualification["transport_for"])
    absorbed = copy.deepcopy(source_qualification["absorbed_by"])
    if transport["status"] != "rejected" and not same_target:
        transport = {
            "status": "rejected",
            "reason_code": "different_target",
            "reason": "Transport requires the same frozen target.",
        }
    if absorbed["status"] != "rejected" and not same_target:
        absorbed = {
            "status": "rejected",
            "reason_code": "different_target",
            "reason": "Absorption requires the same frozen target.",
        }
    return {
        "source_action_id": source_id,
        "consumer_action_id": consumer_id,
        "qualifications": {
            "activation_for": copy.deepcopy(
                source_qualification["activation_for"]
            ),
            "transport_for": transport,
            "absorbed_by": absorbed,
        },
    }


def _fragment_ownership(value, root_names, action_ids, step_ids):
    value = dict(value or {})
    step_ids = {str(step_id) for step_id in step_ids or ()}
    windows = []
    for item in value.get("windows") or ():
        if not isinstance(item, dict) or str(
                item.get("root_name") or ""
        ) not in root_names:
            continue
        projected = _fragment_window_ownership_summary(
            item,
            action_ids,
            step_ids,
        )
        windows.append(projected)
    candidates = [
        _fragment_ownership_candidate_summary(item, action_ids, step_ids)
        for item in value.get("ownership_candidates") or ()
        if isinstance(item, dict)
        and (
            str(item.get("root_name") or "") in root_names
            or str(item.get("step_id") or "") in step_ids
            or str(item.get("opener_action_id") or "") in action_ids
            or bool(
                {str(value) for value in item.get("action_ids") or ()}
                & action_ids
            )
        )
    ]
    return {
        "model": value.get("model"),
        "windows": windows,
        "ownership_candidates": candidates,
        "binding_contract": {
            "new_root_requires_business_name": True,
            "shared_view_requires_same_root_candidate_id": True,
            "view_property_requires_exact_return_type": True,
        },
    }


def _fragment_window_ownership_summary(window, action_ids, step_ids):
    projected = _copy_present_fields(
        window,
        (
            "root_name",
            "root_criteria",
            "action_types",
            "locator_names",
            "control_types",
            "identity_status",
            "window_identities",
        ),
    )
    selected_actions = [
        str(action_id)
        for action_id in window.get("action_ids") or ()
        if str(action_id) in action_ids
    ]
    if selected_actions:
        projected["action_ids"] = selected_actions
    selected_steps = [
        str(step_id)
        for step_id in window.get("step_ids") or ()
        if str(step_id) in step_ids
    ]
    if selected_steps:
        projected["step_ids"] = selected_steps
    owner_match = _fragment_owner_match_summary(
        window.get("owner_match") or {}
    )
    if owner_match:
        projected["owner_match"] = owner_match
    return projected


def _fragment_owner_match_summary(owner_match):
    owner_match = dict(owner_match or {})
    projected = _copy_present_fields(owner_match, ("suggested_strategy",))
    candidates = [
        _copy_present_fields(
            candidate,
            (
                "candidate_id",
                "kind",
                "strength",
                "score",
                "page_object",
                "page_class",
                "root_locator",
                "root_locator_file",
                "reasons",
            ),
        )
        for candidate in owner_match.get("candidates") or ()
        if isinstance(candidate, dict)
    ]
    candidates = [candidate for candidate in candidates if candidate]
    if candidates:
        projected["candidates"] = candidates
    return projected


def _fragment_ownership_candidate_summary(candidate, action_ids, step_ids):
    projected = _copy_present_fields(
        candidate,
        (
            "candidate_id",
            "kind",
            "root_name",
            "opener_action_id",
            "step_id",
            "locator_file",
            "view_object",
            "active_locator",
            "ownership_candidate_id",
            "evidence_root",
            "parent_root_name",
        ),
    )
    if projected.get("step_id") not in step_ids:
        projected.pop("step_id", None)
    action_values = [
        str(action_id)
        for action_id in candidate.get("action_ids") or ()
        if str(action_id) in action_ids
    ]
    if action_values:
        projected["action_ids"] = action_values
    return projected


def _copy_present_fields(value, fields):
    value = dict(value or {})
    return {
        key: copy.deepcopy(value.get(key))
        for key in fields
        if value.get(key) not in (None, "", [], {})
    }


def _compact_value_sources(value):
    return [
        {
            "shape": copy.deepcopy(item.get("shape") or {}),
            "status": item.get("status"),
            "basis": item.get("basis"),
        }
        for item in value.get("sources") or ()
    ]


def _group_value_source_qualifications(value_sources):
    grouped = {}
    for operation, sources in sorted(value_sources.items()):
        source_fingerprint = _fingerprint(sources)
        group = grouped.setdefault(source_fingerprint, {
            "source_fingerprint": source_fingerprint,
            "operations": [],
            "sources": copy.deepcopy(sources),
        })
        group["operations"].append(operation)
    return [grouped[key] for key in sorted(grouped)]


def _business_transition_context(brief, current_step):
    steps = [
        item
        for item in (brief.get("target") or {}).get("steps") or ()
        if isinstance(item, dict) and item.get("id")
    ]
    step_id = str(current_step.get("id") or "")
    position = next((
        index
        for index, item in enumerate(steps)
        if str(item.get("id") or "") == step_id
    ), None)
    if position is None:
        raise ValueError(f"Generation Task Fragment Step is out of scope: {step_id}")

    def summary(index):
        if index < 0 or index >= len(steps):
            return None
        item = steps[index]
        return {
            "step_id": str(item.get("id") or ""),
            "semantic_type": item.get("semantic_type"),
            "text": item.get("text"),
        }

    scenario = (brief.get("target") or {}).get("scenario") or {}
    return {
        "scenario_id": scenario.get("id"),
        "step_position": position + 1,
        "step_count": len(steps),
        "previous_step": summary(position - 1),
        "current_step": summary(position),
        "next_step": summary(position + 1),
        "state_flow_contract": {
            "design_fields": ["consumes", "produces", "observes"],
            "rule": (
                "Use the same business concept name to connect an earlier "
                "producer to a later consumer or observer; the system "
                "compiles and validates forward Scenario transitions."
            ),
        },
    }


def _validate_scoped_action_identities(actions):
    identities = [
        (str(action.get("step_id") or ""), str(action.get("id") or ""))
        for action in actions
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("Generation Task Bundle contains duplicate scoped Actions")


def _validate_dependency_dag(references):
    dependencies = {
        str(item.get("fragment_id") or ""): {
            str(value)
            for value in item.get("dependency_fragment_ids") or ()
        }
        for item in references
    }
    remaining = set(dependencies)
    resolved = set()
    while remaining:
        ready = {
            fragment_id
            for fragment_id in remaining
            if dependencies[fragment_id] <= resolved
        }
        if not ready:
            raise ValueError("Generation Task Bundle dependency cycle detected")
        resolved.update(ready)
        remaining -= ready


def _action_order(action):
    value = action.get("n")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    value = action.get("ordinal")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _same_target(source, consumer):
    left = source.get("target") or {}
    right = consumer.get("target") or {}
    return bool(
        left.get("target_fingerprint")
        and left.get("target_fingerprint") == right.get("target_fingerprint")
        and left.get("root_name") == right.get("root_name")
    )


def _bundle_matches_job(bundle, job):
    actual = _fingerprint({
        key: value
        for key, value in dict(bundle or {}).items()
        if key not in {"bundle_id", "bundle_fingerprint"}
    })
    return bool(
        isinstance(bundle, dict)
        and bundle.get("generation_task_bundle_version")
        == GENERATION_TASK_BUNDLE_VERSION
        and (bundle.get("job") or {}).get("job_id") == job.get("job_id")
        and (bundle.get("job") or {}).get("job_fingerprint")
        == job.get("job_fingerprint")
        and (bundle.get("request") or {}).get("request_id")
        == (job.get("request") or {}).get("request_id")
        and (bundle.get("request") or {}).get("request_fingerprint")
        == (job.get("request") or {}).get("request_fingerprint")
        and (bundle.get("request") or {}).get("revision_seal")
        == (job.get("request") or {}).get("revision_seal")
        and (bundle.get("request") or {}).get("brief_fingerprint")
        == (job.get("brief") or {}).get("brief_fingerprint")
        and (bundle.get("job") or {}).get("profile_fingerprint")
        == (job.get("profile_lease") or {}).get("profile_fingerprint")
        and (bundle.get("job") or {}).get(
            "generation_contract_lease_fingerprint"
        ) == (job.get("generation_contract_lease") or {}).get(
            "lease_fingerprint"
        )
        and bundle.get("bundle_fingerprint") == actual
        and bundle.get("bundle_id")
        == "generation-task-bundle-" + str(
            bundle.get("bundle_fingerprint") or ""
        )[:16]
    )


def _bundle_pointer(session_dir, path, value, job):
    if not _bundle_matches_job(value, job):
        raise ValueError("Generation Task Bundle identity is invalid")
    return {
        "path": Path(path).resolve().relative_to(Path(session_dir).resolve()).as_posix(),
        "bundle_id": value.get("bundle_id"),
        "bundle_fingerprint": value.get("bundle_fingerprint"),
        "job_id": (value.get("job") or {}).get("job_id"),
        "fragment_count": value.get("fragment_count"),
        "action_count": value.get("action_count"),
    }


def _resolve_bundle_path(session_dir, value):
    session_dir = Path(session_dir).resolve()
    path = Path(str(value or ""))
    path = path.resolve() if path.is_absolute() else (session_dir / path).resolve()
    root = (session_dir / "ai" / "generation-task-bundles").resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Generation Task Bundle path is outside its root") from error
    return path


def _write_content_addressed(path, value):
    path = Path(path)
    if path.exists():
        if _read_json(path) != value:
            raise ValueError(f"Generation Task Bundle fingerprint conflict: {path}")
        return
    write_json_atomic(path, value, compact=True)


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return value


def _operation_registry_fingerprint():
    return _fingerprint([
        {
            "operation": name,
            "category": capability_by_name(name).category,
            "requires_value": capability_by_name(name).requires_value_action,
            "required_control_types": sorted(
                capability_by_name(name).required_control_types
            ),
        }
        for name in sorted(plan_operation_names())
    ])


def _encoded_size(value):
    return len(_canonical(value).encode("utf-8"))


def _fingerprint(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _seal_fragment(value):
    fragment = copy.deepcopy(value)
    fragment["fragment_fingerprint"] = _fingerprint(fragment)
    fragment["fragment_id"] = (
        "generation-task-fragment-"
        + fragment["fragment_fingerprint"][:16]
    )
    fragment["input_bytes"] = 0
    for _attempt in range(4):
        actual_size = _encoded_size(fragment)
        if fragment["input_bytes"] == actual_size:
            return fragment
        fragment["input_bytes"] = actual_size
    raise RuntimeError("Generation Task Fragment size did not converge")


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "GENERATION_TASK_BUNDLE_VERSION",
    "GENERATION_TASK_FRAGMENT_VERSION",
    "MAX_ACTIONS_PER_FRAGMENT",
    "MAX_BUNDLE_INDEX_BYTES",
    "MAX_FRAGMENT_BYTES",
    "build_generation_task_bundle",
    "generation_task_bundle_pointer",
    "load_generation_task_bundle",
    "load_generation_task_fragment",
    "persist_generation_task_bundle",
    "project_generation_task_bundle_index",
    "validate_generation_task_bundle_closure",
]
