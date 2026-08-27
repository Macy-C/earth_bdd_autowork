from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION


ANNOTATION_VERSION = "2.0"
SUPPORTED_ANNOTATION_VERSIONS = {ANNOTATION_VERSION}
ANNOTATION_MODEL_VERSION = "3.0"
ANNOTATION_SNAPSHOT_VERSION = "1.0"
STEP_USER_CONTEXT = "step_user_context"
OBSERVATION_INTENT = "observation_intent"
USER_DECLARED_CONTEXT = "user_declared_context"
USER_DECLARED_INTENT = "user_declared_intent"
SYSTEM_INFERRED_INTENT = "system_inferred_intent"
MAX_STEP_CONTEXT_TEXT_LENGTH = 2000
MAX_OBSERVATION_MEANING_LENGTH = 1000
OBSERVATION_FOCUSES = {
    "auto",
    "text",
    "value",
    "visible",
    "enabled",
    "property",
    "window_title",
    "collection",
    "region_text",
}
OBSERVATION_RELATIONS = {
    "auto",
    "equal",
    "contains",
    "not_contains",
}
EXPECTED_SOURCE_KINDS = {
    "auto",
    "feature",
    "examples",
    "data_table",
    "observed_state",
}


class AnnotationRevisionConflict(RuntimeError):
    def __init__(self, step_id, expected_revision, current_revision):
        self.step_id = str(step_id)
        self.expected_revision = int(expected_revision)
        self.current_revision = int(current_revision)
        super().__init__(
            "Step业务说明已被其他窗口更新: "
            f"step={self.step_id}, expected={self.expected_revision}, "
            f"current={self.current_revision}"
        )


