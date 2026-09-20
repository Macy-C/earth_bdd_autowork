from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import yaml


GENERATION_PRODUCT_STATE_VERSION = "1.0"
_PUBLIC_LOCATOR_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TECHNICAL_LOCATOR_SUFFIXES = (
    "_by_name",
    "_by_class",
    "_by_event_chain",
    "_by_ancestor",
    "_by_sibling",
    "_xpath",
    "_ocr",
    "_pos",
    "_index",
)
_MATCH_FIELDS = (
    "auto_id",
    "name",
    "title",
    "class_name",
)
_GENERIC_PRODUCT_TERMS = frozenset({
    "action",
    "actions",
    "button",
    "class",
    "control",
    "feature",
    "file",
    "locators",
    "menu",
    "menuitem",
    "page",
    "root",
    "scenario",
    "step",
    "window",
    "yaml",
})


def related_product_locator_paths(project_root, brief):
    project_root = Path(project_root).resolve()
    locator_root = project_root / "Bdd" / "locators"
    if not locator_root.is_dir():
        return []
    action_terms = _action_product_terms(brief)
    if not action_terms:
        return []
    paths = []
    for path in sorted(locator_root.rglob("*.y*ml")):
        try:
            relative = path.relative_to(project_root).as_posix()
        except ValueError:
            continue
        normalized = relative.casefold().replace("\\", "/")
        parts = normalized.split("/")
        if any(term and term in normalized for term in action_terms):
            paths.append(relative)
            continue
        if len(parts) >= 3 and parts[0] == "bdd" and parts[1] == "locators":
            asset_scope = parts[2]
            if any(_scope_terms_overlap(asset_scope, term) for term in action_terms):
                paths.append(relative)
    return sorted(set(paths))


def resolve_product_state_facts(brief, source_files):
    state = product_state_from_source_files(source_files)
    target_names = {}
    locator_identities = []
    for action in brief.get("actions") or ():
        if not isinstance(action, dict) or action.get("role") == "noise":
            continue
        step_id = str(action.get("step_id") or "")
        action_id = str(action.get("id") or action.get("action_id") or "")
        if not step_id or not action_id:
            continue
        match = _unique_locator_match(state["existing_assets"]["locators"], action)
        if match is None:
            continue
        target_names[(step_id, action_id)] = match["locator_key"]
        locator_identities.append(match)
    product_state = {
        "product_state_version": GENERATION_PRODUCT_STATE_VERSION,
        "facts": {
            "existing_locator_identities": locator_identities,
        },
        "derived_baseline_inputs": {
            "target_names": target_names,
        },
    }
    return {
        "product_state": product_state,
        "naming_overrides": _naming_overrides_from_target_names(
            product_state,
        ),
    }


def existing_locator_name_overrides(brief, source_files):
    return resolve_product_state_facts(brief, source_files)["naming_overrides"]


def product_state_from_source_files(source_files):
    locators = []
    for source in source_files or ():
        if not isinstance(source, dict):
            continue
        path = str(source.get("path") or "").replace("\\", "/")
        if not path.startswith("Bdd/locators/"):
            continue
        if Path(path).suffix.casefold() not in {".yaml", ".yml"}:
            continue
        if source.get("is_symlink") is True:
            continue
        content = _source_text(source)
        if content is None:
            continue
        try:
            value = yaml.safe_load(content) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(value, dict):
            continue
        document_hash = str(source.get("sha256") or "") or _hash_text(content)
        for key, locator in value.items():
            if not isinstance(key, str) or not isinstance(locator, dict):
                continue
            if not _is_public_locator_name(key):
                continue
            locators.append({
                "locator_key": key,
                "locator_file": path,
                "locator_sha256": document_hash,
                "locator": deepcopy(locator),
            })
    return {
        "product_state_version": GENERATION_PRODUCT_STATE_VERSION,
        "existing_assets": {"locators": locators},
    }


