from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.writer import (
    atomic_write_path_probe,
    write_json_atomic,
)


PROJECTION_VERSION = "5.7"
SUPPORTED_PROJECTION_VERSIONS = {PROJECTION_VERSION}
PROJECTION_ROOT_NAME = "p"
PROJECTION_TEMP_PREFIX = ".tmp-"
PROJECTION_TEMP_TOKEN_LENGTH = 8
_LOAD_CURRENT = object()
_LOCKS_GUARD = threading.Lock()
_OWNER_LOCKS = {}


@dataclass(frozen=True)
class ProjectionSnapshot:
    projection_version: str
    projection_revision: str
    source_revision: str
    directory: Path
    artifacts: dict[str, str]

    def path(self, key):
        relative = self.artifacts.get(key)
        return self.directory / relative if relative else None

    @property
    def is_current_version(self):
        return self.projection_version == PROJECTION_VERSION


class ProjectionStore:
    """Publishes immutable derived artifacts behind one atomic pointer."""

    @staticmethod
    def longest_write_path(owner_dir):
        root = Path(owner_dir) / PROJECTION_ROOT_NAME
        temporary = root / (
            f"{PROJECTION_TEMP_PREFIX}"
            f"{'0' * PROJECTION_TEMP_TOKEN_LENGTH}"
        )
        final = root / ProjectionStore.directory_name("0" * 20)
        candidates = (
            atomic_write_path_probe(
                temporary / "locator-candidates.effective.yaml"
            ),
            final / "locator-candidates.effective.yaml",
            final / "pic" / f"pic-{'0' * 16}.png",
        )
        return max(candidates, key=lambda path: len(str(path)))

    @staticmethod
    def directory_name(projection_revision):
        raw = bytes.fromhex(str(projection_revision))
        return base64.b32encode(raw).decode("ascii").rstrip("=").lower()

    @classmethod
    def relative_directory(cls, projection_revision):
        return (
            Path(PROJECTION_ROOT_NAME)
            / cls.directory_name(projection_revision)
        ).as_posix()

    def __init__(self, owner_dir):
        self.owner_dir = Path(owner_dir).resolve()
        self.root = self.owner_dir / PROJECTION_ROOT_NAME
        self.pointer_path = self.owner_dir / "current-projection.json"
        with _LOCKS_GUARD:
            self._lock = _OWNER_LOCKS.setdefault(
                str(self.owner_dir),
                threading.RLock(),
            )

    def publish(self, source_revision, builder, *, required=()):
        with self._lock:
            return self._publish(source_revision, builder, required=required)

    def _publish(self, source_revision, builder, *, required=()):
        source_revision = str(source_revision)
        projection_revision = self.revision_for(source_revision)
        final_dir = self.owner_dir / self.relative_directory(
            projection_revision
        )
        self.root.mkdir(parents=True, exist_ok=True)
        if final_dir.exists():
            try:
                self._load_snapshot(final_dir, verify_hashes=True)
            except (OSError, ValueError, json.JSONDecodeError):
                shutil.rmtree(final_dir, ignore_errors=True)
        if not final_dir.exists():
            temporary = Path(tempfile.mkdtemp(
                prefix=PROJECTION_TEMP_PREFIX,
                dir=self.root,
            )
            )
            try:
                artifacts = _normalize_artifacts(builder(temporary))
                _validate_artifacts(temporary, artifacts, required)
                manifest = {
                    "projection_version": PROJECTION_VERSION,
                    "projection_revision": projection_revision,
                    "source_revision": source_revision,
                    "artifacts": artifacts,
                    "hashes": {
                        key: _sha256(temporary / relative)
                        for key, relative in artifacts.items()
                    },
                }
                write_json_atomic(temporary / "projection.json", manifest)
                self._promote_directory(temporary, final_dir)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        snapshot = self._load_snapshot(final_dir, verify_hashes=True)
        write_json_atomic(self.pointer_path, {
            "projection_version": PROJECTION_VERSION,
            "projection_revision": snapshot.projection_revision,
            "source_revision": snapshot.source_revision,
            "path": snapshot.directory.relative_to(self.owner_dir).as_posix(),
            "artifacts": snapshot.artifacts,
        })
        self.cleanup_temporary()
        return snapshot

    def _promote_directory(self, temporary, final_dir):
        try:
            os.replace(temporary, final_dir)
            return
        except PermissionError:
            pass
        if final_dir.exists():
            try:
                self._load_snapshot(final_dir, verify_hashes=True)
            except (OSError, ValueError, json.JSONDecodeError):
                shutil.rmtree(final_dir, ignore_errors=True)
            else:
                shutil.rmtree(temporary, ignore_errors=True)
                return
        try:
            shutil.copytree(temporary, final_dir)
            self._load_snapshot(final_dir, verify_hashes=True)
        except Exception:
            shutil.rmtree(final_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def revision_for(source_revision, *, version=PROJECTION_VERSION):
        return stable_digest(
            str(source_revision),
            str(version),
            length=20,
        )

    def current(self):
        if not self.pointer_path.exists():
            return None
        try:
            pointer = json.loads(
                self.pointer_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        relative = pointer.get("path")
        if not relative:
            return None
        directory = (self.owner_dir / relative).resolve()
        if not _is_relative_to(directory, self.root):
            return None
        if not directory.exists():
            return None
        try:
            return self._load_snapshot(directory, verify_hashes=True)
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def artifact_path(self, key, *, snapshot=_LOAD_CURRENT):
        if snapshot is _LOAD_CURRENT:
            snapshot = self.current()
        if snapshot is not None:
            path = snapshot.path(key)
            if path is not None:
                return path
        return None

    def cleanup_temporary(self):
        if not self.root.exists():
            return
        for path in self.root.glob(".tmp-*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def _load_snapshot(self, directory, *, verify_hashes):
        manifest = json.loads(
            (Path(directory) / "projection.json").read_text(encoding="utf-8")
        )
        version = str(manifest.get("projection_version") or "")
        if version not in SUPPORTED_PROJECTION_VERSIONS:
            raise ValueError("投影版本不匹配")
        source_revision = str(manifest["source_revision"])
        projection_revision = str(manifest["projection_revision"])
        if projection_revision != self.revision_for(
            source_revision,
            version=version,
        ):
            raise ValueError("投影 revision 不匹配")
        artifacts = _normalize_artifacts(manifest.get("artifacts") or {})
        _validate_artifacts(directory, artifacts, artifacts)
        if verify_hashes:
            hashes = manifest.get("hashes") or {}
            for key, relative in artifacts.items():
                if hashes.get(key) != _sha256(Path(directory) / relative):
                    raise ValueError(f"投影 artifact hash 不匹配: {key}")
        return ProjectionSnapshot(
            projection_version=version,
            projection_revision=projection_revision,
            source_revision=source_revision,
            directory=Path(directory).resolve(),
            artifacts=artifacts,
        )


def resolve_take_artifact(take_dir, key):
    return ProjectionStore(Path(take_dir).resolve()).artifact_path(key)


def _is_relative_to(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_artifacts(artifacts):
    return {
        str(key): Path(relative).as_posix()
        for key, relative in dict(artifacts or {}).items()
    }


def _validate_artifacts(directory, artifacts, required):
    directory = Path(directory).resolve()
    for key in required:
        if key not in artifacts:
            raise ValueError(f"投影缺少 artifact 声明: {key}")
    for key, relative in artifacts.items():
        path = (directory / relative).resolve()
        try:
            path.relative_to(directory)
        except ValueError as error:
            raise ValueError(f"投影 artifact 路径越界: {key}") from error
        if not path.is_file():
            raise FileNotFoundError(f"投影 artifact 不存在: {key}: {path}")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()