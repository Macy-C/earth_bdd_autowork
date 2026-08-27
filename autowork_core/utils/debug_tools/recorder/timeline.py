from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

from autowork_core.utils.debug_tools.recorder.analysis import (
    build_locator_bundle,
)
from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
)
from autowork_core.utils.debug_tools.recorder.action_media import (
    build_action_media,
)
from autowork_core.utils.debug_tools.recorder.evidence_graph import (
    EVIDENCE_GRAPH_VERSION,
    build_evidence_graph,
)
from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.semantic_pack import (
    SEMANTIC_PACK_VERSION,
    build_semantic_pack,
)
from autowork_core.utils.debug_tools.recorder.pic_template_audit import (
    apply_pic_template_audit,
    build_pic_template_audit,
    template_artifacts,
)
from autowork_core.utils.debug_tools.recorder.supplement_repository import (
    SupplementRepository,
)
from autowork_core.utils.debug_tools.recorder.writer import (
    RecordingSessionWriter,
    write_json_atomic,
)


TIMELINE_PROTOCOL_VERSION = "1.1"
LOCATOR_PROJECTION_VERSION = "2.0"
EDIT_OPERATIONS = {
    "exclude",
    "include",
    "change_type",
    "set_role",
    "set_binding",
    "annotate",
    "update",
    "merge",
    "insert_supplement",
    "target_binding_repair",
    "keyboard_event_fragment",
}
USER_EDIT_OPERATIONS = {
    "exclude",
    "include",
    "keyboard_event_fragment",
}
ACTION_ROLES = ("business", "setup", "assertion", "noise", "transport")


class TimelineRevisionConflict(RuntimeError):
    def __init__(self, expected_revision, current_revision):
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            "时间线已被其他窗口更新: "
            f"expected={expected_revision}, current={current_revision}"
        )


