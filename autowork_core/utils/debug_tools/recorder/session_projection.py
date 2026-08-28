from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from autowork_core.utils.debug_tools.recorder.analysis import (
    EXCLUDED_LOCATOR_METHODS,
    LOCATOR_PRIORITY,
)
from autowork_core.utils.debug_tools.recorder.annotations import (
    RecordingAnnotationRepository,
)
from autowork_core.utils.debug_tools.recorder.bundle_validator import validate_ai_bundle
from autowork_core.utils.debug_tools.recorder.catalog import update_recording_catalog
from autowork_core.utils.debug_tools.recorder.generation_contract import (
    build_generation_contract,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION, public_dict
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.writer import RecordingSessionWriter
from config.settings import settings


@dataclass(frozen=True)
class SessionProjectionSource:
    run_id: str
    created_at: str | None
    updated_at: str | None
    finalized_at: str | None
    closed_at: str | None
    is_recording: bool
    feature_plan: Any
    scenario_plan: Any
    config: Any
    selected_steps: tuple[Any, ...]
    step_states: dict[str, dict]
    annotation_model_version: str | None = None
    environment: dict | None = None


class SessionProjectionBuilder:
    def __init__(self, session_dir, output_root, writer=None):
        self.session_dir = Path(session_dir).resolve()
        self.output_root = Path(output_root).resolve()
        self.writer = writer or RecordingSessionWriter(self.session_dir)

    def write_source(self, source):
        return self.write_manifest(self.build_manifest(source))

    def write_manifest(self, manifest):
        self.writer.write_manifest(manifest)
        context, unresolved, locator_drafts, summary = self.build_ai_bundle(manifest)
        self.writer.write_ai_bundle(
            context,
            self.build_target_index(manifest, context),
            build_generation_contract(manifest),
            unresolved,
            locator_drafts,
            summary,
        )
        readiness = validate_ai_bundle(self.session_dir)
        self.writer.write_readiness(readiness)
        update_recording_catalog(
            self.output_root,
            self.session_dir,
            manifest,
            readiness=readiness,
        )
        return readiness

    def build_manifest(self, source):
        completed = sum(
            state["status"] == "completed"
            for state in source.step_states.values()
        )
        skipped = sum(
            state["status"] == "skipped"
            for state in source.step_states.values()
        )
        status = (
            "finalized"
            if source.finalized_at is not None
            else "closed"
            if source.closed_at is not None
            else "recording"
            if source.is_recording
            else "open"
        )
        environment = source.environment or {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "backend": source.config.backend,
            "desktop_size": list(settings.desktop_size),
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "session_type": "feature_guided_step_recording",
            "session_id": source.run_id,
            "created_at": source.created_at,
            "updated_at": source.updated_at,
            "finalized_at": source.finalized_at,
            "closed_at": source.closed_at,
            "status": status,
            "source_feature": "source.feature",
            "source_hash": source.feature_plan.source_hash,
            "feature": {
                "id": source.feature_plan.id,
                "key": source.feature_plan.key,
                "name": source.feature_plan.name,
                "line": source.feature_plan.line,
                "tags": list(source.feature_plan.tags),
                "source_relpath": source.feature_plan.source_relpath,
                "description": list(source.feature_plan.description),
            },
            "scenario": public_dict(source.scenario_plan),
            "capture_config": public_dict(source.config),
            "environment": environment,
            "locator_policy": {
                "priority": list(LOCATOR_PRIORITY),
                "excluded": list(EXCLUDED_LOCATOR_METHODS),
            },
            "progress": {
                "selected": len(source.selected_steps),
                "completed": completed,
                "skipped": skipped,
                "pending": len(source.selected_steps) - completed - skipped,
            },
            "steps": [
                {
                    "plan": public_dict(step),
                    **public_dict(source.step_states[step.id]),
                }
                for step in source.selected_steps
            ],
        }
        if source.annotation_model_version:
            manifest["annotation_model_version"] = (
                source.annotation_model_version
            )
        return manifest

    def build_ai_bundle(self, manifest):
        step_contexts = []
        unresolved = []
        roots = {}
        locators = {}
        annotations = RecordingAnnotationRepository(self.session_dir)
        for step_entry in manifest["steps"]:
            selected_take = step_entry.get("selected_take")
            take_entry = next(
                (
                    take
                    for take in step_entry.get("takes") or []
                    if take["id"] == selected_take
                ),
                None,
            )
            context = {
                "step": step_entry["plan"],
                "status": step_entry["status"],
                "selected_take": (
                    {
                        key: value
                        for key, value in take_entry.items()
                        if key not in {
                            "take_summary",
                            "discard_reason",
                            "note",
                        }
                    }
                    if take_entry
                    else None
                ),
            }
            step_user_context = annotations.current_step_context(
                step_entry["plan"]["id"]
            )
            if step_user_context is not None:
                context["step_user_context"] = step_user_context
            if take_entry:
                take_dir = self.session_dir / take_entry["path"]
                projection_store = ProjectionStore(take_dir)
                projection = projection_store.current()
                if projection is None:
                    raise ValueError(
                        f"Take 缺少有效 Projection 5.7: {take_entry['path']}"
                    )
                locator_path = projection.path(
                    "locator_candidates_effective"
                )
                if locator_path.exists():
                    bundle = yaml.safe_load(
                        locator_path.read_text(encoding="utf-8")
                    ) or {}
                    _merge_named_drafts(roots, bundle.get("roots") or {})
                    _merge_named_drafts(locators, bundle.get("locators") or {})
                    unresolved.extend(bundle.get("unresolved") or [])
                artifacts = {"take": take_entry["path"]}
                raw_artifact_paths = {
                    "take_metadata": "take.json",
                    "events": "events.jsonl",
                    "raw_events": "raw-events.jsonl",
                    "raw_events_seal": "raw-events.seal.json",
                    "capture_completion": "capture-completion.json",
                    "actions_auto": "actions.auto.json",
                    "timeline_edits": "timeline-edits.jsonl",
                    "tree_diff": "ui/tree-diff.json",
                    "locator_candidates_auto": "locator-candidates.auto.yaml",
                    "summary": "capture-summary.md",
                    "contact_sheet": "contact-sheet.png",
                    "video": "step.mp4",
                    "before_screenshot": "screenshots/before.png",
                    "after_screenshot": "screenshots/after.png",
                }
                for key, relative_path in raw_artifact_paths.items():
                    if (take_dir / relative_path).exists():
                        artifacts[key] = f"{take_entry['path']}/{relative_path}"
                projected_keys = {
                        "events_effective": "events_effective",
                        "actions": "actions_effective",
                        "actions_effective": "actions_effective",
                        "timeline_state": "timeline_state",
                        "locator_candidates": (
                            "locator_candidates_effective"
                        ),
                        "locator_candidates_effective": (
                            "locator_candidates_effective"
                        ),
                        "media_index": "media_index",
                        "action_media": "action_media",
                        "action_contact_sheet": "action_contact_sheet",
                        "evidence_graph": "evidence_graph",
                        "semantic_pack": "semantic_pack",
                        "pic_template_audit": "pic_template_audit",
                    }
                for key, projection_key in projected_keys.items():
                    path = projection.path(projection_key)
                    if path is not None and path.exists():
                        artifacts[key] = path.relative_to(
                            self.session_dir
                        ).as_posix()
                for key in projection.artifacts:
                    if not key.startswith("pic_template:"):
                        continue
                    path = projection.path(key)
                    if path is not None and path.exists():
                        artifacts[key] = path.relative_to(
                            self.session_dir
                        ).as_posix()
                context["artifacts"] = artifacts
                semantic_path = artifacts.get("semantic_pack")
                if semantic_path and (self.session_dir / semantic_path).is_file():
                    semantic_pack = json.loads(
                        (self.session_dir / semantic_path).read_text(
                            encoding="utf-8"
                        )
                    )
                    context["observation_intents"] = [
                        dict(item)
                        for item in semantic_pack.get(
                            "observation_intents"
                        ) or ()
                        if isinstance(item, dict)
                    ]
                take_metadata_path = take_dir / "take.json"
                if take_metadata_path.exists():
                    take_metadata = json.loads(
                        take_metadata_path.read_text(encoding="utf-8")
                    )
                    context["pauses"] = take_metadata.get("pauses") or []
                    context["target_windows"] = (
                        take_metadata.get("target_windows")
                        or [take_metadata.get("target_window") or {}]
                    )
                    context["window_evidence"] = (
                        take_metadata.get("window_evidence") or []
                    )
                    context["window_lifecycle"] = (
                        take_metadata.get("window_lifecycle") or []
                    )
            step_contexts.append(context)
        context = {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "id": manifest["session_id"],
                "relative_path": self.session_dir.relative_to(
                    self.output_root
                ).as_posix(),
            },
            "purpose": (
                "Generate Behave step definitions, locator YAML, page objects, "
                "and data drafts."
            ),
            "generation_contract": "generation-contract.json",
            "readiness": "readiness.json",
            "locator_policy": manifest["locator_policy"],
            "feature": manifest["feature"],
            "scenario": manifest["scenario"],
            "steps": step_contexts,
        }
        locator_drafts = {
            "schema_version": SCHEMA_VERSION,
            "policy": manifest["locator_policy"],
            "roots": roots,
            "locators": locators,
        }
        return (
            context,
            unresolved,
            locator_drafts,
            self.build_generation_summary(manifest, unresolved),
        )

    @staticmethod
    def build_target_index(manifest, context):
        selected_step_ids = [
            entry["step"]["id"]
            for entry in context["steps"]
            if entry.get("status") == "completed"
            and entry.get("selected_take")
        ]
        scenario_step_ids = [
            entry["id"]
            for entry in manifest["scenario"].get("steps") or []
        ]
        excluded_step_ids = [
            step_id
            for step_id in scenario_step_ids
            if step_id not in set(selected_step_ids)
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "session": context["session"],
            "feature": manifest["feature"],
            "scenarios": [
                {
                    "id": manifest["scenario"]["id"],
                    "key": manifest["scenario"]["key"],
                    "logical_template_id": manifest["scenario"].get(
                        "logical_template_id"
                    ) or manifest["scenario"]["id"],
                    "name": manifest["scenario"]["name"],
                    "kind": manifest["scenario"]["kind"],
                    "example_id": manifest["scenario"]["example_id"],
                    "example_values": manifest["scenario"]["example_values"],
                    "tags": list(manifest["scenario"].get("tags") or ()),
                    "specification": manifest["scenario"].get(
                        "specification"
                    ) or {},
                    "generation_scope": {
                        "kind": "scenario",
                        "complete": not excluded_step_ids,
                        "selected_step_ids": selected_step_ids,
                        "excluded_step_ids": excluded_step_ids,
                    },
                    "steps": [
                        {
                            "id": entry["step"]["id"],
                            "key": entry["step"]["key"],
                            "ordinal": entry["step"]["ordinal"],
                            "keyword": entry["step"]["keyword"],
                            "semantic_type": entry["step"].get(
                                "semantic_type"
                            ),
                            "text": entry["step"]["text"],
                            "status": entry["status"],
                            "selected_take": entry["selected_take"],
                            "artifacts": entry.get("artifacts"),
                        }
                        for entry in context["steps"]
                    ],
                }
            ],
        }

    @staticmethod
    def build_generation_summary(manifest, unresolved):
        progress = manifest["progress"]
        scenario_name = manifest["scenario"].get("display_name") or manifest[
            "scenario"
        ]["name"]
        lines = [
            f"# Recording summary: {manifest['feature']['name']}",
            "",
            f"Scenario: `{scenario_name}`",
            f"Completed: `{progress['completed']}/{progress['selected']}`",
            f"Skipped: `{progress['skipped']}`",
            f"Unresolved/container targets: `{len(unresolved)}`",
            "",
            (
                "Locator priority: `Child -> XPath -> OCR + Region -> POS`. "
                "`PIC` is default-deny and requires an action-bound Decision "
                "plus a passed template audit."
            ),
            "",
            (
                "Generate through RequestV3 Workflow State using its referenced "
                "Generation Brief, validated GenerationPlanV4.2, and "
                "`generation-contract.json`. Read Evidence Graph, media, UI trees, "
                "or raw Take artifacts only for forensic routing or a specific claim."
            ),
            "",
        ]
        return "\n".join(lines)


def rebuild_session_projections(session_dir, manifest=None, output_root=None):
    session_dir = Path(session_dir).resolve()
    if manifest is None:
        manifest = json.loads(
            (session_dir / "manifest.json").read_text(encoding="utf-8")
        )
    configured_root = (manifest.get("capture_config") or {}).get("output_root")
    builder = SessionProjectionBuilder(
        session_dir,
        output_root
        or find_recording_output_root(session_dir, configured_root),
    )
    return builder.write_manifest(manifest)


def find_recording_output_root(session_dir, configured_root=None):
    session_dir = Path(session_dir).resolve()
    for candidate in session_dir.parents:
        if (candidate / "catalog.json").exists():
            return candidate.resolve()
    if configured_root:
        return Path(configured_root).resolve()
    raise FileNotFoundError(
        f"无法从历史会话定位 recording_sessions 根目录: {session_dir}"
    )


def _merge_named_drafts(destination, additions):
    for name, value in additions.items():
        candidate = name
        index = 2
        while candidate in destination and destination[candidate] != value:
            candidate = f"{name}_{index}"
            index += 1
        destination[candidate] = value