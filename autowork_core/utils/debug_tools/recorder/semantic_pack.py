from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.canonical_action import (
    replacement_text_candidate as _canonical_replacement_text_candidate,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.observation_repository import (
    load_observation_receipt,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


SEMANTIC_PACK_VERSION = "6.1"
SUPPORTED_SEMANTIC_PACK_VERSIONS = {
    "5.0",
    "5.1",
    "5.2",
    "5.3",
    "5.4",
    "5.5",
    "5.6",
    "5.7",
    "5.8",
    "5.9",
    "6.0",
    SEMANTIC_PACK_VERSION,
}
ASSERTION_OPERATIONS = (
    "assert_collection_equal",
    "assert_ocr_contains",
    "assert_ocr_not_contains",
    "assert_exists",
    "assert_not_exists",
    "assert_visible",
    "assert_not_visible",
    "assert_enabled",
    "assert_disabled",
    "assert_text_equal",
    "assert_text_contains",
    "assert_text_not_contains",
    "assert_text_empty",
    "assert_attr_equal",
    "assert_attr_contains",
)
_TEXT_PROPERTIES = (
    "Value.Value",
    "LegacyIAccessible.Value",
)
_DISPLAY_TEXT_PROPERTIES = (
    "element_info.rich_text",
    "wrapper.window_text",
)
_ATTRIBUTE_ASSERTION_PROPERTIES = (
    "Value.IsReadOnly",
)


def build_semantic_pack(
        take_dir,
        *,
        actions,
        events,
        locator_bundle,
        action_media,
        metadata,
        scenario=None,
        step=None,
        observation_intents=None,
        annotation_model_version=None,
        legacy_observation_notes=False,
        write_path=None,
    ):
    take_dir = Path(take_dir).resolve()
    actions = [dict(item) for item in actions or ()]
    events = [dict(item) for item in events or ()]
    scenario = dict(scenario or {})
    step = dict(step or (metadata or {}).get("step") or {})
    event_map = {
        str(event.get("id")): event
        for event in events
        if event.get("id")
    }
    media_map = {
        str(item.get("action_id")): item
        for item in (action_media or {}).get("actions") or ()
        if item.get("action_id")
    }
    locator_map = {
        str(item.get("event_id")): item
        for item in (locator_bundle or {}).get("event_targets") or ()
        if item.get("event_id")
    }
    typed_intents = [
        dict(item)
        for item in observation_intents or ()
        if isinstance(item, dict)
    ]
    intent_map = {
        str(item.get("action_id") or ""): item
        for item in typed_intents
        if item.get("action_id")
    }
    action_effects = []
    semantic_facts = []
    assertion_candidates = []
    binding_candidates = []
    intent_candidates = []
    role_candidates = []
    locator_fallback_candidates = []
    unresolved = []
    structured_observations = [
        observation
        for event in events
        for observation in [
            _structured_event_observation(event, take_dir)
        ]
        if observation is not None
    ]
    structured_observation_map = {
        str(item.get("event_id") or ""): item
        for item in structured_observations
        if item.get("event_id")
    }

    for index, action in enumerate(actions):
        action_id = str(action.get("id") or f"action-{index + 1}")
        action_events = [
            event_map[event_id]
            for event_id in (
                action.get("media_event_ids")
                or action.get("event_ids")
                or ()
            )
            if event_id in event_map
        ]
        effect = _action_effect(
            action,
            action_events,
            media_map.get(action_id) or {},
            metadata or {},
        )
        action_effects.append(effect)
        facts = _semantic_action_facts(
            action,
            action_events,
            step,
            scenario,
            observation_intent=intent_map.get(action_id),
        )
        semantic_facts.append(facts)
        binding = _binding_candidates(
            action,
            action_events,
            step,
            scenario,
        )
        if binding:
            binding_candidates.append(binding)
        intent, roles = _intent_and_role_candidates(
            action,
            effect,
            step,
            index,
            len(actions),
        )
        intent_candidates.append(intent)
        role_candidates.append(roles)
        if action.get("type") == "observe" or action.get("role") == "assertion":
            observation_intent = intent_map.get(action_id)
            if observation_intent is None and legacy_observation_notes:
                observation_intent = _legacy_observation_intent(action)
            neutral_observation = _is_neutral_observation_intent(
                observation_intent
            )
            expectation_conflict = _assertion_expectation_conflict(
                action_events,
                step,
            )
            candidates = _assertion_candidates(
                action,
                action_events,
                effect,
                step,
                scenario,
                locator_map,
                observation_intent=observation_intent,
                structured_observation=structured_observation_map.get(
                    str(next(iter(action.get("event_ids") or ()), ""))
                ),
            )
            value_gap = (
                []
                if (observation_intent or {}).get("focus") == "region_text"
                else _assertion_value_gap(
                    action,
                    action_events,
                    step,
                    scenario,
                    observation_intent=observation_intent,
                )
            )
            business_expectation_required = bool(
                observation_intent
                and observation_intent.get("authority")
                == "system_inferred_intent"
                and (
                    observation_intent.get("expected_source") or {}
                ).get("kind") == "observed_state"
                and candidates
            )
            assertion_candidates.append({
                "action_id": action_id,
                "candidates": candidates,
                "observation_intent": observation_intent,
                "resolution_authority": "ai",
                "requires_decision": (
                    False
                    if neutral_observation or expectation_conflict or value_gap
                    else _requires_assertion_decision(candidates)
                ),
            })
            if neutral_observation:
                pass
            elif expectation_conflict:
                unresolved.append({
                    "code": "assertion_expectation_conflict",
                    "action_id": action_id,
                    "blocking": True,
                    "expected_operation": expectation_conflict[
                        "operation"
                    ],
                    "expected": expectation_conflict.get("expected"),
                    "observed": expectation_conflict.get("observed"),
                    "evidence_ids": effect.get("evidence_ids") or [],
                })
            elif annotation_model_version and observation_intent is None:
                unresolved.append({
                    "code": "observation_intent_missing",
                    "action_id": action_id,
                    "blocking": True,
                    "evidence_ids": effect.get("evidence_ids") or [],
                })
            elif value_gap:
                unresolved.append({
                    "code": "assertion_value_unobserved",
                    "action_id": action_id,
                    "blocking": True,
                    "declared_expectations": value_gap,
                    "evidence_ids": effect.get("evidence_ids") or [],
                })
            elif business_expectation_required:
                unresolved.append({
                    "code": "assertion_business_expectation_required",
                    "action_id": action_id,
                    "blocking": True,
                    "observed_candidates": candidates,
                    "evidence_ids": effect.get("evidence_ids") or [],
                })
            elif (
                    (observation_intent or {}).get("focus") == "collection"
                    and not candidates
            ):
                unresolved.append({
                    "code": "collection_assertion_unsupported",
                    "action_id": action_id,
                    "blocking": True,
                    "structured_observation_available": bool(
                        (observation_intent or {}).get("event_id")
                    ),
                    "evidence_ids": effect.get("evidence_ids") or [],
                })
            elif (
                    (observation_intent or {}).get("focus") == "region_text"
                    and not candidates
            ):
                unresolved.append({
                    "code": "region_text_assertion_unsupported",
                    "action_id": action_id,
                    "blocking": True,
                    "structured_observation_available": bool(
                        (observation_intent or {}).get("event_id")
                    ),
                    "evidence_ids": effect.get("evidence_ids") or [],
                })
            elif not candidates:
                unresolved.append({
                    "code": "assertion_candidate_missing",
                    "action_id": action_id,
                    "blocking": True,
                })
        fallback = _locator_fallback_candidates(
            take_dir,
            action,
            locator_map,
            media_map.get(action_id) or {},
        )
        if fallback:
            locator_fallback_candidates.append(fallback)
            if fallback.get("pic_candidate"):
                unresolved.append({
                    "code": "pic_authorization_required",
                    "action_id": action_id,
                    "candidate_id": fallback["pic_candidate"]["candidate_id"],
                    "blocking": True,
                })

    pack = {
        "schema_version": SCHEMA_VERSION,
        "semantic_pack_version": SEMANTIC_PACK_VERSION,
        "take_id": (metadata or {}).get("id"),
        "step": step,
        "action_effects": action_effects,
        "semantic_facts": semantic_facts,
        "assertion_candidates": assertion_candidates,
        "binding_candidates": binding_candidates,
        "intent_candidates": intent_candidates,
        "role_candidates": role_candidates,
        "window_causality": _window_causality(actions, metadata or {}),
        "reuse_candidates": [],
        "locator_fallback_candidates": locator_fallback_candidates,
        "structured_observations": structured_observations,
        "observation_intents": typed_intents,
        "annotation_model_version": annotation_model_version,
        "legacy_observation_notes": bool(legacy_observation_notes),
        "unresolved_decisions": unresolved,
        "policy": {
            "raw_evidence_immutable": True,
            "pic_default": "deny",
            "pic_requires_action_authorization": True,
            "ai_may_propose_alternative_hypotheses": True,
        },
    }
    pack["semantic_fingerprint"] = _hash({
        key: value
        for key, value in pack.items()
        if key != "semantic_fingerprint"
    })
    if write_path is not None:
        write_json_atomic(write_path, pack)
    return pack


def _is_neutral_observation_intent(intent):
    intent = intent if isinstance(intent, dict) else {}
    return all((
        intent.get("authority") in {
            "system_inferred_intent",
            "user_declared_intent",
        },
        intent.get("focus") == "auto",
        intent.get("relation") == "auto",
        (intent.get("expected_source") or {}).get("kind") == "auto",
    ))


def _semantic_action_facts(
        action,
        events,
        step,
        scenario,
        *,
        observation_intent=None,
    ):
    observations = [
        observation
        for event in events
        for observation in [_event_observation(event)]
        if observation is not None
    ]
    key_sequence = [
        str((event.get("key") or {}).get("name") or "")
        for event in events
        if event.get("event_type") == "key_down"
        and (event.get("key") or {}).get("name")
    ]
    replacement_text = _replacement_text_candidate(events)
    runtime_value_sources = _runtime_value_sources(events)
    return {
        "action_id": str(action.get("id") or ""),
        "step_role": str(
            step.get("semantic_type")
            or step.get("keyword")
            or ""
        ).strip().casefold(),
        "declared_step_text": str(step.get("text") or ""),
        "example_values": dict(scenario.get("example_values") or {}),
        "observation_note": str(action.get("note") or "").strip() or None,
        "observation_intent": observation_intent,
        "key_sequence": key_sequence,
        "key_events": [
            {
                "name": str((event.get("key") or {}).get("name") or ""),
                "pressed": [
                    str(item)
                    for item in (event.get("key") or {}).get("pressed") or ()
                ],
            }
            for event in events
            if event.get("event_type") == "key_down"
            and (event.get("key") or {}).get("name")
        ],
        "replacement_text_candidate": replacement_text,
        "observed_text_values": _unique(
            observation.get("text")
            for observation in observations
            if observation.get("text") is not None
        ),
        "observed_window_titles": _unique(
            _event_window_title(event)
            for event in events
            if _event_window_title(event) is not None
        ),
        "runtime_value_sources": runtime_value_sources,
        "value_binding": action.get("value_binding"),
        "authority": {
            "declared_step_text": "feature_declared",
            "example_values": "feature_declared",
            "observation_note": "recording_annotation",
            "observation_intent": (
                (observation_intent or {}).get("authority")
                or "not_declared"
            ),
            "key_sequence": "runtime_observed",
            "key_events": "runtime_observed",
            "replacement_text_candidate": "deterministic_derivation",
            "observed_text_values": "runtime_observed",
            "observed_window_titles": "runtime_observed",
            "runtime_value_sources": "runtime_observed",
            "value_binding": "user_confirmed",
        },
    }


def _runtime_value_sources(events):
    text_available = False
    attributes = set()
    element_attributes = {
        "name": "name",
        "auto_id": "automation_id",
        "class_name": "class_name",
        "control_type": "control_type",
        "handle": "handle",
        "enabled": "enabled",
        "visible": "visible",
    }
    for event in events:
        observation = _event_observation(event)
        if observation and observation.get("text_source"):
            text_available = True
        target = event.get("target") or {}
        element = target.get("element") or {}
        for key, attr_name in element_attributes.items():
            if element.get(key) is not None:
                attributes.add(attr_name)
        for key, value in (
                target.get("element_properties") or {}
        ).items():
            normalized = str(key or "").strip().casefold()
            if value is None:
                continue
            if normalized in {"value.value", "value.isreadonly"}:
                attributes.add(normalized)
            elif normalized.startswith("legacyiaccessible."):
                attributes.add(normalized)
    return {
        "text": text_available,
        "attributes": sorted(attributes),
    }


def _structured_event_observation(event, take_dir):
    reference = (
        (event.get("details") or {}).get("structured_observation")
    )
    if not isinstance(reference, dict):
        return None
    receipt = load_observation_receipt(take_dir, reference)
    if str(receipt.get("event_id") or "") != str(event.get("id") or ""):
        raise ValueError("Observation receipt 与 canonical event 不一致")
    payload = receipt.get("payload") or {}
    items = [
        item
        for item in payload.get("items") or ()
        if isinstance(item, dict)
    ]
    return {
        "event_id": str(event.get("id") or ""),
        "provider": receipt.get("provider"),
        "provider_version": receipt.get("provider_version"),
        "status": receipt.get("status"),
        "item_names": [
            str(item.get("name") or "")
            for item in items[:200]
        ],
        "item_values": [
            item.get("value")
            for item in items[:200]
        ],
        "item_confidences": [
            item.get("confidence")
            for item in items[:200]
        ],
        "item_rectangles": [
            item.get("rectangle")
            for item in items[:200]
        ],
        "item_count": int(payload.get("item_count") or len(items)),
        "truncated": bool(payload.get("truncated")),
        "region": dict(payload.get("region") or {}),
        "target": dict(payload.get("target") or {}),
        "receipt": dict(reference),
    }


def _action_effect(action, events, media, metadata):
    action_id = str(action.get("id") or "")
    observations = [
        observation
        for event in events
        for observation in [_event_observation(event)]
        if observation
    ]
    after_state = observations[-1] if observations else {}
    changes = []
    if len(observations) >= 2:
        before = observations[0]
        after = observations[-1]
        for key in (
            "text",
            "enabled",
            "visible",
            "toggle_state",
            "selected",
            "expanded",
            "range_value",
            "range_minimum",
            "range_maximum",
        ):
            if before.get(key) != after.get(key):
                changes.append({
                    "property": key,
                    "before": before.get(key),
                    "after": after.get(key),
                    "confidence": 1.0,
                    "evidence_ids": [before["event_id"], after["event_id"]],
                })
    lifecycle = (metadata or {}).get("window_lifecycle") or []
    start_ms = action.get("start_ms")
    end_ms = action.get("end_ms")
    opened = [
        _window_summary(item)
        for item in lifecycle
        if item.get("opened_during_take")
        and _between(item.get("first_seen_ms"), start_ms, end_ms, tail=1200)
    ]
    closed = [
        _window_summary(item)
        for item in lifecycle
        if item.get("closed_during_take")
        and _between(item.get("last_seen_ms"), start_ms, end_ms, tail=1200)
    ]
    return {
        "effect_id": f"effect-{action_id}",
        "action_id": action_id,
        "changes": changes,
        "windows_opened": opened,
        "windows_closed": closed,
        "visual_stability": (media.get("stability") or {}).get("status"),
        "result": _effect_result(changes, opened, closed, media),
        "after_state": {
            key: after_state.get(key)
            for key in (
                "toggle_state",
                "selected",
                "expanded",
                "range_value",
                "range_minimum",
                "range_maximum",
            )
            if after_state.get(key) is not None
        },
        "evidence_ids": _unique(
            event.get("id") for event in events if event.get("id")
        ),
    }


def _event_observation(event):
    target = event.get("target") or {}
    element = target.get("element") or {}
    properties = target.get("element_properties") or {}
    text = None
    text_source = None
    for name in _TEXT_PROPERTIES:
        if name in properties and properties[name] is not None:
            text = properties[name]
            text_source = f"element_properties.{name}"
            break
    if text_source is None:
        for name in _DISPLAY_TEXT_PROPERTIES:
            if properties.get(name) not in (None, ""):
                text = properties[name]
                text_source = f"element_properties.{name}"
                break
    if text_source is None and element.get("value") is not None:
        text = element.get("value")
        text_source = "element.value"
    if text_source is None and element.get("name") not in (None, ""):
        text = element.get("name")
        text_source = "element.name"
    if not element and text is None:
        return None
    return {
        "event_id": event.get("id"),
        "text": text,
        "text_source": text_source,
        "enabled": element.get("enabled"),
        "visible": element.get("visible"),
        "rectangle": element.get("rectangle"),
        "runtime_id": element.get("runtime_id"),
        "toggle_state": _optional_int(
            properties.get("Toggle.ToggleState")
        ),
        "selected": _optional_bool(
            properties.get("SelectionItem.IsSelected")
        ),
        "expanded": _optional_bool(
            properties.get("ExpandCollapse.IsExpanded")
        ),
        "range_value": _optional_float(
            properties.get("RangeValue.Value")
        ),
        "range_minimum": _optional_float(
            properties.get("RangeValue.Minimum")
        ),
        "range_maximum": _optional_float(
            properties.get("RangeValue.Maximum")
        ),
    }


def _optional_bool(value):
    text = str(value or "").strip().casefold()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _optional_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _optional_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _assertion_candidates(
        action,
        events,
        effect,
        step,
        scenario,
        locator_map,
        *,
        observation_intent=None,
        structured_observation=None,
    ):
    action_id = str(action.get("id") or "")
    event_id = next(iter(action.get("event_ids") or ()), None)
    target = action.get("target") or {}
    element = target.get("element") or {}
    observation_event = next(
        (
            event
            for event in reversed(events)
            if _event_observation(event)
        ),
        {},
    )
    observation = _event_observation(observation_event) or {}
    observed_properties = (
        (observation_event.get("target") or {}).get("element_properties")
        or {}
    )
    locator_target = locator_map.get(str(event_id)) or {}
    locator_name = locator_target.get("locator_name")
    unique_locator = (
        ((locator_target.get("selected_candidate") or {}).get("validation") or {})
        .get("status") == "unique"
    )
    evidence_ids = _unique([
        *effect.get("evidence_ids", []),
        str(event_id) if event_id else None,
    ])
    expected_candidates = _expected_candidates(
        observation.get("text"),
        step,
        scenario,
        observation_intent=observation_intent,
    )
    explicit = explicit_assertion_expectation(step.get("text"))
    window_title_assertion = _window_title_assertion_candidate(
        action,
        events,
        step,
        scenario,
        locator_target,
        evidence_ids,
        observation_intent=observation_intent,
    )
    if explicit and explicit["operation"] == "assert_text_empty":
        if observation.get("text") == "" and locator_name:
            return [_assertion(
                action_id,
                "assert_text_empty",
                locator_name,
                {},
                1.0 if unique_locator else 0.72,
                evidence_ids,
                "explicit empty expectation matches F9 observation",
            )]
        return []
    candidates = []
    if (
        (observation_intent or {}).get("focus") == "collection"
        and locator_name
        and unique_locator
        and (structured_observation or {}).get("provider") == "uia_collection"
        and (structured_observation or {}).get("status") == "captured"
        and not (structured_observation or {}).get("truncated")
        and int((structured_observation or {}).get("item_count") or 0)
        == len((structured_observation or {}).get("item_names") or ())
    ):
        candidates.append(_assertion(
            action_id,
            "assert_collection_equal",
            locator_name,
            {
                "expected": list(
                    (structured_observation or {}).get("item_names") or ()
                ),
                "expected_source": (
                    f"structured_observation.{event_id}.item_names"
                ),
                "max_items": 200,
                "timeout": 5,
            },
            1.0,
            evidence_ids,
            "typed collection intent matches complete content-addressed receipt",
        ))
    candidates.extend(_canvas_region_assertion_candidates(
        action,
        events,
        step,
        scenario,
        locator_target,
        evidence_ids,
        observation_intent=observation_intent,
        structured_observation=structured_observation,
    ))
    if window_title_assertion:
        candidates.append(window_title_assertion)
    if locator_name:
        candidates.append(_assertion(
            action_id,
            "assert_exists",
            locator_name,
            {},
            0.78 if unique_locator else 0.45,
            evidence_ids,
            "target observed with executable locator",
        ))
    if locator_name and observation.get("visible") is True:
        candidates.append(_assertion(
            action_id,
            "assert_visible",
            locator_name,
            {},
            0.84 if unique_locator else 0.5,
            evidence_ids,
            "target is visible at F9 observation",
        ))
    if locator_name and observation.get("enabled") is not None:
        operation = (
            "assert_enabled"
            if observation["enabled"]
            else "assert_disabled"
        )
        candidates.append(_assertion(
            action_id,
            operation,
            locator_name,
            {},
            0.9 if unique_locator else 0.55,
            evidence_ids,
            "enabled state directly observed",
        ))
    for attr_name in _ATTRIBUTE_ASSERTION_PROPERTIES:
        expected = observed_properties.get(attr_name)
        if (
            not locator_name
            or expected is None
            or str(expected).startswith("<error:")
        ):
            continue
        candidates.append(_assertion(
            action_id,
            "assert_attr_equal",
            locator_name,
            {
                "attr_name": attr_name,
                "expected": expected,
                "expected_source": f"observed_property.{attr_name}",
                "timeout": 5,
            },
            0.88 if unique_locator else 0.56,
            evidence_ids,
            "stable executable property directly observed at F9",
        ))
    for expected in expected_candidates:
        if not locator_name or observation.get("text") is None:
            continue
        confidence = min(
            1.0,
            expected["confidence"] + (0.08 if unique_locator else 0.0),
        )
        relation = str(
            (observation_intent or {}).get("relation") or "auto"
        )
        operation = {
            "equal": "assert_text_equal",
            "contains": "assert_text_contains",
            "not_contains": "assert_text_not_contains",
        }.get(relation, "assert_text_equal")
        typed_feature_constraint = bool(
            (observation_intent or {}).get("authority")
            == "user_declared_intent"
            and (
                (observation_intent or {}).get("expected_source") or {}
            ).get("kind") == "feature"
            and expected.get("source") in {"step_text", "text_block"}
        )
        candidates.append(_assertion(
            action_id,
            operation,
            locator_name,
            {
                "expected": expected["value"],
                "expected_source": expected["source"],
                "timeout": 5,
            },
            confidence,
            evidence_ids,
            expected["reason"],
            implementation_constraint=(
                "typed_feature_assertion"
                if typed_feature_constraint
                else None
            ),
        ))
        if (
            relation == "auto"
            and str(expected["value"])
            and str(expected["value"]) != str(observation.get("text"))
        ):
            candidates.append(_assertion(
                action_id,
                "assert_text_contains",
                locator_name,
                {
                    "expected": expected["value"],
                    "expected_source": expected["source"],
                    "timeout": 5,
                },
                max(0.0, confidence - 0.18),
                evidence_ids,
                "expected token appears in observed text",
            ))
    candidates = _filter_assertion_candidates(
        candidates,
        observation_intent,
    )
    return sorted(
        candidates,
        key=lambda item: (-item["confidence"], item["operation"]),
    )


def _canvas_region_assertion_candidates(
        action,
        events,
        step,
        scenario,
        locator_target,
        evidence_ids,
        *,
        observation_intent,
        structured_observation,
    ):
    if (observation_intent or {}).get("focus") != "region_text":
        return []
    observation = structured_observation or {}
    names = [
        str(item or "").strip()
        for item in observation.get("item_names") or ()
    ]
    confidences = observation.get("item_confidences") or ()
    rectangles = observation.get("item_rectangles") or ()
    if any((
        observation.get("provider") != "canvas_ocr",
        observation.get("status") != "captured",
        observation.get("truncated"),
        int(observation.get("item_count") or 0) != len(names),
        not names,
        any(not item for item in names),
        len(confidences) != len(names),
        len(rectangles) != len(names),
        any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) < 0.9
            for value in confidences
        ),
    )):
        return []
    target = observation.get("target") or {}
    event = next((
        item
        for item in events
        if str(item.get("id") or "") == str(observation.get("event_id") or "")
    ), {})
    event_target = event.get("target") or {}
    event_element = event_target.get("element") or {}
    event_window = event_target.get("window") or {}
    target_rectangle = _validated_rectangle(target.get("rectangle"))
    selected = locator_target.get("selected_candidate") or {}
    locator = selected.get("locator") or {}
    locator_name = str(locator_target.get("locator_name") or "")
    if any((
        str(target.get("control_type") or "").casefold() != "canvas",
        not locator_name,
        (selected.get("validation") or {}).get("status") != "unique",
        (selected.get("validation") or {}).get("target_matches") is not True,
        not target_rectangle,
        _validated_rectangle(event_element.get("rectangle"))
        != target_rectangle,
        _monitor_rectangle(observation.get("region")) != target_rectangle,
        int(target.get("window_handle") or 0)
        != int(event_window.get("handle") or 0),
        str(target.get("root_name") or "")
        != str(locator.get("root") or ""),
        any(
            not _rectangle_contains(
                target_rectangle,
                _validated_rectangle(rectangle),
            )
            for rectangle in rectangles
        ),
    )):
        return []
    observed = " ".join(names)
    relation = str((observation_intent or {}).get("relation") or "auto")
    effective_relation = "contains" if relation == "auto" else relation
    if effective_relation not in {"contains", "not_contains"}:
        return []
    expectations = _expected_candidates(
        observed,
        step,
        scenario,
        observation_intent=observation_intent,
    )
    return [
        _assertion(
            str(action.get("id") or ""),
            (
                "assert_ocr_not_contains"
                if effective_relation == "not_contains"
                else "assert_ocr_contains"
            ),
            locator_name,
            {
                "expected": expected["value"],
                "expected_source": expected["source"],
                "region_source": (
                    "structured_observation."
                    f"{observation.get('event_id')}.region"
                ),
                "timeout": 5,
            },
            min(1.0, float(expected["confidence"])),
            evidence_ids,
            "typed Canvas region text matches complete bound OCR receipt",
            implementation_constraint="ocr_region_binding",
        )
        for expected in expectations
        if expected.get("source") != "observed"
    ]