def _naming_overrides_from_target_names(product_state):
    target_names = (
        (product_state.get("derived_baseline_inputs") or {}).get(
            "target_names"
        ) or {}
    )
    if not target_names:
        return {}
    return {
        "target_names": dict(target_names),
        "product_state": deepcopy(product_state),
    }


def _unique_locator_match(locators, action):
    target = action.get("target") or {}
    matches = []
    for item in locators:
        locator = item.get("locator") or {}
        if not _locator_matches_target(locator, target):
            continue
        matches.append({
            "step_id": str(action.get("step_id") or ""),
            "action_id": str(action.get("id") or action.get("action_id") or ""),
            "locator_key": item["locator_key"],
            "locator_file": item["locator_file"],
            "locator_sha256": item["locator_sha256"],
            "match_fields": _matching_fields(locator, target),
            "root_name": str(target.get("root_name") or ""),
            "target_fingerprint": str(target.get("target_fingerprint") or ""),
        })
    unique_keys = {(match["locator_file"], match["locator_key"]) for match in matches}
    if len(unique_keys) != 1:
        return None
    return matches[0]


def _locator_matches_target(locator, target):
    if not isinstance(locator, dict) or not isinstance(target, dict):
        return False
    target_control_type = str(target.get("control_type") or "")
    if target_control_type and str(locator.get("control_type") or "") != target_control_type:
        return False
    return bool(_matching_fields(locator, target))


def _matching_fields(locator, target):
    fields = []
    target_values = dict(target)
    if "locator_name" in target_values and "name" not in target_values:
        locator_name = str(target_values.get("locator_name") or "")
        if locator_name.endswith("_by_name"):
            target_values["name"] = locator_name[:-8]
    for field in _MATCH_FIELDS:
        expected = str(target_values.get(field) or "").strip()
        actual = str(locator.get(field) or "").strip()
        if expected and actual and expected == actual:
            fields.append(field)
    return fields


def _action_product_terms(brief):
    terms = set()
    feature = (brief.get("target") or {}).get("feature") or {}
    for value in (
            feature.get("name"),
            feature.get("id"),
            Path(str(feature.get("source_relpath") or "")).stem,
    ):
        terms.update(_token_terms(value))
    windows = (brief.get("window_ownership") or {}).get("windows") or ()
    for window in windows:
        if not isinstance(window, dict):
            continue
        root_criteria = window.get("root_criteria") or {}
        for value in (
                window.get("root_name"),
                root_criteria.get("class_name"),
                root_criteria.get("title"),
        ):
            terms.update(_token_terms(value))
    for action in brief.get("actions") or ():
        if not isinstance(action, dict):
            continue
        target = action.get("target") or {}
        for value in (
                target.get("root_name"),
                target.get("locator_name"),
                target.get("class_name"),
                target.get("name"),
                target.get("auto_id"),
        ):
            terms.update(_token_terms(value))
    return {
        term for term in terms
        if len(term) >= 3 and term not in _GENERIC_PRODUCT_TERMS
    }


def _token_terms(value):
    text = str(value or "").casefold().replace("\\", "/")
    if not text:
        return set()
    raw = re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", text)
    terms = set()
    for item in raw:
        item = item.strip("_")
        if not item:
            continue
        terms.add(item)
        for part in item.split("_"):
            part = part.strip()
            if part:
                terms.add(part)
    return terms


def _scope_terms_overlap(asset_scope, term):
    scope_terms = _token_terms(asset_scope)
    term_tokens = _token_terms(term)
    return bool(scope_terms and term_tokens and scope_terms & term_tokens)


def _source_text(source):
    if source.get("content_encoding") == "utf-8" and isinstance(source.get("content"), str):
        return source["content"]
    return None


def _hash_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _is_public_locator_name(value):
    value = str(value or "")
    if not _PUBLIC_LOCATOR_NAME.fullmatch(value):
        return False
    return not value.endswith(_TECHNICAL_LOCATOR_SUFFIXES)