class RecordingAnnotationRepository:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir).resolve()
        self.path = self.session_dir / "recording-annotations.jsonl"

    def load(self):
        if not self.path.exists():
            return []
        records = []
        for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(),
                start=1,
        ):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"annotation记录不是有效JSON: line={line_number}"
                ) from error
            records.append(record)
        _validate_records(records)
        return records

    def current_step_context(self, step_id):
        step_id = str(step_id or "").strip()
        current = None
        for record in self.load():
            if (
                    record.get("annotation_type") == STEP_USER_CONTEXT
                    and record.get("step_id") == step_id
            ):
                current = record
        return _public_context(current) if current is not None else None

    def step_context_revision(self, step_id):
        step_id = str(step_id or "").strip()
        context = self.current_step_context(step_id)
        value = context or {
            "annotation_version": ANNOTATION_VERSION,
            "annotation_type": STEP_USER_CONTEXT,
            "step_id": step_id,
            "authority": USER_DECLARED_CONTEXT,
            "revision": 0,
            "active": False,
        }
        return {
            "step_id": step_id,
            "revision": int(value.get("revision") or 0),
            "annotation_id": value.get("annotation_id"),
            "fingerprint": stable_digest(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                length=32,
            ),
        }

    def append_step_user_context(
            self,
            step_id,
            *,
            business_context=None,
            purpose=None,
            constraints=None,
            expected_revision,
        ):
        step_id = str(step_id or "").strip()
        if not step_id:
            raise ValueError("Step业务说明必须绑定step_id")
        business_context = _normalize_business_context_input(
            business_context,
            purpose=purpose,
            constraints=constraints,
        )
        if len(business_context) > MAX_STEP_CONTEXT_TEXT_LENGTH:
            raise ValueError("Step业务补充超过2000字符")
        records = self.load()
        previous = next((
            record
            for record in reversed(records)
            if (
                record.get("annotation_type") == STEP_USER_CONTEXT
                and record.get("step_id") == step_id
            )
        ), None)
        current_revision = int((previous or {}).get("revision") or 0)
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise ValueError("Step业务说明expected_revision必须是整数") from error
        if expected_revision != current_revision:
            raise AnnotationRevisionConflict(
                step_id,
                expected_revision,
                current_revision,
            )

        created_at = datetime.now().isoformat(timespec="milliseconds")
        revision = current_revision + 1
        record = {
            "schema_version": SCHEMA_VERSION,
            "annotation_version": ANNOTATION_VERSION,
            "annotation_type": STEP_USER_CONTEXT,
            "annotation_id": _annotation_id(
                created_at,
                step_id,
                revision,
                business_context,
            ),
            "created_at": created_at,
            "step_id": step_id,
            "authority": USER_DECLARED_CONTEXT,
            "revision": revision,
            "supersedes": (previous or {}).get("annotation_id"),
            "active": bool(business_context),
            "business_context": business_context,
        }
        _validate_records([*records, record])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return _public_context(record)

    def current_observation_intent(self, step_id, take_id, event_id):
        scope = _observation_scope(step_id, take_id, event_id)
        current = None
        for record in self.load():
            if (
                    record.get("annotation_type") == OBSERVATION_INTENT
                    and _record_observation_scope(record) == scope
            ):
                current = record
        return _public_observation_intent(current) if current else None

    def observation_intent_revision(self, step_id, take_id, event_id):
        scope = _observation_scope(step_id, take_id, event_id)
        intent = self.current_observation_intent(*scope)
        return {
            "step_id": scope[0],
            "take_id": scope[1],
            "event_id": scope[2],
            "revision": int((intent or {}).get("revision") or 0),
            "annotation_id": (intent or {}).get("annotation_id"),
        }

    def current_observation_intents(self, *, step_id=None, take_id=None):
        current = {}
        for record in self.load():
            if record.get("annotation_type") != OBSERVATION_INTENT:
                continue
            scope = _record_observation_scope(record)
            if step_id is not None and scope[0] != str(step_id):
                continue
            if take_id is not None and scope[1] != str(take_id):
                continue
            current[scope] = record
        return [
            _public_observation_intent(record)
            for _scope, record in sorted(current.items())
        ]

    def project_observation_intents(
            self,
            step_id,
            take_id,
            actions,
        ):
        event_actions = {}
        for action in actions or ():
            if str(action.get("type") or "") != "observe":
                continue
            action_id = str(action.get("id") or "").strip()
            for event_id in action.get("event_ids") or ():
                event_id = str(event_id or "").strip()
                if not action_id or not event_id:
                    continue
                previous = event_actions.get(event_id)
                if previous is not None and previous != action_id:
                    raise ValueError(
                        "Observation event映射到多个Action: "
                        f"event={event_id}"
                    )
                event_actions[event_id] = action_id
        result = []
        for intent in self.current_observation_intents(
                step_id=step_id,
                take_id=take_id,
        ):
            action_id = event_actions.get(intent["event_id"])
            if action_id is None:
                continue
            result.append({
                **intent,
                "action_id": action_id,
            })
        return result

    def append_observation_intent(
            self,
            step_id,
            take_id,
            event_id,
            *,
            focus="auto",
            relation="auto",
            expected_source=None,
            property_name=None,
            business_meaning="",
            authority=USER_DECLARED_INTENT,
            expected_revision,
        ):
        scope = _observation_scope(step_id, take_id, event_id)
        focus = str(focus or "auto").strip().casefold()
        relation = str(relation or "auto").strip().casefold()
        expected_source = _normalize_expected_source(expected_source)
        property_name = str(property_name or "").strip() or None
        business_meaning = str(business_meaning or "").strip()
        authority = str(authority or "").strip()
        if authority not in {
            USER_DECLARED_INTENT,
            SYSTEM_INFERRED_INTENT,
        }:
            raise ValueError(f"ObservationIntent authority无效: {authority}")
        if focus not in OBSERVATION_FOCUSES:
            raise ValueError(f"未知Observation focus: {focus}")
        if relation not in OBSERVATION_RELATIONS:
            raise ValueError(f"未知Observation relation: {relation}")
        if focus == "property" and not property_name:
            raise ValueError("property focus必须提供property_name")
        if focus != "property" and property_name:
            raise ValueError("只有property focus可以提供property_name")
        _validate_observation_combination(
            focus,
            relation,
            expected_source,
        )
        if len(business_meaning) > MAX_OBSERVATION_MEANING_LENGTH:
            raise ValueError("Observation业务含义超过1000字符")
        records = self.load()
        previous = next((
            record
            for record in reversed(records)
            if (
                record.get("annotation_type") == OBSERVATION_INTENT
                and _record_observation_scope(record) == scope
            )
        ), None)
        current_revision = int((previous or {}).get("revision") or 0)
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise ValueError("Observation expected_revision必须是整数") from error
        if expected_revision != current_revision:
            raise AnnotationRevisionConflict(
                "/".join(scope),
                expected_revision,
                current_revision,
            )
        revision = current_revision + 1
        created_at = datetime.now().isoformat(timespec="milliseconds")
        payload = {
            "step_id": scope[0],
            "take_id": scope[1],
            "event_id": scope[2],
            "focus": focus,
            "relation": relation,
            "expected_source": expected_source,
            "property_name": property_name,
            "business_meaning": business_meaning,
        }
        record = {
            "schema_version": SCHEMA_VERSION,
            "annotation_version": ANNOTATION_VERSION,
            "annotation_type": OBSERVATION_INTENT,
            "annotation_id": _observation_intent_id(
                created_at,
                revision,
                payload,
                authority=authority,
            ),
            "created_at": created_at,
            **payload,
            "authority": authority,
            "revision": revision,
            "supersedes": (previous or {}).get("annotation_id"),
        }
        _validate_records([*records, record])
        self._append_record(record)
        return _public_observation_intent(record)

    def _append_record(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def build_annotation_snapshot(target_steps):
    references = []
    for step in target_steps or ():
        if not isinstance(step, dict):
            continue
        target_step_id = str(step.get("id") or "").strip()
        context = step.get("step_user_context")
        if isinstance(context, dict):
            references.append(_annotation_reference(
                context,
                target_step_id=target_step_id,
            ))
        for intent in step.get("observation_intents") or ():
            if not isinstance(intent, dict):
                continue
            references.append(_annotation_reference(
                intent,
                target_step_id=target_step_id,
            ))
    references.sort(key=lambda item: (
        item["scope"]["step_id"],
        item["annotation_type"],
        item["annotation_id"],
    ))
    annotation_ids = [item["annotation_id"] for item in references]
    if len(annotation_ids) != len(set(annotation_ids)):
        raise ValueError("Annotation snapshot包含重复annotation_id")
    required_by_step = _required_annotation_ids_by_step(references)
    value = {
        "annotation_snapshot_version": ANNOTATION_SNAPSHOT_VERSION,
        "references": references,
        "required_annotation_ids_by_step": required_by_step,
    }
    value["snapshot_fingerprint"] = _snapshot_fingerprint(value)
    return value


def annotation_snapshot_is_valid(snapshot):
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("annotation_snapshot_version") != ANNOTATION_SNAPSHOT_VERSION:
        return False
    references = snapshot.get("references")
    if not isinstance(references, list):
        return False
    if not all(
            _annotation_reference_is_valid(reference)
            for reference in references
    ):
        return False
    expected_order = sorted(references, key=lambda item: (
        item["scope"]["step_id"],
        item["annotation_type"],
        item["annotation_id"],
    ))
    annotation_ids = [item["annotation_id"] for item in references]
    if references != expected_order or len(annotation_ids) != len(set(annotation_ids)):
        return False
    expected_required = _required_annotation_ids_by_step(references)
    return all((
        expected_required == snapshot.get("required_annotation_ids_by_step"),
        _snapshot_fingerprint(snapshot) == snapshot.get("snapshot_fingerprint"),
    ))


def current_annotation_snapshot_for_request(session_dir, request):
    session_dir = Path(session_dir).resolve()
    evidence_by_step = {
        str((item.get("step") or {}).get("id") or ""): item
        for item in request.get("evidence") or ()
        if isinstance(item, dict)
    }
    repository = RecordingAnnotationRepository(session_dir)
    target_steps = []
    for source_step in (request.get("target") or {}).get("steps") or ():
        if not isinstance(source_step, dict):
            continue
        step_id = str(source_step.get("id") or "").strip()
        target_step = {"id": step_id, "observation_intents": []}
        context = repository.current_step_context(step_id)
        if context is not None:
            target_step["step_user_context"] = context
        evidence = evidence_by_step.get(step_id) or {}
        selected_take = source_step.get("selected_take") or {}
        take_id = str(selected_take.get("id") or "").strip()
        take_path = str(
            selected_take.get("path")
            or (evidence.get("artifacts") or {}).get("take")
            or ""
        )
        actions = _request_effective_actions(
            session_dir,
            evidence,
        )
        if take_id:
            target_step["observation_intents"].extend(
                repository.project_observation_intents(
                    step_id,
                    take_id,
                    actions,
                )
            )
        if take_path:
            take_dir = _contained_path(session_dir, take_path)
            supplement_ids = sorted({
                str((action.get("source") or {}).get("supplement_id") or "")
                for action in actions
                if (action.get("source") or {}).get("kind") == "supplement"
                and (action.get("source") or {}).get("supplement_id")
            })
            for supplement_id in supplement_ids:
                supplement_dir = _contained_path(
                    take_dir,
                    Path("supplements") / supplement_id,
                )
                target_step["observation_intents"].extend(
                    RecordingAnnotationRepository(
                        supplement_dir
                    ).project_observation_intents(
                        step_id,
                        supplement_id,
                        actions,
                    )
                )
        target_steps.append(target_step)
    return build_annotation_snapshot(target_steps)


def _annotation_reference(annotation, *, target_step_id):
    annotation_type = str(annotation.get("annotation_type") or "")
    annotation_id = str(annotation.get("annotation_id") or "").strip()
    step_id = str(annotation.get("step_id") or "").strip()
    authority = str(annotation.get("authority") or "").strip()
    revision = int(annotation.get("revision") or 0)
    if not annotation_id or not target_step_id or step_id != target_step_id:
        raise ValueError(
            "Annotation snapshot Step scope不匹配: "
            f"target={target_step_id}, annotation={step_id}"
        )
    if revision < 1:
        raise ValueError(f"Annotation revision无效: {annotation_id}")
    if annotation_type == STEP_USER_CONTEXT:
        if authority != USER_DECLARED_CONTEXT:
            raise ValueError(f"StepUserContext authority无效: {annotation_id}")
        scope = {
            "step_id": step_id,
            "take_id": None,
            "event_id": None,
            "action_id": None,
        }
        active = bool(annotation.get("active"))
    elif annotation_type == OBSERVATION_INTENT:
        if authority not in {
            USER_DECLARED_INTENT,
            SYSTEM_INFERRED_INTENT,
        }:
            raise ValueError(f"ObservationIntent authority无效: {annotation_id}")
        scope = {
            "step_id": step_id,
            "take_id": str(annotation.get("take_id") or "").strip() or None,
            "event_id": str(annotation.get("event_id") or "").strip() or None,
            "action_id": str(annotation.get("action_id") or "").strip() or None,
        }
        if not all(scope[key] for key in ("take_id", "event_id", "action_id")):
            raise ValueError(f"ObservationIntent scope不完整: {annotation_id}")
        active = True
    else:
        raise ValueError(f"Annotation type无效: {annotation_type}")
    return {
        "annotation_id": annotation_id,
        "annotation_type": annotation_type,
        "authority": authority,
        "revision": revision,
        "active": active,
        "scope": scope,
        "content_fingerprint": _annotation_content_fingerprint(annotation),
    }


def _annotation_content_fingerprint(annotation):
    value = {
        key: item
        for key, item in annotation.items()
        if key not in {"created_at", "action_id"}
    }
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _required_annotation_ids_by_step(references):
    result = {}
    for reference in references:
        if (
                reference["annotation_type"] == STEP_USER_CONTEXT
                and not reference.get("active")
        ):
            continue
        result.setdefault(reference["scope"]["step_id"], []).append(
            reference["annotation_id"]
        )
    return {
        step_id: sorted(annotation_ids)
        for step_id, annotation_ids in sorted(result.items())
    }


def _annotation_reference_is_valid(reference):
    if not isinstance(reference, dict):
        return False
    annotation_id = str(reference.get("annotation_id") or "")
    annotation_type = str(reference.get("annotation_type") or "")
    authority = str(reference.get("authority") or "")
    scope = reference.get("scope")
    fingerprint = str(reference.get("content_fingerprint") or "")
    try:
        revision = int(reference.get("revision") or 0)
    except (TypeError, ValueError):
        return False
    if (
            not annotation_id
            or revision < 1
            or not isinstance(scope, dict)
            or not str(scope.get("step_id") or "")
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        return False
    if annotation_type == STEP_USER_CONTEXT:
        return bool(
            authority == USER_DECLARED_CONTEXT
            and all(
                scope.get(key) is None
                for key in ("take_id", "event_id", "action_id")
            )
            and isinstance(reference.get("active"), bool)
        )
    if annotation_type == OBSERVATION_INTENT:
        return bool(
            authority in {
                USER_DECLARED_INTENT,
                SYSTEM_INFERRED_INTENT,
            }
            and reference.get("active") is True
            and all(
                str(scope.get(key) or "")
                for key in ("take_id", "event_id", "action_id")
            )
        )
    return False


def _snapshot_fingerprint(value):
    return hashlib.sha256(json.dumps(
        {
            key: item
            for key, item in value.items()
            if key != "snapshot_fingerprint"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _request_effective_actions(session_dir, evidence):
    relative = str(
        (evidence.get("artifacts") or {}).get("actions_effective")
        or ""
    )
    if not relative:
        return []
    value = json.loads(
        _contained_path(session_dir, relative).read_text(encoding="utf-8")
    )
    return [
        dict(item)
        for item in value.get("actions") or ()
        if (
            isinstance(item, dict)
            and item.get("included", True)
            and str(item.get("role") or "business") != "noise"
        )
    ]


def _contained_path(root, value):
    root = Path(root).resolve()
    path = (root / Path(value)).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Annotation artifact路径越界: {value}") from error
    return path


def _annotation_id(created_at, step_id, revision, business_context):
    return "annotation-" + stable_digest(
        created_at,
        STEP_USER_CONTEXT,
        step_id,
        str(revision),
        business_context,
        length=16,
    )


def _normalize_business_context_input(
        business_context,
        *,
        purpose=None,
        constraints=None,
    ):
    if business_context is not None and (
        str(purpose or "").strip()
        or str(constraints or "").strip()
    ):
        raise ValueError(
            "business_context不能与旧purpose/constraints同时提交"
        )
    if business_context is not None:
        return str(business_context or "").strip()
    purpose = str(purpose or "").strip()
    constraints = str(constraints or "").strip()
    if purpose and constraints:
        return f"{purpose}；{constraints}"
    return purpose or constraints


def step_business_context_text(context):
    if not isinstance(context, dict):
        return ""
    return str(context.get("business_context") or "").strip()


def _observation_intent_id(
        created_at,
        revision,
        payload,
        *,
        authority=None,
    ):
    values = [
        created_at,
        OBSERVATION_INTENT,
        str(revision),
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    if authority is not None:
        values.append(str(authority))
    return "annotation-" + stable_digest(*values, length=16)


def _public_context(record):
    keys = [
        "annotation_version",
        "annotation_type",
        "annotation_id",
        "created_at",
        "step_id",
        "authority",
        "revision",
        "supersedes",
        "active",
    ]
    keys.append("business_context")
    return {
        key: record.get(key)
        for key in keys
    }


def _public_observation_intent(record):
    return {
        key: record.get(key)
        for key in (
            "annotation_version",
            "annotation_type",
            "annotation_id",
            "created_at",
            "step_id",
            "take_id",
            "event_id",
            "authority",
            "revision",
            "supersedes",
            "focus",
            "relation",
            "expected_source",
            "property_name",
            "business_meaning",
        )
    }


def _observation_scope(step_id, take_id, event_id):
    values = tuple(
        str(value or "").strip()
        for value in (step_id, take_id, event_id)
    )
    if not all(values):
        raise ValueError(
            "ObservationIntent必须绑定step_id、take_id和event_id"
        )
    return values


def _record_observation_scope(record):
    return _observation_scope(
        record.get("step_id"),
        record.get("take_id"),
        record.get("event_id"),
    )


def _normalize_expected_source(value):
    value = dict(value or {"kind": "auto"})
    kind = str(value.get("kind") or "auto").strip().casefold()
    reference = str(value.get("reference") or "").strip() or None
    if kind not in EXPECTED_SOURCE_KINDS:
        raise ValueError(f"未知expected source: {kind}")
    if kind in {"examples", "data_table"} and not reference:
        raise ValueError(f"{kind} expected source必须提供reference")
    if kind not in {"examples", "data_table"} and reference:
        raise ValueError(f"{kind} expected source不能提供reference")
    return {"kind": kind, "reference": reference}


def _validate_records(records):
    previous_by_step = {}
    previous_by_observation = {}
    annotation_ids = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"annotation记录必须是object: index={index}")
        if record.get("annotation_version") not in (
            SUPPORTED_ANNOTATION_VERSIONS
        ):
            raise ValueError(f"annotation_version无效: index={index}")
        annotation_type = record.get("annotation_type")
        if annotation_type not in {STEP_USER_CONTEXT, OBSERVATION_INTENT}:
            raise ValueError(f"annotation_type无效: index={index}")
        step_id = str(record.get("step_id") or "").strip()
        if not step_id:
            raise ValueError(f"annotation缺少step_id: index={index}")
        if annotation_type == STEP_USER_CONTEXT:
            _validate_step_context_record(
                record,
                index,
                step_id,
                previous_by_step,
            )
        else:
            _validate_observation_intent_record(
                record,
                index,
                previous_by_observation,
            )
        annotation_id = str(record.get("annotation_id") or "")
        if not annotation_id or annotation_id in annotation_ids:
            raise ValueError(f"annotation_id缺失或重复: index={index}")
        annotation_ids.add(annotation_id)


def _validate_step_context_record(record, index, step_id, previous_by_step):
        if record.get("authority") != USER_DECLARED_CONTEXT:
            raise ValueError(f"annotation authority无效: index={index}")
        previous = previous_by_step.get(step_id)
        expected_revision = int((previous or {}).get("revision") or 0) + 1
        if record.get("revision") != expected_revision:
            raise ValueError(
                f"annotation revision不连续: step={step_id}, "
                f"expected={expected_revision}, actual={record.get('revision')}"
            )
        if record.get("supersedes") != (previous or {}).get("annotation_id"):
            raise ValueError(f"annotation supersedes无效: step={step_id}")
        business_context = record.get("business_context")
        if not isinstance(business_context, str):
            raise ValueError(
                f"annotation内容必须是字符串: index={index}"
            )
        if len(business_context) > MAX_STEP_CONTEXT_TEXT_LENGTH:
            raise ValueError(
                f"annotation内容超过2000字符: index={index}"
            )
        if "purpose" in record or "constraints" in record:
            raise ValueError(
                f"新annotation不能包含旧业务字段: index={index}"
            )
        active = bool(business_context)
        expected_id = _annotation_id(
            record.get("created_at"),
            step_id,
            expected_revision,
            business_context,
        )
        if record.get("active") is not active:
            raise ValueError(f"annotation active无效: index={index}")
        annotation_id = str(record.get("annotation_id") or "")
        if annotation_id != expected_id:
            raise ValueError(f"annotation_id指纹无效: index={index}")
        previous_by_step[step_id] = record


def _validate_observation_intent_record(
        record,
        index,
        previous_by_observation,
    ):
    valid_authorities = {USER_DECLARED_INTENT, SYSTEM_INFERRED_INTENT}
    if record.get("authority") not in valid_authorities:
        raise ValueError(f"ObservationIntent authority无效: index={index}")
    scope = _record_observation_scope(record)
    previous = previous_by_observation.get(scope)
    revision = int((previous or {}).get("revision") or 0) + 1
    if record.get("revision") != revision:
        raise ValueError(
            "ObservationIntent revision不连续: "
            f"scope={'/'.join(scope)}, expected={revision}, "
            f"actual={record.get('revision')}"
        )
    if record.get("supersedes") != (previous or {}).get("annotation_id"):
        raise ValueError(
            f"ObservationIntent supersedes无效: scope={'/'.join(scope)}"
        )
    focus = str(record.get("focus") or "")
    relation = str(record.get("relation") or "")
    if focus not in OBSERVATION_FOCUSES:
        raise ValueError(f"ObservationIntent focus无效: index={index}")
    if relation not in OBSERVATION_RELATIONS:
        raise ValueError(f"ObservationIntent relation无效: index={index}")
    expected_source = _normalize_expected_source(record.get("expected_source"))
    if expected_source != record.get("expected_source"):
        raise ValueError(f"ObservationIntent expected_source无效: index={index}")
    property_name = record.get("property_name")
    if focus == "property" and not property_name:
        raise ValueError(f"ObservationIntent缺少property_name: index={index}")
    if focus != "property" and property_name is not None:
        raise ValueError(f"ObservationIntent property_name越界: index={index}")
    _validate_observation_combination(focus, relation, expected_source)
    meaning = record.get("business_meaning")
    if not isinstance(meaning, str) or len(meaning) > MAX_OBSERVATION_MEANING_LENGTH:
        raise ValueError(f"ObservationIntent业务含义无效: index={index}")
    payload = {
        "step_id": scope[0],
        "take_id": scope[1],
        "event_id": scope[2],
        "focus": focus,
        "relation": relation,
        "expected_source": expected_source,
        "property_name": property_name,
        "business_meaning": meaning,
    }
    expected_id = _observation_intent_id(
        record.get("created_at"),
        revision,
        payload,
        authority=record.get("authority"),
    )
    if record.get("annotation_id") != expected_id:
        raise ValueError(f"ObservationIntent annotation_id无效: index={index}")
    previous_by_observation[scope] = record


def _validate_observation_combination(focus, relation, expected_source):
    source_kind = expected_source["kind"]
    observed_state_focuses = {"visible", "enabled", "property", "collection"}
    if focus in {"auto", *observed_state_focuses} and relation != "auto":
        raise ValueError(f"{focus} focus只支持auto relation")
    if focus == "auto" and source_kind != "auto":
        raise ValueError("auto focus只支持auto expected source")
    if focus in observed_state_focuses and source_kind != "observed_state":
        raise ValueError(
            f"{focus} focus必须使用observed_state expected source"
        )
    if focus in {
        "text",
        "value",
        "window_title",
        "region_text",
    } and source_kind == "observed_state":
        raise ValueError(
            f"{focus} focus不能把当前观察值作为业务期望"
        )
    if focus == "region_text" and relation == "equal":
        raise ValueError("region_text focus不支持equal relation")