def _validated_rectangle(value):
    try:
        left, top, right, bottom = [int(item) for item in value]
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _monitor_rectangle(value):
    try:
        left = int(value.get("left"))
        top = int(value.get("top"))
        width = int(value.get("width"))
        height = int(value.get("height"))
    except (AttributeError, TypeError, ValueError):
        return None
    return _validated_rectangle([left, top, left + width, top + height])


def _rectangle_contains(outer, inner):
    if not outer or not inner:
        return False
    return bool(
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _window_title_assertion_candidate(
        action,
        events,
        step,
        scenario,
        locator_target,
        evidence_ids,
        *,
        observation_intent=None,
    ):
    if (observation_intent or {}).get("focus") != "window_title":
        return None
    title = next(
        (
            value
            for event in reversed(events)
            for value in [_event_window_title(event)]
            if value is not None
        ),
        None,
    )
    root_name = str(
        (((locator_target.get("selected_candidate") or {}).get("locator") or {})
         .get("root"))
        or ""
    )
    if not title or not root_name:
        return None
    relation = str(
        (observation_intent or {}).get("relation") or "auto"
    )
    if relation == "auto":
        relation = "contains"
    expectations = _declared_assertion_expectations(
        action,
        step,
        scenario,
        observation_intent=observation_intent,
        observed=title,
    )
    matching = [
        item
        for item in expectations
        if _relation_matches(title, item.get("value"), relation)
    ]
    if not matching:
        return None
    expected = matching[0]
    return {
        **_assertion(
        str(action.get("id") or ""),
        {
            "equal": "assert_text_equal",
            "contains": "assert_text_contains",
            "not_contains": "assert_text_not_contains",
        }[relation],
        root_name,
        {
            "expected": expected["value"],
            "expected_source": expected["source"],
            "timeout": 5,
        },
        1.0,
        evidence_ids,
        "declared Examples value is directly observed in the window title",
        ),
        "subject": "window_title",
    }


def explicit_assertion_expectation(text):
    normalized = " ".join(str(text or "").casefold().split())
    rules = (
        (("should not exist", "does not exist"), "assert_not_exists"),
        (("should not be visible", "is not visible"), "assert_not_visible"),
        (("should be disabled", "is disabled"), "assert_disabled"),
        (("should be enabled", "is enabled"), "assert_enabled"),
        (("should be visible", "is visible"), "assert_visible"),
        (("should exist", "exists"), "assert_exists"),
        (("should be empty", "is empty"), "assert_text_empty"),
    )
    for phrases, operation in rules:
        if any(phrase in normalized for phrase in phrases):
            return {
                "operation": operation,
                "expected": "" if operation == "assert_text_empty" else None,
            }
    return None


def _assertion_expectation_conflict(events, step):
    expectation = explicit_assertion_expectation(step.get("text"))
    if not expectation or expectation["operation"] != "assert_text_empty":
        return None
    observation = next(
        (
            value
            for event in reversed(events)
            for value in [_event_observation(event)]
            if value is not None
        ),
        {},
    )
    observed = observation.get("text")
    if observed is None or observed == "":
        return None
    return {**expectation, "observed": observed}


def _assertion_value_gap(
        action,
        events,
        step,
        scenario,
        *,
        observation_intent=None,
    ):
    explicit = explicit_assertion_expectation(step.get("text"))
    source = (observation_intent or {}).get("expected_source") or {
        "kind": "auto"
    }
    source_kind = str(source.get("kind") or "auto")
    relation = str(
        (observation_intent or {}).get("relation") or "auto"
    )
    if relation == "auto":
        relation = "equal"
    observation = next(
        (
            value
            for event in reversed(events)
            for value in [_event_observation(event)]
            if value is not None
        ),
        {},
    )
    observed = observation.get("text")
    expectations = _declared_assertion_expectations(
        action,
        step,
        scenario,
        observation_intent=observation_intent,
        observed=observed,
    )
    if (observation_intent or {}).get("focus") == "window_title":
        relation = (
            "contains"
            if (observation_intent or {}).get("relation") == "auto"
            else relation
        )
        title = next(
            (
                value
                for event in reversed(events)
                for value in [_event_window_title(event)]
                if value is not None
            ),
            None,
        )
        expectations = _declared_assertion_expectations(
            action,
            step,
            scenario,
            observation_intent=observation_intent,
            observed=title,
        )
        if title and any(
            _relation_matches(title, item.get("value"), relation)
            for item in expectations
        ):
            return []
        if source_kind in {"examples", "data_table", "feature"}:
            if expectations:
                return expectations
            return [{
                "source": (
                    f"{source_kind}.{source.get('reference')}"
                    if source.get("reference")
                    else source_kind
                ),
                "value": None,
                "authority": "feature_declared",
                "reason": (
                    "typed window-title source is missing or cannot be resolved"
                ),
            }]
    if source_kind in {"examples", "data_table", "feature"}:
        if observed is not None and any(
                _relation_matches(observed, item.get("value"), relation)
                for item in expectations
        ):
            return []
        if expectations:
            return expectations
        return [{
            "source": (
                f"{source_kind}.{source.get('reference')}"
                if source.get("reference")
                else source_kind
            ),
            "value": None,
            "authority": "feature_declared",
            "reason": "typed expected source is missing or cannot be resolved",
        }]
    if observation.get("text") is not None:
        return []
    return expectations


def _declared_assertion_expectations(
        action,
        step,
        scenario,
        *,
        observation_intent=None,
        observed=None,
    ):
    explicit = explicit_assertion_expectation(step.get("text"))
    expectations = []
    if explicit and explicit["operation"] == "assert_text_empty":
        expectations.append({
            "source": "step_text",
            "value": "",
            "authority": "feature_declared",
        })
    source = (observation_intent or {}).get("expected_source") or {
        "kind": "auto"
    }
    source_kind = str(source.get("kind") or "auto")
    source_reference = str(source.get("reference") or "")
    if source_kind == "examples":
        value = (scenario.get("example_values") or {}).get(source_reference)
        if value is not None:
            expectations.append({
                "source": f"examples.{source_reference}",
                "value": value,
                "authority": "feature_declared",
            })
        return _dedupe_expectations(expectations)
    if source_kind == "data_table":
        table = step.get("table") or {}
        headings = list(table.get("headings") or ())
        if source_reference in headings:
            column = headings.index(source_reference)
            for row in table.get("rows") or ():
                if column < len(row):
                    expectations.append({
                        "source": f"table.{source_reference}",
                        "value": row[column],
                        "authority": "feature_declared",
                    })
        return _dedupe_expectations(expectations)
    if source_kind == "feature":
        return _dedupe_expectations([
            {
                "source": item.get("source"),
                "value": item.get("value"),
                "authority": "feature_declared",
            }
            for item in _expected_candidates(
                observed,
                step,
                scenario,
                observation_intent=observation_intent,
            )
            if item.get("source") in {"step_text", "text_block"}
        ])
    if source_kind == "observed_state":
        return _dedupe_expectations(expectations)
    declared_parts = [str(step.get("text") or "")]
    if (observation_intent or {}).get("authority") == "legacy_migrated":
        declared_parts.append(str(action.get("note") or ""))
    declared_text = " ".join(declared_parts)
    example_values = scenario.get("example_values") or {}
    example_keys = _step_example_keys(step, scenario)
    matches = [
        (key, value)
        for key, value in example_values.items()
        if str(value) and str(value) in declared_text
        and (not example_keys or key in example_keys)
    ]
    if not example_keys:
        matches = [
            (key, value)
            for key, value in matches
            if not any(
                str(value) != str(other_value)
                and str(value) in str(other_value)
                for _other_key, other_value in matches
            )
        ]
    for key, value in matches:
        expectations.append({
            "source": f"examples.{key}",
            "value": value,
            "authority": "feature_declared",
        })
    if not expectations:
        return []
    return _dedupe_expectations(expectations)


def _event_window_title(event):
    window = (event.get("target") or {}).get("window") or {}
    value = window.get("name")
    if value in (None, ""):
        value = (event.get("details") or {}).get("window_title")
    return str(value) if value not in (None, "") else None


def _step_example_keys(step, scenario):
    concrete = " ".join(str(step.get("text") or "").split())
    step_line = step.get("line")
    example_values = scenario.get("example_values") or {}
    template_steps = (
        ((scenario.get("specification") or {}).get("template") or {}).get(
            "steps"
        )
        or []
    )
    for template_step in template_steps:
        template = str(template_step.get("text") or "")
        rendered = re.sub(
            r"<([^>]+)>",
            lambda match: str(example_values.get(match.group(1), match.group(0))),
            template,
        )
        same_line = (
            step_line is not None
            and template_step.get("line") == step_line
        )
        if not same_line and " ".join(rendered.split()) != concrete:
            continue
        return {
            match.group(1)
            for match in re.finditer(r"<([^>]+)>", template)
            if match.group(1) in example_values
        }
    return set()


def _dedupe_expectations(expectations):
    result = {}
    for item in expectations:
        result[(item.get("source"), str(item.get("value")))] = item
    return list(result.values())


def _assertion(
        action_id,
        operation,
        target,
        parameters,
        confidence,
        evidence,
        reason,
        *,
        implementation_constraint=None,
    ):
    candidate_id = "assertion-" + _hash({
        "action": action_id,
        "operation": operation,
        "target": target,
        "parameters": parameters,
    })[:16]
    result = {
        "candidate_id": candidate_id,
        "action_id": action_id,
        "operation": operation,
        "target": target,
        "parameters": parameters,
        "confidence": round(float(confidence), 4),
        "evidence_ids": list(evidence),
        "reason": reason,
        "api_available": operation in ASSERTION_OPERATIONS,
    }
    if implementation_constraint:
        result["implementation_constraint"] = str(
            implementation_constraint
        )
    return result


def _expected_candidates(
        observed,
        step,
        scenario,
        *,
        observation_intent=None,
    ):
    source = (observation_intent or {}).get("expected_source") or {
        "kind": "auto"
    }
    source_kind = str(source.get("kind") or "auto")
    source_reference = str(source.get("reference") or "")
    relation = str((observation_intent or {}).get("relation") or "auto")
    if source_kind == "observed_state":
        return []
    if source_kind == "examples":
        value = (scenario.get("example_values") or {}).get(source_reference)
        return _typed_expected_candidate(
            observed,
            value,
            source=f"examples.{source_reference}",
            relation=relation,
            confidence=1.0,
            reason="typed Scenario Examples source matches F9 observation",
        )
    if source_kind == "data_table":
        table = step.get("table") or {}
        headings = list(table.get("headings") or ())
        if source_reference not in headings:
            return []
        column = headings.index(source_reference)
        candidates = []
        for row in table.get("rows") or ():
            if column >= len(row):
                continue
            candidates.extend(_typed_expected_candidate(
                observed,
                row[column],
                source=f"table.{source_reference}",
                relation=relation,
                confidence=0.96,
                reason="typed Step Data Table source matches F9 observation",
            ))
        return _dedupe_expectations(candidates)
    candidates = []
    for key, value in (scenario.get("example_values") or {}).items():
        if observed is not None and str(value) == str(observed):
            candidates.append({
                "source": f"examples.{key}",
                "value": value,
                "confidence": 1.0,
                "reason": "exact Scenario Examples value match",
            })
    table = step.get("table") or {}
    headings = table.get("headings") or []
    for row in table.get("rows") or []:
        for key, value in zip(headings, row):
            if observed is not None and str(value) == str(observed):
                candidates.append({
                    "source": f"table.{key}",
                    "value": value,
                    "confidence": 0.96,
                    "reason": "exact Step Data Table value match",
                })
    text_block = step.get("text_block")
    if observed is not None and text_block and str(text_block) == str(observed):
        candidates.append({
            "source": "text_block",
            "value": text_block,
            "confidence": 0.92,
            "reason": "exact Step text block match",
        })
    note = str(step.get("text") or "")
    if observed is not None and str(observed) and str(observed) in note:
        candidates.append({
            "source": "step_text",
            "value": observed,
            "confidence": 0.82,
            "reason": "observed value appears in Step text",
        })
    if source_kind == "feature":
        return [
            item
            for item in candidates
            if item.get("source") in {"step_text", "text_block"}
        ]
    if observed is not None and not candidates and source_kind == "auto":
        candidates.append({
            "source": "observed",
            "value": observed,
            "confidence": 0.6,
            "reason": "value observed at F9 but no declared expected source",
        })
    return candidates


def _typed_expected_candidate(
        observed,
        value,
        *,
        source,
        relation,
        confidence,
        reason,
    ):
    if value is None or observed is None:
        return []
    effective_relation = "equal" if relation == "auto" else relation
    if not _relation_matches(observed, value, effective_relation):
        return []
    return [{
        "source": source,
        "value": value,
        "confidence": confidence,
        "reason": reason,
    }]


def _relation_matches(observed, expected, relation):
    observed = str(observed if observed is not None else "")
    expected = str(expected if expected is not None else "")
    if relation == "equal":
        return observed == expected
    if relation == "contains":
        return bool(expected) and expected in observed
    if relation == "not_contains":
        return bool(expected) and expected not in observed
    return observed == expected


def _filter_assertion_candidates(candidates, observation_intent):
    focus = str((observation_intent or {}).get("focus") or "auto")
    if focus == "auto":
        return candidates
    if focus in {"text", "value"}:
        allowed = {
            "assert_text_equal",
            "assert_text_contains",
            "assert_text_not_contains",
            "assert_text_empty",
        }
        return [item for item in candidates if item.get("operation") in allowed]
    if focus == "visible":
        return [
            item
            for item in candidates
            if item.get("operation") in {"assert_visible", "assert_not_visible"}
        ]
    if focus == "enabled":
        return [
            item
            for item in candidates
            if item.get("operation") in {"assert_enabled", "assert_disabled"}
        ]
    if focus == "property":
        property_name = str(observation_intent.get("property_name") or "")
        return [
            item
            for item in candidates
            if item.get("operation") in {"assert_attr_equal", "assert_attr_contains"}
            and str((item.get("parameters") or {}).get("attr_name") or "")
            == property_name
        ]
    if focus == "window_title":
        return [
            item for item in candidates
            if item.get("subject") == "window_title"
        ]
    if focus == "collection":
        return [
            item
            for item in candidates
            if item.get("operation") == "assert_collection_equal"
        ]
    if focus == "region_text":
        return [
            item
            for item in candidates
            if item.get("operation") in {
                "assert_ocr_contains",
                "assert_ocr_not_contains",
            }
        ]
    return []


def _legacy_observation_intent(action):
    note = " ".join(str(action.get("note") or "").casefold().split())
    if "window title" not in note or "contains" not in note:
        return None
    return {
        "annotation_type": "observation_intent",
        "authority": "legacy_migrated",
        "focus": "window_title",
        "relation": "contains",
        "expected_source": {"kind": "auto", "reference": None},
        "property_name": None,
        "business_meaning": "",
    }


def _binding_candidates(action, events, step, scenario):
    if action.get("type") not in {"keyboard", "input_text"}:
        return None
    event_observations = [
        observation
        for event in events
        for observation in [_event_observation(event)]
        if observation and observation.get("text") is not None
    ]
    observed = (
        action.get("text")
        or action.get("value")
        or (
            event_observations[-1]["text"]
            if event_observations
            else None
        )
    )
    replacement_text = _replacement_text_candidate(events)
    binding = action.get("value_binding")
    candidates = []
    if binding:
        candidates.append({
            "source": str(binding),
            "value": observed,
            "confidence": 1.0,
            "reason": "explicit timeline binding",
        })
    if observed is not None:
        for expected in _expected_candidates(observed, step, scenario):
            candidates.append({
                "source": expected["source"],
                "value": expected["value"],
                "confidence": expected["confidence"],
                "reason": expected["reason"],
            })
        candidates.append({
            "source": "literal",
            "value": observed,
            "confidence": 0.3,
            "reason": "literal fallback",
        })
    elif replacement_text:
        for expected in _expected_candidates(
            replacement_text["value"],
            step,
            scenario,
        ):
            candidates.append({
                "source": expected["source"],
                "value": expected["value"],
                "confidence": expected["confidence"],
                "reason": (
                    "declared value exactly matches deterministic "
                    "replacement key sequence"
                ),
            })
        candidates.append({
            "source": "derived_key_sequence",
            "value": replacement_text["value"],
            "confidence": 0.45,
            "reason": "deterministic replacement key sequence candidate",
        })
    return {
        "action_id": action.get("id"),
        "observed_value": observed,
        "replacement_text_candidate": replacement_text,
        "resolution_authority": "ai",
        "candidates": _dedupe_candidates(candidates),
        "resolved_source": (
            candidates[0]["source"]
            if candidates
            and candidates[0]["confidence"] >= 0.9
            and (
                len(candidates) == 1
                or candidates[0]["confidence"] - candidates[1]["confidence"] >= 0.15
            )
            else None
        ),
    }


def _replacement_text_candidate(events):
    return _canonical_replacement_text_candidate(events)


def _intent_and_role_candidates(action, effect, step, index, count):
    action_id = action.get("id")
    action_type = action.get("type")
    current_role = action.get("role") or "business"
    candidates = [{
        "intent": _default_intent(action_type),
        "confidence": 0.62,
        "reason": "action type prior",
    }]
    target_element = (action.get("target") or {}).get("element") or {}
    control_type = str(target_element.get("control_type") or "").casefold()
    step_text = str(step.get("text") or "")
    selection_words = (
        " set ",
        " select ",
        " choose ",
        "设置",
        "选择",
    )
    normalized_step = f" {step_text.casefold()} "
    semantic_control = _semantic_control_candidate(
        action_type,
        control_type,
        effect,
        normalized_step,
    )
    if semantic_control is not None:
        candidates.insert(0, semantic_control)
    if (
        action_type == "click"
        and control_type == "combobox"
        and any(word in normalized_step for word in selection_words)
    ):
        value_changed = any(
            change.get("property") == "text"
            for change in effect.get("changes") or ()
        )
        candidates.insert(0, {
            "intent": "select_option",
            "recommended_operation": "select_dropdown_option",
            "confidence": 0.92 if value_changed else 0.82,
            "reason": (
                "ComboBox target and Step selection language"
            ),
            "declared_values": re.findall(r'"([^\"]+)"', step_text),
            "requires_value_evidence": not value_changed,
        })
    if effect.get("windows_opened"):
        candidates.insert(0, {
            "intent": "open_window",
            "confidence": 0.9,
            "reason": "window opened after action",
        })
    if effect.get("windows_closed"):
        candidates.insert(0, {
            "intent": "close_window",
            "confidence": 0.9,
            "reason": "window closed after action",
        })
    if action_type == "observe":
        candidates.insert(0, {
            "intent": "verify_state",
            "confidence": 0.95,
            "reason": "explicit F9 observation",
        })
    roles = [{
        "role": current_role,
        "confidence": 1.0 if action.get("role") else 0.65,
        "reason": "timeline role" if action.get("role") else "default role",
    }]
    if action_type == "observe" and current_role != "assertion":
        roles.append({
            "role": "assertion",
            "confidence": 0.9,
            "reason": "F9 observation is assertion evidence",
        })
    step_role = str(
        step.get("semantic_type") or step.get("keyword") or ""
    ).strip().casefold()
    return (
        {"action_id": action_id, "candidates": candidates},
        {
            "action_id": action_id,
            "candidates": roles,
            "resolution_authority": "ai",
        },
    )


def _semantic_control_candidate(
        action_type,
        control_type,
        effect,
        normalized_step,
    ):
    if action_type not in {"click", "drag"}:
        return None
    changes = {
        str(item.get("property")): item
        for item in effect.get("changes") or ()
        if isinstance(item, dict)
    }
    after_state = effect.get("after_state") or {}
    selection_words = (
        " select ",
        " choose ",
        " switch to ",
        " set ",
        "选择",
        "切换到",
        "设置",
    )
    selection_intent = any(
        word in normalized_step for word in selection_words
    )
    expansion_intent = any(
        word in normalized_step
        for word in (
            " expand ",
            " expanded ",
            " collapse ",
            " collapsed ",
            "展开",
            "收起",
        )
    )
    if control_type == "checkbox" and action_type == "click":
        state = (
            (changes.get("toggle_state") or {}).get("after")
            if "toggle_state" in changes
            else after_state.get("toggle_state")
        )
        candidate = {
            "intent": "set_checked_state",
            "recommended_operation": "set_checked",
            "confidence": 0.96 if state in {0, 1} else 0.8,
            "reason": "CheckBox target requires final-state semantics",
            "requires_value_evidence": state not in {0, 1},
        }
        if state in {0, 1}:
            candidate.update({
                "value": state == 1,
                "source": "observed_property.toggle_state",
            })
        return candidate
    operation_by_type = {
        "radiobutton": "select_radio",
        "tabitem": "select_tab",
        "listitem": "select_list_item",
        "dataitem": "select_list_item",
    }
    operation = operation_by_type.get(control_type)
    selected = (
        (changes.get("selected") or {}).get("after")
        if "selected" in changes
        else after_state.get("selected")
    )
    if operation and (selected is True or selection_intent):
        return {
            "intent": "select_control_item",
            "recommended_operation": operation,
            "confidence": 0.96 if selected is True else 0.82,
            "reason": "SelectionItem target requires selection semantics",
        }
    if control_type == "treeitem":
        expanded_changed = "expanded" in changes
        expanded = (
            (changes.get("expanded") or {}).get("after")
            if expanded_changed
            else after_state.get("expanded")
        )
        if (
            selected is True
            and selection_intent
            and not expansion_intent
        ):
            return {
                "intent": "select_control_item",
                "recommended_operation": "select_tree_item",
                "confidence": 0.96,
                "reason": "TreeItem SelectionItem state is frozen",
            }
        if expanded is not None and (
            expansion_intent
            or (expanded_changed and selected is not True)
        ):
            return {
                "intent": "set_tree_expansion",
                "recommended_operation": "set_tree_expanded",
                "value": expanded,
                "source": "observed_property.expanded",
                "confidence": 0.96,
                "reason": "TreeItem final ExpandCollapse state is frozen",
                "requires_value_evidence": False,
            }
        if selected is True or selection_intent:
            return {
                "intent": "select_control_item",
                "recommended_operation": "select_tree_item",
                "confidence": 0.96 if selected is True else 0.82,
                "reason": "TreeItem SelectionItem state is frozen",
            }
    if control_type == "slider":
        value = (
            (changes.get("range_value") or {}).get("after")
            if "range_value" in changes
            else after_state.get("range_value")
        )
        if value is not None:
            candidate = {
                "intent": "set_range_value",
                "recommended_operation": "set_slider_value",
                "value": value,
                "source": "observed_property.range_value",
                "confidence": 0.96,
                "reason": "Slider RangeValue change is frozen",
                "requires_value_evidence": False,
            }
            range_minimum = after_state.get("range_minimum")
            range_maximum = after_state.get("range_maximum")
            if range_minimum is not None:
                candidate["range_minimum"] = range_minimum
            if range_maximum is not None:
                candidate["range_maximum"] = range_maximum
            return candidate
    return None


def _locator_fallback_candidates(take_dir, action, locator_map, media):
    event_id = next(iter(action.get("event_ids") or ()), None)
    target = locator_map.get(str(event_id)) or {}
    selected = target.get("selected_candidate") or {}
    validation = selected.get("validation") or {}
    if validation.get("status") == "unique" and validation.get("target_matches") is True:
        return None
    candidates = [
        selected
        for locator in [selected.get("locator") or {}]
        if locator.get("by", "child") in {"child", "xpath", "ocr", "pos"}
    ]
    before = (media.get("before") or {}).get("path")
    source_monitor = (media.get("before") or {}).get("monitor")
    target_element = (action.get("target") or {}).get("element") or {}
    rectangle = target_element.get("rectangle")
    (
        region_rectangle,
        region_source,
        region_locator_name,
        region_locator,
    ) = _pic_region(target)
    pic_candidate = None
    if before and rectangle:
        source = (take_dir / before).resolve()
        try:
            source.relative_to(take_dir)
        except ValueError:
            source = None
        if source is not None and source.exists():
            pic_candidate = {
                "candidate_id": "pic-" + _hash({
                    "action": action.get("id"),
                    "source": before,
                    "rectangle": rectangle,
                })[:16],
                "source_frame": before,
                "source_monitor": source_monitor,
                "crop_rectangle": rectangle,
                "region_rectangle": region_rectangle,
                "region_source": region_source,
                "region_locator_name": region_locator_name,
                "region_locator": region_locator,
                "template_status": "draft_not_exported",
                "requires_user_authorization": True,
                "default_policy": "deny",
                "region_required": True,
                "validation_required": (
                    "cross_frame_unique_match_and_template_hash"
                ),
            }
    return {
        "action_id": action.get("id"),
        "structured_candidates": candidates,
        "pic_candidate": pic_candidate,
        "pos_candidate": next(
            (
                candidate
                for candidate in candidates
                if (candidate.get("locator") or {}).get("by") == "pos"
            ),
            None,
        ),
        "decision": "forensic_or_authorize_fallback",
    }


def _pic_region(locator_target):
    candidate = (locator_target or {}).get("pic_region_candidate") or {}
    validation = candidate.get("validation") or {}
    locator = candidate.get("locator") or {}
    if (
        _valid_rectangle(candidate.get("rectangle"))
        and candidate.get("name")
        and locator.get("by", "child") in {"child", "xpath"}
        and validation.get("status") == "unique"
        and validation.get("target_matches") is True
    ):
        return (
            candidate["rectangle"],
            "validated_parent_locator",
            candidate["name"],
            locator,
        )
    return None, None, None, None


def _valid_rectangle(value):
    return bool(
        isinstance(value, (list, tuple))
        and len(value) == 4
        and value[2] > value[0]
        and value[3] > value[1]
    )


def _window_causality(actions, metadata):
    result = []
    for lifecycle in (metadata or {}).get("window_lifecycle") or []:
        first_seen = lifecycle.get("first_seen_ms")
        last_seen = lifecycle.get("last_seen_ms")
        opened_by = (
            _nearest_action(actions, first_seen)
            if lifecycle.get("opened_during_take")
            else None
        )
        closed_by = (
            _nearest_action(actions, last_seen)
            if lifecycle.get("closed_during_take")
            else None
        )
        result.append({
            "window": _window_summary(lifecycle),
            "opened_during_take": bool(lifecycle.get("opened_during_take")),
            "closed_during_take": bool(lifecycle.get("closed_during_take")),
            "opened_by_action_id": (
                opened_by.get("id") if opened_by else None
            ),
            "closed_by_action_id": (
                closed_by.get("id") if closed_by else None
            ),
            "confidence": 0.75 if opened_by or closed_by else 0.4,
        })
    return result


def _nearest_action(actions, timestamp, threshold=1500):
    if timestamp is None:
        return None
    candidates = []
    for action in actions:
        end_ms = action.get("end_ms")
        if end_ms is None or end_ms > timestamp:
            continue
        distance = timestamp - end_ms
        if distance <= threshold:
            candidates.append((distance, action))
    return min(candidates, default=(None, None), key=lambda item: item[0])[1]


def _window_summary(value):
    return {
        "handle": value.get("handle"),
        "process_id": value.get("process_id"),
        "title": value.get("title"),
        "class_name": value.get("class_name"),
    }


def _effect_result(changes, opened, closed, media):
    if closed:
        return "window_closed"
    if opened:
        return "window_opened"
    if changes:
        return "properties_changed"
    if (media.get("stability") or {}).get("status") == "visual_still_changing":
        return "visual_transition_incomplete"
    return "no_semantic_change_observed"


def _default_intent(action_type):
    return {
        "click": "activate_target",
        "double_click": "open_target",
        "right_click": "open_context_menu",
        "keyboard": "enter_or_send_value",
        "input_text": "enter_value",
        "observe": "verify_state",
        "focus": "focus_target",
        "scroll": "navigate_content",
        "drag": "move_or_select_range",
    }.get(action_type, "perform_ui_action")


def _requires_assertion_decision(candidates):
    if not candidates:
        return True
    if len(candidates) == 1:
        return candidates[0]["confidence"] < 0.9
    return candidates[0]["confidence"] - candidates[1]["confidence"] < 0.15


def _between(value, start, end, tail=0):
    if value is None or start is None or end is None:
        return False
    return start <= value <= end + tail


def _dedupe_candidates(candidates):
    result = {}
    for candidate in candidates:
        key = (candidate.get("source"), str(candidate.get("value")))
        current = result.get(key)
        if current is None or candidate.get("confidence", 0) > current.get(
                "confidence", 0
        ):
            result[key] = candidate
    return sorted(
        result.values(),
        key=lambda item: (-item.get("confidence", 0), item.get("source", "")),
    )


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _hash(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()