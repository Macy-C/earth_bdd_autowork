from __future__ import annotations

from autowork_core.utils.debug_tools.recorder.identity import (
    locator_candidate_id as expected_locator_candidate_id,
)


TOP_LEVEL_ROOT_LOCATOR_CRITERIA_KEYS = frozenset({
    "auto_id",
    "class_name",
    "title",
    "title_re",
    "name",
})


def top_level_root_has_locator_criteria(criteria):
    criteria = criteria if isinstance(criteria, dict) else {}
    return any(
        criteria.get(key) not in (None, "", False, [], {})
        for key in TOP_LEVEL_ROOT_LOCATOR_CRITERIA_KEYS
    )


def top_level_root_requires_locator_fallback(window):
    window = window if isinstance(window, dict) else {}
    if top_level_root_has_locator_criteria(window.get("root_criteria") or {}):
        return False
    if window.get("window_identities"):
        return True
    return str(window.get("identity_status") or "") in {"resolved", "ambiguous"}


def verified_pos_locator_candidate(candidate):
    if not isinstance(candidate, dict):
        return False
    locator = candidate.get("locator") or {}
    validation = candidate.get("validation") or {}
    return all((
        str(locator.get("by") or "") == "pos",
        candidate.get("candidate_id") == expected_locator_candidate_id(
            locator,
            candidate.get("reason"),
        ),
        validation.get("status") in {"fallback", "unique"},
        validation.get("target_matches") is True,
    ))


def target_uses_pos_locator(target):
    locator = (target or {}).get("locator") or {}
    return _is_pos_locator(locator)


def selected_target_uses_pos_locator(target, locator_candidate_id=None):
    candidate_id = str(locator_candidate_id or "").strip()
    if not candidate_id:
        return target_uses_pos_locator(target)
    return any(
        str(candidate.get("candidate_id") or "") == candidate_id
        and verified_pos_locator_candidate(candidate)
        for candidate in (target or {}).get("locator_candidates") or ()
        if isinstance(candidate, dict)
    )


def top_level_root_uses_pos_only(window, selected_targets):
    return bool(
        top_level_root_requires_locator_fallback(window)
        and selected_targets
        and all(
            selected_target_uses_pos_locator(target, candidate_id)
            for target, candidate_id in selected_targets
        )
    )


def _is_pos_locator(locator):
    if not isinstance(locator, dict) or str(locator.get("by") or "") != "pos":
        return False
    coords = locator.get("coords")
    return (
        isinstance(coords, (list, tuple))
        and len(coords) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in coords)
    )


def best_verified_pos_locator_candidate(target):
    candidates = [
        item for item in (target or {}).get("locator_candidates") or ()
        if verified_pos_locator_candidate(item)
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            -int(item.get("score") or 0),
            str(item.get("candidate_id") or ""),
        ),
    )[0]