class TimelineStore:
    def __init__(self, take_dir):
        self.take_dir = Path(take_dir)
        self.auto_path = self.take_dir / "actions.auto.json"
        self.locator_auto_path = self.take_dir / "locator-candidates.auto.yaml"
        self.edits_path = self.take_dir / "timeline-edits.jsonl"
        self.projections = ProjectionStore(self.take_dir)
        self.supplements = SupplementRepository(self.take_dir)
        self._reject_legacy_artifacts()

    def _reject_legacy_artifacts(self):
        legacy = [
            name
            for name in ("actions.json", "locator-candidates.yaml")
            if (self.take_dir / name).exists()
        ]
        if legacy and not self.auto_path.exists():
            raise ValueError(
                "当前 Recorder 只支持 schema 2.1；旧 Run 需要使用旧版本"
                "或独立离线迁移工具: " + ", ".join(legacy)
            )

    @property
    def effective_path(self):
        return self.projections.artifact_path(
            "actions_effective",
        ) or (self.projections.root / "__missing__" / "actions.effective.json")

    @property
    def events_effective_path(self):
        return self.projections.artifact_path(
            "events_effective",
        ) or (
            self.projections.root / "__missing__" / "events.effective.jsonl"
        )

    @property
    def locator_effective_path(self):
        return self.projections.artifact_path(
            "locator_candidates_effective",
        ) or (
            self.projections.root
            / "__missing__"
            / "locator-candidates.effective.yaml"
        )

    @property
    def state_path(self):
        return self.projections.artifact_path(
            "timeline_state",
        ) or (self.projections.root / "__missing__" / "timeline-state.json")

    def initialize(self, actions):
        auto = {
            "schema_version": SCHEMA_VERSION,
            "timeline_protocol_version": TIMELINE_PROTOCOL_VERSION,
            "source": "automatic",
            "actions": copy.deepcopy(list(actions)),
        }
        write_json_atomic(self.auto_path, auto)
        RecordingSessionWriter._write_jsonl_path(self.edits_path, [])
        return self.materialize()

    def apply_edit(self, operation, action_ids, payload=None, reason=""):
        if operation not in USER_EDIT_OPERATIONS:
            raise ValueError(
                "Timeline技术写操作已退役；用户只能忽略或恢复录制动作"
            )
        return self._append_legacy_edit(
            operation,
            action_ids,
            payload,
            reason,
        )

    def keyboard_events(self, action_id):
        action = self._keyboard_review_action(action_id)
        if action is None or action.get("type") != "keyboard":
            raise ValueError("键盘事件只能属于当前键盘动作")
        source_events = self._keyboard_source_events(action)
        if not source_events:
            return []
        excluded = {
            str(item)
            for item in action.get("excluded_keyboard_event_ids") or ()
        }
        return [{
            "event_id": str(event.get("id") or ""),
            "event_type": str(event.get("event_type") or ""),
            "key": copy.deepcopy(event.get("key") or {}),
            "included": str(event.get("id") or "") not in excluded,
        } for event in source_events]

    def set_keyboard_event_included(
            self,
            action_id,
            event_id,
            included,
            *,
            expected_revision=None,
        ):
        return self._set_keyboard_events_included(
            action_id,
            (event_id,),
            included,
            expected_revision=expected_revision,
            payload_key="event_id",
        )

    def set_keyboard_events_included(
            self,
            action_id,
            event_ids,
            included,
            *,
            expected_revision=None,
        ):
        return self._set_keyboard_events_included(
            action_id,
            event_ids,
            included,
            expected_revision=expected_revision,
            payload_key="event_ids",
        )

    def _set_keyboard_events_included(
            self,
            action_id,
            event_ids,
            included,
            *,
            expected_revision,
            payload_key,
        ):
        if expected_revision is not None:
            self.require_revision(expected_revision)
        action = self._keyboard_review_action(action_id)
        if action is None or action.get("type") != "keyboard":
            raise ValueError("键盘事件只能属于当前键盘动作")
        event_ids = _keyboard_edit_event_ids({"event_ids": event_ids})
        source_ids = {
            str(item.get("id") or "")
            for item in self._keyboard_source_events(action)
        }
        if not event_ids or not set(event_ids) <= source_ids:
            raise ValueError("键盘事件不属于当前动作")
        excluded = {
            str(item)
            for item in action.get("excluded_keyboard_event_ids") or ()
        }
        if included:
            excluded.difference_update(event_ids)
        else:
            excluded.update(event_ids)
        if len(excluded) >= len(source_ids):
            raise ValueError(
                "不能逐事件排除整段键盘输入；请明确确认后忽略整段输入"
            )
        payload = {
            payload_key: (
                event_ids[0] if payload_key == "event_id" else event_ids
            ),
            "included": bool(included),
        }
        record = self._edit_record(
            "keyboard_event_fragment",
            [str(action_id)],
            payload,
            "keyboard_event_user_correction",
        )
        self._append_record(record)
        return self.materialize()

    def _keyboard_source_events(self, action):
        source = action.get("source") or {}
        if source.get("kind") == "supplement":
            events = self.supplements.load_events(source.get("supplement_id"))
        else:
            path = self.take_dir / "events.jsonl"
            if not path.is_file():
                return []
            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        source_ids = {
            str(item)
            for item in (
                action.get("media_event_ids")
                or action.get("event_ids")
                or ()
            )
        }
        return [
            event for event in events
            if (
                str(event.get("id") or "") in source_ids
                and event.get("event_type") in {"key_down", "key_up"}
            )
        ]

    def _keyboard_review_action(self, action_id):
        if not self.auto_path.exists():
            return None
        auto = self._load_auto()
        actions = [
            {**copy.deepcopy(action), "included": True}
            for action in _normalize_actions(auto.get("actions", []))
        ]
        edits = self.load_edits()
        active = _active_edit_map(edits)
        for record in edits:
            if record.get("kind") != "edit" or not active.get(record["edit_id"], True):
                continue
            if record.get("operation") == "insert_supplement":
                actions = self._insert_supplement_actions(actions, record)
            else:
                actions = _apply_record(actions, record)
        return next((
            item for item in actions
            if item.get("id") == str(action_id)
        ), None)

    def apply_target_binding_repair(
            self,
            candidate,
            *,
            expected_revision,
        ):
        self.require_revision(expected_revision)
        candidate = copy.deepcopy(dict(candidate or {}))
        if any((
            candidate.get("target_repair_version") != "1.0",
            candidate.get("status") != "forensic_verified",
            not candidate.get("candidate_id"),
            not candidate.get("action_id"),
            not candidate.get("target_event_id"),
            not isinstance(candidate.get("event_target"), dict),
            not isinstance(candidate.get("action_target"), dict),
        )):
            raise ValueError("target binding repair candidate无效")
        action = next((
            item
            for item in self.review_actions()
            if item.get("id") == candidate["action_id"]
        ), None)
        if action is None:
            raise KeyError(
                f"target repair action不存在: {candidate['action_id']}"
            )
        if candidate["target_event_id"] != action.get("target_event_id"):
            raise ValueError("target repair event与Action target_event_id不一致")
        event_element = (
            (candidate.get("event_target") or {}).get("element") or {}
        )
        action_element = (
            (candidate.get("action_target") or {}).get("element") or {}
        )
        candidate_element = candidate.get("element") or {}
        if not (
            event_element == action_element == candidate_element
        ):
            raise ValueError("target repair payload不一致: element")
        locator = candidate.get("locator") or {}
        candidates = (
            (candidate.get("event_target") or {}).get(
                "locator_candidates"
            ) or ()
        )
        if not any(
            (item.get("locator") or {}) == {
                **locator,
                "root": (
                    (candidate.get("event_target") or {}).get("root_name")
                ),
            }
            for item in candidates
        ):
            raise ValueError("target repair payload不一致: locator")
        from autowork_core.utils.debug_tools.recorder.target_repair import (
            TargetRepairService,
        )
        trusted_candidate = next((
            item
            for item in TargetRepairService(self.take_dir).candidates(
                candidate["action_id"]
            )
            if item.get("candidate_id") == candidate["candidate_id"]
        ), None)
        if trusted_candidate is None or candidate != trusted_candidate:
            raise ValueError("target repair candidate与sealed证据不一致")
        record = self._edit_record(
            "target_binding_repair",
            [candidate["action_id"]],
            candidate,
            "system_verified_target_binding_repair",
        )
        record["authority"] = "system_verified"
        self._append_record(record)
        return self.materialize()

    def _append_legacy_edit(
            self,
            operation,
            action_ids,
            payload=None,
            reason="",
        ):
        if operation not in EDIT_OPERATIONS:
            raise ValueError(f"不支持的时间线操作: {operation}")
        action_ids = _normalize_action_ids(action_ids)
        current_actions = self.review_actions()
        current_ids = {action["id"] for action in current_actions}
        missing = [action_id for action_id in action_ids if action_id not in current_ids]
        if missing:
            raise KeyError(f"时间线中不存在动作: {missing}")
        if operation == "merge" and len(action_ids) < 2:
            raise ValueError("合并至少需要两个动作")
        if operation == "merge":
            selected_ids = set(action_ids)
            selected = [
                action
                for action in current_actions
                if action.get("id") in selected_ids
            ]
            if any(
                not action.get("included", True) for action in selected
            ):
                raise ValueError("已忽略的动作不能合并；请先恢复动作")
            selected_indexes = [
                index
                for index, action in enumerate(current_actions)
                if action.get("id") in selected_ids
            ]
            if selected_indexes != list(range(
                selected_indexes[0],
                selected_indexes[-1] + 1,
            )):
                raise ValueError("只能合并时间线中连续的动作")
            source_keys = {_action_source_key(action) for action in selected}
            if len(source_keys) > 1:
                raise ValueError("不同录制片段的动作不能合并")
            if any(action.get("source_action_ids") for action in selected):
                raise ValueError("已合并的动作不能再次合并；请先解除合并")
        payload = dict(payload or {})
        if operation == "change_type" and not payload.get("type"):
            raise ValueError("change_type 必须提供 payload.type")
        if operation == "set_role" and payload.get("role") not in ACTION_ROLES:
            raise ValueError(f"未知动作角色: {payload.get('role')}")
        if operation == "update":
            if payload.get("role") not in ACTION_ROLES:
                raise ValueError(f"未知动作角色: {payload.get('role')}")
            if not payload.get("type"):
                raise ValueError("update 必须提供 payload.type")
        if operation == "exclude":
            selected = [
                action for action in current_actions
                if action.get("id") in set(action_ids)
            ]
            if any(action.get("type") == "keyboard" for action in selected):
                if any((
                    len(action_ids) != 1,
                    payload.get("confirmed_keyboard_exclusion") is not True,
                )):
                    raise ValueError("忽略整段键盘输入需要明确确认")
        if operation == "keyboard_event_fragment":
            if len(action_ids) != 1:
                raise ValueError("键盘事件编辑一次只能作用于一个动作")
            if not isinstance(payload.get("included"), bool):
                raise ValueError("键盘事件编辑必须提供included布尔值")
            action = self._keyboard_review_action(action_ids[0])
            if action is None or action.get("type") != "keyboard":
                raise ValueError("键盘事件只能属于当前键盘动作")
            source_ids = {
                str(event.get("id") or "")
                for event in self._keyboard_source_events(action)
            }
            event_ids = _keyboard_edit_event_ids(payload)
            if not event_ids or not set(event_ids) <= source_ids:
                raise ValueError("键盘事件不属于当前动作")

        record = self._edit_record(operation, action_ids, payload, reason)
        self._append_record(record)
        return self.materialize()

    def restore_legacy_noise(self, action_id):
        action_ids = _normalize_action_ids(action_id)
        if len(action_ids) != 1:
            raise ValueError("恢复历史noise编辑时只能选择一个动作")
        action = next((
            item
            for item in self.review_actions()
            if item.get("id") == action_ids[0]
        ), None)
        if action is None:
            raise KeyError(f"时间线中不存在动作: {action_ids[0]}")
        if (action.get("role") or "business") != "noise":
            raise ValueError("只有历史noise动作需要兼容恢复")
        return self._append_legacy_edit(
            "update",
            action_ids,
            {
                "included": True,
                "type": action.get("type") or "click",
                "role": action.get("previous_role") or "business",
                "binding": action.get("value_binding"),
                "note": action.get("note") or "",
            },
            reason="compatibility_noise_restore",
        )

    def insert_supplement(
            self,
            supplement_id,
            action_ids,
            *,
            before_action_id=None,
            reason="",
        ):
        ordered_source_actions = self.supplements.load_actions(supplement_id)
        source_actions = {
            action["id"]: action
            for action in ordered_source_actions
        }
        action_ids = _normalize_action_ids(action_ids)
        missing = [action_id for action_id in action_ids if action_id not in source_actions]
        if missing:
            raise KeyError(f"补录片段中不存在动作: {missing}")
        source_order = [action["id"] for action in ordered_source_actions]
        if action_ids != source_order:
            raise ValueError("新补录必须按原顺序整段插入，不能逐Action挑选或重排")
        current_ids = {action["id"] for action in self.review_actions()}
        if before_action_id is not None and before_action_id not in current_ids:
            raise KeyError(f"插入位置不存在: {before_action_id}")
        record = self._edit_record(
            "insert_supplement",
            action_ids,
            {
                "supplement_id": str(supplement_id),
                "before_action_id": before_action_id,
            },
            reason,
        )
        self._append_record(record)
        return self.materialize()

    def undo(self):
        edits = self.load_edits()
        active = _active_edit_map(edits)
        target = next(
            (
                record
                for record in reversed(edits)
                if (
                    _user_controllable_edit(record)
                    and active.get(record["edit_id"], True)
                )
            ),
            None,
        )
        if target is None:
            return self.materialize()
        self._append_record(self._control_record("undo", target["edit_id"]))
        return self.materialize()

    def redo(self):
        edits = self.load_edits()
        active = _active_edit_map(edits)
        target = next(
            (
                record
                for record in reversed(edits)
                if (
                    _user_controllable_edit(record)
                    and not active.get(record["edit_id"], True)
                )
            ),
            None,
        )
        if target is None:
            return self.materialize()
        self._append_record(self._control_record("redo", target["edit_id"]))
        return self.materialize()

    def reset(self):
        edits = self.load_edits()
        active = _active_edit_map(edits)
        for record in edits:
            if (
                _user_controllable_edit(record)
                and active.get(record["edit_id"], True)
            ):
                self._append_record(self._control_record("undo", record["edit_id"]))
        return self.materialize()

    def materialize(self):
        auto = self._load_auto()
        auto_actions = _normalize_actions(auto.get("actions", []))
        if not self.auto_path.exists() or auto_actions != auto.get("actions", []):
            auto = {
                **auto,
                "source": "migrated_legacy",
                "actions": auto_actions,
            }
            write_json_atomic(self.auto_path, auto)
        edits = self.load_edits()
        active = _active_edit_map(edits)
        actions = [
            {**copy.deepcopy(action), "included": True}
            for action in auto_actions
        ]
        for record in edits:
            if record.get("kind") != "edit" or not active.get(record["edit_id"], True):
                continue
            if record.get("operation") == "insert_supplement":
                actions = self._insert_supplement_actions(actions, record)
            else:
                actions = _apply_record(actions, record)

        actions = self._materialize_keyboard_event_fragments(actions)

        target_repairs = {
            str((record.get("payload") or {}).get("target_event_id") or ""):
            copy.deepcopy(record.get("payload") or {})
            for record in edits
            if (
                record.get("kind") == "edit"
                and active.get(record["edit_id"], True)
                and record.get("operation") == "target_binding_repair"
            )
        }

        metadata = _load_json_file(self.take_dir / "take.json")
        review_actions = _reindex_actions(actions)
        confirmed_input_exclusions = _confirmed_keyboard_input_exclusions(
            review_actions,
            edits,
            active,
            step_id=str((metadata.get("step") or {}).get("id") or ""),
        )
        effective_actions = [
            {key: value for key, value in action.items() if key != "included"}
            for action in review_actions
            if action.get("included", True)
            and action.get("role", "business") != "noise"
        ]
        revision = _timeline_revision(edits)
        effective = {
            "schema_version": SCHEMA_VERSION,
            "timeline_protocol_version": TIMELINE_PROTOCOL_VERSION,
            "source": "effective",
            "timeline_revision": revision,
            "actions": effective_actions,
        }
        effective_events = self._compose_effective_events(
            effective_actions,
            target_repairs=target_repairs,
        )
        effective_locator = self._combined_locator_bundle(
            effective_actions,
            events=effective_events,
        )
        state = {
            "schema_version": SCHEMA_VERSION,
            "timeline_protocol_version": TIMELINE_PROTOCOL_VERSION,
            "timeline_revision": revision,
            "active_edit_ids": [
                record["edit_id"]
                for record in edits
                if record.get("kind") == "edit" and active.get(record["edit_id"], True)
            ],
            "can_undo": any(
                _user_controllable_edit(record)
                and active.get(record["edit_id"], True)
                for record in edits
            ),
            "can_redo": any(
                _user_controllable_edit(record)
                and not active.get(record["edit_id"], True)
                for record in edits
            ),
            "actions": review_actions,
            "confirmed_keyboard_input_exclusions": (
                confirmed_input_exclusions
            ),
            "role_contract": {
                "business": "generate business operation",
                "setup": "generate prerequisite/setup behavior",
                "assertion": "use as assertion evidence",
                "noise": "exclude from effective generation while preserving review history",
                "transport": "preserve navigation context; do not treat as business assertion",
            },
        }
        source_revision = self._projection_source_revision(
            revision,
            effective_actions,
        )
        projection_revision = self.projections.revision_for(source_revision)
        projection_prefix = self.projections.relative_directory(
            projection_revision
        )

        def build_projection(directory):
            write_json_atomic(directory / "actions.effective.json", effective)
            RecordingSessionWriter._write_jsonl_path(
                directory / "events.effective.jsonl",
                effective_events,
            )
            _materialize_locator_bundle(
                effective_locator,
                directory / "locator-candidates.effective.yaml",
                effective_actions,
            )
            write_json_atomic(directory / "timeline-state.json", state)
            action_media = build_action_media(
                self.take_dir,
                effective_actions,
                effective_events,
                metadata=metadata,
                output_dir=directory,
                artifact_prefix=projection_prefix,
            )
            media_index = _load_json_file(self.take_dir / "media-index.json")
            media_index["action_media"] = "action-media.json"
            media_index["actions"] = action_media.get("actions") or []
            media_index["action_contact_sheet"] = action_media.get(
                "contact_sheet"
            )
            write_json_atomic(directory / "media-index.json", media_index)
            build_evidence_graph(
                self.take_dir,
                write=True,
                projection_dir=directory,
                projection_prefix=projection_prefix,
            )
            manifest = _load_run_manifest(self.take_dir)
            run_directory = _run_directory_for_take(self.take_dir)
            annotation_model_version = (
                manifest.get("annotation_model_version")
                or metadata.get("annotation_model_version")
            )
            observation_intents = RecordingAnnotationRepository(
                run_directory
            ).project_observation_intents(
                (metadata.get("step") or {}).get("id"),
                metadata.get("id"),
                effective_actions,
            )
            for source in _supplement_sources(effective_actions):
                supplement_directory = self.supplements.path_for(
                    source["supplement_id"]
                )
                observation_intents.extend(
                    RecordingAnnotationRepository(
                        supplement_directory
                    ).project_observation_intents(
                        (metadata.get("step") or {}).get("id"),
                        source["supplement_id"],
                        effective_actions,
                    )
                )
            semantic_pack = build_semantic_pack(
                self.take_dir,
                actions=effective_actions,
                events=effective_events,
                locator_bundle=effective_locator,
                action_media=action_media,
                metadata=metadata,
                scenario=manifest.get("scenario") or {},
                step=metadata.get("step") or {},
                observation_intents=observation_intents,
                annotation_model_version=annotation_model_version,
            )
            pic_audit = build_pic_template_audit(
                self.take_dir,
                semantic_pack,
                action_media,
                output_dir=directory,
            )
            semantic_pack = apply_pic_template_audit(
                semantic_pack,
                pic_audit,
            )
            write_json_atomic(
                directory / "semantic-pack.json",
                semantic_pack,
            )
            artifacts = {
                "timeline_state": "timeline-state.json",
                "actions_effective": "actions.effective.json",
                "events_effective": "events.effective.jsonl",
                "locator_candidates_effective": (
                    "locator-candidates.effective.yaml"
                ),
                "action_media": "action-media.json",
                "media_index": "media-index.json",
                "evidence_graph": "evidence/graph.json",
                "semantic_pack": "semantic-pack.json",
                "pic_template_audit": "pic-template-audit.json",
            }
            artifacts.update(template_artifacts(pic_audit))
            if (directory / "action-contact-sheet.png").exists():
                artifacts["action_contact_sheet"] = (
                    "action-contact-sheet.png"
                )
            return artifacts

        self.projections.publish(
            source_revision,
            build_projection,
            required=(
                "timeline_state",
                "actions_effective",
                "events_effective",
                "locator_candidates_effective",
                "action_media",
                "media_index",
                "evidence_graph",
                "semantic_pack",
                "pic_template_audit",
            ),
        )
        return state

    def _projection_source_revision(self, timeline_revision, actions):
        digest = hashlib.sha256()
        digest.update(str(timeline_revision).encode("utf-8"))
        digest.update(
            (
                f"locator:{LOCATOR_PROJECTION_VERSION}|"
                f"graph:{EVIDENCE_GRAPH_VERSION}|"
                f"semantics:{SEMANTIC_PACK_VERSION}"
            ).encode("utf-8")
        )
        paths = [
            self.auto_path,
            self.edits_path,
            self.take_dir / "events.jsonl",
            self.locator_auto_path,
            self.take_dir / "take.json",
            self.take_dir / "media-index.json",
            _run_directory_for_take(self.take_dir)
            / "recording-annotations.jsonl",
        ]
        paths.extend(self._base_tree_paths())
        media_stats = []
        for source in _supplement_sources(actions):
            directory = self.supplements.path_for(source["supplement_id"])
            structured = {
                "supplement.json",
                "events.jsonl",
                "actions.captured.json",
                "locator-candidates.captured.yaml",
                "recording-annotations.jsonl",
            }
            for path in sorted(item for item in directory.rglob("*") if item.is_file()):
                if path.name in structured:
                    paths.append(path)
                    continue
                stat = path.stat()
                media_stats.append((
                    path.relative_to(self.take_dir).as_posix(),
                    stat.st_size,
                    stat.st_mtime_ns,
                ))
        for path in paths:
            path = Path(path)
            try:
                logical_path = path.relative_to(self.take_dir).as_posix()
            except ValueError:
                logical_path = path.name
            digest.update(logical_path.encode("utf-8"))
            if path.exists():
                digest.update(self._projection_source_payload(path))
        for logical_path, size, modified in media_stats:
            digest.update(f"{logical_path}|{size}|{modified}".encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _projection_source_payload(path):
        path = Path(path)
        if path.name not in {"take.json", "media-index.json"}:
            return path.read_bytes()
        value = _load_json_file(path)
        if path.name == "take.json":
            value.pop("timeline_revision", None)
            value.pop("effective_action_count", None)
        else:
            value.pop("action_media", None)
            value.pop("actions", None)
            value.pop("action_contact_sheet", None)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def review_actions(self):
        if not self.auto_path.exists():
            return []
        if not self.state_path.exists():
            return self.materialize().get("actions", [])
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.materialize().get("actions", [])
        state_actions = state.get("actions", [])
        expected_revision = _timeline_revision(self.load_edits())
        if (
            state.get("timeline_revision") != expected_revision
            or any(not action.get("id") for action in state_actions)
        ):
            return self.materialize().get("actions", [])
        return state_actions

    def _insert_supplement_actions(self, actions, record):
        payload = record.get("payload") or {}
        supplement_id = payload.get("supplement_id")
        selected_ids = set(record.get("action_ids") or ())
        metadata = self.supplements.load(supplement_id)
        source_path = metadata.get("relative_path")
        additions = []
        existing_ids = {action.get("id") for action in actions}
        for source in self.supplements.load_actions(supplement_id):
            source_id = source.get("id")
            if source_id not in selected_ids:
                continue
            inserted = copy.deepcopy(source)
            inserted_id = (
                f"{source_id}-insert-"
                f"{stable_digest(record['edit_id'], source_id, length=8)}"
            )
            if inserted_id in existing_ids:
                raise ValueError(f"补录动作实例 ID 冲突: {inserted_id}")
            inserted["id"] = inserted_id
            inserted["source_action_id"] = source_id
            inserted["source"] = {
                "kind": "supplement",
                "supplement_id": supplement_id,
                "path": source_path,
                "insert_edit_id": record.get("edit_id"),
                "video_to_event_offset_ms": (
                    metadata.get("timeline") or {}
                ).get("video_to_event_offset_ms"),
            }
            additions.append(inserted)
            existing_ids.add(inserted_id)
        if len(additions) != len(selected_ids):
            raise ValueError(f"补录动作读取不完整: {supplement_id}")
        before_action_id = payload.get("before_action_id")
        if before_action_id is None:
            return [*actions, *additions]
        index = next(
            (
                index
                for index, action in enumerate(actions)
                if action.get("id") == before_action_id
            ),
            None,
        )
        if index is None:
            raise KeyError(f"插入位置已失效: {before_action_id}")
        return [*actions[:index], *additions, *actions[index:]]

    def effective_actions(self):
        if not self.effective_path.exists():
            self.materialize()
        try:
            return json.loads(
                self.effective_path.read_text(encoding="utf-8")
            ).get("actions", [])
        except (OSError, json.JSONDecodeError):
            self.materialize()
            return json.loads(
                self.effective_path.read_text(encoding="utf-8")
            ).get("actions", [])

    def effective_events(self):
        if not self.events_effective_path.exists():
            self.materialize()
        try:
            return self._read_effective_events()
        except (OSError, json.JSONDecodeError):
            self.materialize()
            return self._read_effective_events()

    def _read_effective_events(self):
        return [
            json.loads(line)
            for line in self.events_effective_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

    def _compose_effective_events(self, actions, *, target_repairs=None):
        target_repairs = target_repairs or {}
        effective_event_ids = {
            str(event_id)
            for action in actions
            for event_id in (
                action.get("media_event_ids")
                or action.get("event_ids")
                or ()
            )
        }
        events_path = self.take_dir / "events.jsonl"
        events = []
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("id") in effective_event_ids:
                    event = _apply_target_repair_to_event(
                        event,
                        target_repairs.get(str(event.get("id") or "")),
                    )
                    events.append(event)
        for source in _supplement_sources(actions):
            relative_root = Path(source["path"])
            for event in self.supplements.load_events(source["supplement_id"]):
                if event.get("id") not in effective_event_ids:
                    continue
                event = copy.deepcopy(event)
                screenshot = event.get("screenshot")
                if screenshot:
                    event["screenshot"] = (relative_root / screenshot).as_posix()
                details = event.setdefault("details", {})
                details["supplement"] = {
                    "supplement_id": source["supplement_id"],
                    "path": source["path"],
                }
                for frame in details.get("frames") or ():
                    if frame.get("path"):
                        frame["path"] = (
                            relative_root / frame["path"]
                        ).as_posix()
                events.append(event)
        return events

    def _materialize_keyboard_event_fragments(self, actions):
        if not any(
                action.get("type") == "keyboard"
                and action.get("excluded_keyboard_event_ids")
                for action in actions
        ):
            return actions
        for action in actions:
            excluded = {
                str(item)
                for item in action.get("excluded_keyboard_event_ids") or ()
            }
            if action.get("type") != "keyboard" or not excluded:
                continue
            original_key_ids = [
                str(item) for item in action.get("event_ids") or ()
            ]
            kept_key_ids = [
                item for item in original_key_ids if item not in excluded
            ]
            action["event_ids"] = kept_key_ids
            action["keys"] = [
                item
                for index, item in enumerate(action.get("keys") or ())
                if (
                    index < len(original_key_ids)
                    and original_key_ids[index] in kept_key_ids
                )
            ]
            action["media_event_ids"] = [
                str(item)
                for item in action.get("media_event_ids") or original_key_ids
                if str(item) not in excluded
            ]
            if action["media_event_ids"]:
                action["commit_event_id"] = action["media_event_ids"][-1]
        return actions

    def _combined_locator_bundle(self, actions, *, events=None):
        base = self._recomputed_base_locator_bundle(events=events)
        combined = copy.deepcopy(base)
        combined.setdefault("roots", {})
        combined.setdefault("locators", {})
        combined.setdefault("event_targets", [])
        combined.setdefault("unresolved", [])
        for source in _supplement_sources(actions):
            bundle = self.supplements.load_locator_bundle(
                source["supplement_id"]
            )
            suffix = stable_digest(source["supplement_id"], length=6)
            root_names = _merge_named_bundle_values(
                combined["roots"],
                bundle.get("roots") or {},
                suffix,
            )
            remapped_locators = {}
            for name, locator in (bundle.get("locators") or {}).items():
                locator = copy.deepcopy(locator)
                for key in ("root", "region"):
                    if locator.get(key) in root_names:
                        locator[key] = root_names[locator[key]]
                remapped_locators[name] = locator
            locator_names = _merge_named_bundle_values(
                combined["locators"],
                remapped_locators,
                suffix,
            )
            for target in bundle.get("event_targets") or ():
                target = _remap_target_names(
                    target,
                    root_names,
                    locator_names,
                )
                combined["event_targets"].append(target)
            combined["unresolved"].extend([
                _remap_target_names(target, root_names, locator_names)
                for target in bundle.get("unresolved") or []
            ])
        return combined

    def _recomputed_base_locator_bundle(self, *, events=None):
        events_path = self.take_dir / "events.jsonl"
        if events is not None or events_path.exists():
            try:
                events = list(events) if events is not None else [
                    json.loads(line)
                    for line in events_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if events:
                    snapshots = [
                        _load_json_file(path)
                        for path in self._base_tree_paths()
                    ]
                    return build_locator_bundle(
                        events,
                        tree_snapshots=snapshots,
                    )
            except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError):
                pass
        if self.locator_auto_path.exists():
            return yaml.safe_load(
                self.locator_auto_path.read_text(encoding="utf-8")
            ) or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "roots": {},
            "locators": {},
            "event_targets": [],
            "unresolved": [],
        }

    def _base_tree_paths(self):
        paths = [
            self.take_dir / "ui" / "before-tree.json",
            self.take_dir / "ui" / "after-tree.json",
        ]
        windows = self.take_dir / "windows"
        if windows.exists():
            paths.extend(sorted(windows.glob("window-*/before-tree.json")))
            paths.extend(sorted(windows.glob("window-*/after-tree.json")))
        return [path for path in paths if path.exists()]

    def load_edits(self):
        if not self.edits_path.exists():
            return []
        edits = [
            json.loads(line)
            for line in self.edits_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        active = _active_edit_map(edits)
        if any(
                record.get("operation") == "keyboard_fragment"
                and active.get(record.get("edit_id"), True)
                for record in edits
        ):
            raise ValueError(
                "keyboard_fragment 编辑已退役；"
                "请重新检查当前录制内容"
            )
        if self.auto_path.is_file():
            keyboard_action_ids = {
                str(action.get("id") or "")
                for action in _normalize_actions(
                    self._load_auto().get("actions") or ()
                )
                if action.get("type") == "keyboard"
            }
            if any(
                    record.get("kind") == "edit"
                    and active.get(record.get("edit_id"), True)
                    and record.get("operation") == "exclude"
                    and keyboard_action_ids & {
                        str(item)
                        for item in record.get("action_ids") or ()
                    }
                    and (record.get("payload") or {}).get(
                        "confirmed_keyboard_exclusion"
                    ) is not True
                    for record in edits
            ):
                raise ValueError(
                    "未确认的整段键盘输入排除已退役；"
                    "请重新检查当前录制内容"
                )
        return edits

    def current_revision(self):
        return _timeline_revision(self.load_edits())

    def require_revision(self, expected_revision):
        current_revision = self.current_revision()
        if str(expected_revision or "") != current_revision:
            raise TimelineRevisionConflict(
                expected_revision,
                current_revision,
            )
        return current_revision

    def _load_auto(self):
        if self.auto_path.exists():
            return json.loads(self.auto_path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"自动动作文件不存在: {self.auto_path}")

    def _append_record(self, record):
        self.edits_path.parent.mkdir(parents=True, exist_ok=True)
        with self.edits_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    def _edit_record(self, operation, action_ids, payload, reason):
        created_at = datetime.now().isoformat(timespec="milliseconds")
        edit_id = "edit-" + stable_digest(
            created_at,
            operation,
            *action_ids,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            length=12,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "timeline_protocol_version": TIMELINE_PROTOCOL_VERSION,
            "kind": "edit",
            "edit_id": edit_id,
            "created_at": created_at,
            "operation": operation,
            "action_ids": action_ids,
            "payload": payload,
            "reason": str(reason or ""),
        }

    def _control_record(self, operation, target_edit_id, reason=""):
        created_at = datetime.now().isoformat(timespec="milliseconds")
        record = {
            "schema_version": SCHEMA_VERSION,
            "timeline_protocol_version": TIMELINE_PROTOCOL_VERSION,
            "kind": "control",
            "control_id": "control-" + stable_digest(
                created_at,
                operation,
                target_edit_id,
                length=12,
            ),
            "created_at": created_at,
            "operation": operation,
            "target_edit_id": target_edit_id,
        }
        if reason:
            record["reason"] = str(reason)
        return record


def _load_json_file(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_run_manifest(take_dir):
    take_dir = Path(take_dir).resolve()
    for directory in take_dir.parents:
        manifest = directory / "manifest.json"
        if manifest.exists():
            return _load_json_file(manifest)
    return {}


def _run_directory_for_take(take_dir):
    take_dir = Path(take_dir).resolve()
    for directory in (take_dir, *take_dir.parents):
        if (directory / "manifest.json").is_file():
            return directory
    return take_dir


def _normalize_action_ids(action_ids):
    if isinstance(action_ids, str):
        action_ids = [action_ids]
    result = []
    for action_id in action_ids or ():
        action_id = str(action_id).strip()
        if action_id and action_id not in result:
            result.append(action_id)
    if not result:
        raise ValueError("至少选择一个动作")
    return result


def _normalize_actions(actions):
    result = []
    used_ids = set()
    for ordinal, original in enumerate(actions or (), start=1):
        action = dict(original)
        action_id = action.get("id")
        if not action_id:
            action_id = "action-" + stable_digest(
                "legacy-action",
                action.get("type"),
                *(action.get("event_ids") or ()),
                ordinal,
                length=12,
            )
        candidate = action_id
        suffix = 2
        while candidate in used_ids:
            candidate = f"{action_id}-{suffix}"
            suffix += 1
        action["id"] = candidate
        action["ordinal"] = ordinal
        action.setdefault("role", "business")
        used_ids.add(candidate)
        result.append(action)
    return result


def _active_edit_map(records):
    active = {
        record["edit_id"]: True
        for record in records
        if record.get("kind") == "edit"
    }
    for record in records:
        if record.get("kind") != "control":
            continue
        target = record.get("target_edit_id")
        if target not in active:
            continue
        active[target] = record.get("operation") == "redo"
    return active


def _user_controllable_edit(record):
    return bool(
        record.get("kind") == "edit"
        and record.get("authority") != "system_verified"
    )


def _confirmed_keyboard_input_exclusions(
        actions,
        edits,
        active,
        *,
        step_id,
    ):
    by_id = {
        str(action.get("id") or ""): action
        for action in actions or ()
        if action.get("id")
    }
    result = []
    for record in edits or ():
        if any((
            record.get("kind") != "edit",
            not active.get(record.get("edit_id"), True),
            record.get("operation") != "exclude",
            (record.get("payload") or {}).get(
                "confirmed_keyboard_exclusion"
            ) is not True,
        )):
            continue
        action_ids = [
            str(item) for item in record.get("action_ids") or () if item
        ]
        if len(action_ids) != 1:
            continue
        excluded = by_id.get(action_ids[0])
        if excluded is None or excluded.get("type") != "keyboard":
            continue
        identity = _action_target_identity(excluded)
        candidate_actions = [
            action for action in actions or ()
            if all((
                action.get("included", True),
                action.get("type") in {"click", "focus"},
                int(action.get("ordinal") or 0)
                < int(excluded.get("ordinal") or 0),
                _action_target_identity(action) == identity,
            ))
        ] if identity is not None else []
        target_action = (
            candidate_actions[0]
            if len(candidate_actions) == 1
            else None
        )
        reason = (
            "target_identity_unavailable"
            if identity is None
            else "target_missing"
            if not candidate_actions
            else "target_ambiguous"
            if len(candidate_actions) > 1
            else None
        )
        candidate_id = "input-recovery-" + stable_digest(
            record.get("edit_id"),
            excluded.get("id"),
            target_action.get("id") if target_action else reason,
            length=16,
        )
        result.append({
            "candidate_id": candidate_id,
            "status": (
                "pending_target_validation"
                if target_action is not None
                else "unavailable"
            ),
            "reason": reason,
            "step_id": step_id or None,
            "confirmed_edit_id": record.get("edit_id"),
            "excluded_action_id": excluded.get("id"),
            "excluded_event_ids": list(
                excluded.get("media_event_ids")
                or excluded.get("event_ids")
                or ()
            ),
            "excluded_input_text": _keyboard_input_text(excluded),
            "excluded_target_identity": identity,
            "target_action_id": (
                target_action.get("id") if target_action else None
            ),
            "target_identity": (
                _action_target_identity(target_action)
                if target_action is not None
                else None
            ),
        })
    return result


def _keyboard_input_text(action):
    names = [
        str((key or {}).get("name") or "")
        for key in (action.get("keys") or ())
    ]
    if not names or not all(
            len(name) == 1 and name.isprintable()
            for name in names
    ):
        return None
    return "".join(names)


def _action_target_identity(action):
    target = (action or {}).get("target") or {}
    element = target.get("element") or {}
    root_name = str(target.get("root_name") or "")
    process_id = element.get("process_id")
    handle = element.get("handle")
    runtime_id = tuple(element.get("runtime_id") or ())
    if root_name and process_id and handle:
        return {
            "root_name": root_name,
            "process_id": int(process_id),
            "handle": int(handle),
        }
    if root_name and runtime_id:
        return {
            "root_name": root_name,
            "runtime_id": list(runtime_id),
        }
    auto_id = str(element.get("auto_id") or "")
    control_type = str(element.get("control_type") or "")
    class_name = str(element.get("class_name") or "")
    if root_name and auto_id and control_type:
        return {
            "root_name": root_name,
            "auto_id": auto_id,
            "control_type": control_type,
            "class_name": class_name,
        }
    return None


def _apply_record(actions, record):
    operation = record["operation"]
    action_ids = set(record.get("action_ids") or ())
    payload = record.get("payload") or {}
    if operation == "merge":
        return _merge_actions(actions, record)
    for action in actions:
        if action.get("id") not in action_ids:
            continue
        if operation == "exclude":
            action["included"] = False
        elif operation == "include":
            action["included"] = True
        elif operation == "change_type":
            action["type"] = payload["type"]
        elif operation == "set_role":
            role = payload["role"]
            if role == "noise" and action.get("role") != "noise":
                action["previous_role"] = action.get("role") or "business"
            elif role != "noise":
                action.pop("previous_role", None)
            action["role"] = role
        elif operation == "set_binding":
            action["value_binding"] = payload.get("binding")
        elif operation == "annotate":
            action["note"] = payload.get("note", "")
        elif operation == "update":
            action["included"] = bool(payload.get("included", True))
            action["type"] = payload["type"]
            role = payload["role"]
            if role == "noise" and action.get("role") != "noise":
                action["previous_role"] = action.get("role") or "business"
            elif role != "noise":
                action.pop("previous_role", None)
            action["role"] = role
            action["value_binding"] = payload.get("binding")
            action["note"] = payload.get("note", "")
        elif operation == "target_binding_repair":
            action["target"] = copy.deepcopy(payload["action_target"])
        elif operation == "keyboard_event_fragment":
            excluded = {
                str(item)
                for item in action.get("excluded_keyboard_event_ids") or ()
            }
            event_ids = _keyboard_edit_event_ids(payload)
            if payload.get("included"):
                excluded.difference_update(event_ids)
            else:
                excluded.update(event_ids)
            action["excluded_keyboard_event_ids"] = sorted(excluded)
    return actions


def _keyboard_edit_event_ids(payload):
    payload = payload or {}
    value = payload.get("event_ids")
    if value is None:
        value = (payload.get("event_id"),)
    if isinstance(value, str):
        value = (value,)
    result = []
    for item in value or ():
        event_id = str(item or "")
        if event_id and event_id not in result:
            result.append(event_id)
    return result


def _apply_target_repair_to_event(event, repair):
    if not repair:
        return event
    event = copy.deepcopy(event)
    event["target"] = copy.deepcopy(repair["event_target"])
    details = event.setdefault("details", {})
    details["observation_phase"] = "forensic_repaired"
    details["target_binding"] = {
        "target_binding_version": repair["target_repair_version"],
        "status": "forensic_verified",
        "phase": "forensic_snapshot",
        "candidate_id": repair["candidate_id"],
    }
    evidence = repair.get("evidence") or {}
    details["target_repair"] = {
        "kind": "target_binding_repair",
        "target_repair_version": repair["target_repair_version"],
        "candidate_id": repair["candidate_id"],
        "raw_event_id": evidence.get("raw_event_id"),
        "tree_path": evidence.get("tree_path"),
        "tree_sha256": evidence.get("tree_sha256"),
        "tree_node_id": evidence.get("tree_node_id"),
        "tree_delay_ms": evidence.get("tree_delay_ms"),
    }
    return event


def _merge_actions(actions, record):
    action_ids = list(record.get("action_ids") or ())
    indexes = [
        index
        for index, action in enumerate(actions)
        if action.get("id") in action_ids and action.get("included", True)
    ]
    if len(indexes) < 2:
        return actions
    selected = [actions[index] for index in indexes]
    payload = record.get("payload") or {}
    event_ids = [
        event_id
        for action in selected
        for event_id in action.get("event_ids") or ()
    ]
    merged = {
        "id": _merged_action_id(record["edit_id"]),
        "type": payload.get("type") or selected[-1].get("type") or "compound",
        "event_ids": event_ids,
        "source_action_ids": [action["id"] for action in selected],
        "start_ms": min(
            (action.get("start_ms") for action in selected if action.get("start_ms") is not None),
            default=None,
        ),
        "end_ms": max(
            (action.get("end_ms") for action in selected if action.get("end_ms") is not None),
            default=None,
        ),
        "target": payload.get("target") or selected[-1].get("target"),
        "role": payload.get("role") or selected[-1].get("role") or "business",
        "included": True,
        "note": payload.get("note", ""),
        "merge_sources": [
            {
                "id": action.get("id"),
                "ordinal": action.get("ordinal"),
                "type": action.get("type"),
                "target": copy.deepcopy(action.get("target") or {}),
            }
            for action in selected
        ],
    }
    if selected[-1].get("source"):
        merged["source"] = copy.deepcopy(selected[-1]["source"])
    if payload.get("binding"):
        merged["value_binding"] = payload["binding"]
    first_index = min(indexes)
    result = []
    for index, action in enumerate(actions):
        if index == first_index:
            result.append(merged)
        if index not in indexes:
            result.append(action)
    return result


def _reindex_actions(actions):
    result = []
    for ordinal, action in enumerate(actions, start=1):
        action = dict(action)
        action["ordinal"] = ordinal
        result.append(action)
    return result


def _timeline_revision(edits):
    return stable_digest(
        *(
            json.dumps(record, ensure_ascii=False, sort_keys=True)
            for record in edits
        ),
        length=16,
    )


def _merged_action_id(edit_id):
    return "action-merge-" + stable_digest(edit_id, length=12)


def _action_source_key(action):
    source = action.get("source") or {}
    if source.get("kind") == "supplement":
        return (
            "supplement",
            source.get("supplement_id"),
            source.get("path"),
        )
    return ("take",)


def _materialize_locator_bundle(bundle, effective_path, actions):
    event_ids = {
        event_id
        for action in actions
        for event_id in action.get("event_ids") or ()
    }
    event_targets = [
        target
        for target in bundle.get("event_targets") or []
        if target.get("event_id") in event_ids
    ]
    locator_names = {
        target.get("locator_name")
        for target in event_targets
        if target.get("locator_name")
    }
    locators = {
        name: locator
        for name, locator in (bundle.get("locators") or {}).items()
        if name in locator_names
    }
    root_names = {
        value
        for locator in locators.values()
        for key, value in locator.items()
        if key in ("root", "region") and value
    }
    roots = {
        name: locator
        for name, locator in (bundle.get("roots") or {}).items()
        if name in root_names
    }
    effective = {
        **bundle,
        "source": "effective_timeline",
        "roots": roots,
        "locators": locators,
        "event_targets": event_targets,
        "unresolved": [
            target
            for target in bundle.get("unresolved") or []
            if target.get("event_id") in event_ids
        ],
    }
    RecordingSessionWriter._write_yaml_path(effective_path, effective)


def _supplement_sources(actions):
    result = []
    seen = set()
    for action in actions:
        source = action.get("source") or {}
        supplement_id = source.get("supplement_id")
        if source.get("kind") != "supplement" or not supplement_id:
            continue
        if supplement_id in seen:
            continue
        seen.add(supplement_id)
        result.append(source)
    return result


def _merge_named_bundle_values(destination, additions, suffix):
    names = {}
    for name, value in additions.items():
        candidate = str(name)
        if candidate in destination and destination[candidate] != value:
            candidate = f"{candidate}_{suffix}"
            index = 2
            while candidate in destination and destination[candidate] != value:
                candidate = f"{name}_{suffix}_{index}"
                index += 1
        destination.setdefault(candidate, copy.deepcopy(value))
        names[name] = candidate
    return names


def _remap_target_names(target, root_names, locator_names):
    target = copy.deepcopy(target)
    if target.get("locator_name") in locator_names:
        target["locator_name"] = locator_names[target["locator_name"]]
    candidate = target.get("selected_candidate") or {}
    if candidate.get("name") in locator_names:
        candidate["name"] = locator_names[candidate["name"]]
    locator = candidate.get("locator") or {}
    for key in ("root", "region"):
        if locator.get(key) in root_names:
            locator[key] = root_names[locator[key]]
    return target
