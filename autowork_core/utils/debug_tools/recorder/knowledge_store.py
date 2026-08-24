from __future__ import annotations

import os
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.ai_paths import (
    PROJECT_AI_ROOT,
    PROJECT_KNOWLEDGE_ROOT,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


KNOWLEDGE_STORE_VERSION = "1.0"
_CAPABILITY_LOCKS = {}
_CAPABILITY_LOCKS_GUARD = threading.Lock()


def ai_root_for_recording_root(recording_root):
    recording_root = Path(recording_root).resolve()
    if recording_root.name == PROJECT_KNOWLEDGE_ROOT.name and (
            recording_root.parent.name == PROJECT_AI_ROOT.name
            and recording_root.parent.parent.name == PROJECT_AI_ROOT.parent.name
    ):
        return recording_root.parent
    if (
            recording_root.name == PROJECT_AI_ROOT.name
            and recording_root.parent.name == PROJECT_AI_ROOT.parent.name
    ):
        return recording_root
    if (
            recording_root.name == "recording_sessions"
            and recording_root.parent.name == "artifacts"
    ):
        return recording_root.parent.parent / PROJECT_AI_ROOT
    project_root = next(
        (
            candidate
            for candidate in (recording_root, *recording_root.parents)
            if (candidate / "Bdd").is_dir()
            and (candidate / "autowork_core").is_dir()
        ),
        None,
    )
    if project_root is None:
        project_root = recording_root.parent
    return project_root / PROJECT_AI_ROOT


def knowledge_root_for_recording_root(recording_root):
    return ai_root_for_recording_root(recording_root) / PROJECT_KNOWLEDGE_ROOT.name


def ensure_knowledge_store(recording_root):
    root = knowledge_root_for_recording_root(recording_root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    if not manifest.exists():
        write_json_atomic(manifest, {
            "knowledge_store_version": KNOWLEDGE_STORE_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "policy": {
                "purpose": "Durable advisory AI knowledge detached from Recorder Runs.",
                "runtime_evidence": False,
                "raw_media_allowed": False,
            },
        })
    return root


def migrate_legacy_knowledge_file(
        recording_root,
        *,
        legacy_relative,
        knowledge_relative,
):
    recording_root = Path(recording_root).resolve()
    root = ensure_knowledge_store(recording_root)
    target = _contained(root, root / knowledge_relative)
    if target.exists():
        return target
    source = _contained(recording_root, recording_root / legacy_relative)
    if not source.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.migrating")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def resolve_knowledge_path(recording_root, relative_path):
    root = knowledge_root_for_recording_root(recording_root).resolve()
    return _contained(root, root / str(relative_path or ""))


@contextmanager
def capability_store_lock(recording_root):
    root = knowledge_root_for_recording_root(recording_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = str(root).casefold()
    with _CAPABILITY_LOCKS_GUARD:
        state = _CAPABILITY_LOCKS.setdefault(key, {
            "thread_lock": threading.RLock(),
            "local": threading.local(),
        })
    with state["thread_lock"]:
        local = state["local"]
        depth = int(getattr(local, "depth", 0))
        if depth:
            local.depth = depth + 1
            try:
                yield
            finally:
                local.depth -= 1
            return
        path = root / ".capabilities.lock"
        stream = path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            acquired = True
            local.depth = 1
            yield
        finally:
            local.depth = 0
            try:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


def _contained(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"AI knowledge 路径越界: {path}") from error
    return path