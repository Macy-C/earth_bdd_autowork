"""Zero-DB file store manager for UI Inventory (`manifest.json`, `snapshots/`, `repos/`, `buildlists/`, `dist/`)."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import uuid
import zipfile

from autowork_core.ui_inventory import SCHEMA_VERSION
from autowork_core.ui_inventory.scanner import SCANNER_SCHEMA_VERSION
from autowork_core.ui_inventory.scanner.repo_scanner import read_snapshot_file, write_snapshot_file

_TRANSIENT_WINERRORS = frozenset({5, 32, 33, 145})  # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION, DIR_NOT_EMPTY


def _is_transient_fs_lock_error(exc: OSError) -> bool:
    """True when Windows Defender / AV / Search Indexer briefly holds a file/dir handle."""
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    if exc.errno in (errno.EACCES, errno.EBUSY, errno.EPERM):
        return True
    return False


def _win_long_path(path_str: str) -> str:
    """Prefix absolute Windows paths with `\\\\?\\` so deep directory trees (>260 chars) can be removed cleanly."""
    if os.name != "nt":
        return path_str
    abs_p = os.path.abspath(path_str)
    if abs_p.startswith("\\\\?\\"):
        return abs_p
    if abs_p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_p[2:]
    return "\\\\?\\" + abs_p


def safe_rmtree(path: Path, *, max_wait_sec: float = 10.0) -> None:
    """Recursively remove a directory tree safely on Windows (long paths + read-only files + AV lock retry)."""
    raw_target = _win_long_path(str(path))
    if not os.path.exists(raw_target):
        return

    deadline = time.monotonic() + max(0.0, float(max_wait_sec))
    delay = 0.1
    last_exc: Optional[OSError] = None

    while os.path.exists(raw_target):
        last_exc = None

        def _on_rm_error(func: Any, target: str, exc_info: Any) -> None:
            nonlocal last_exc
            long_t = _win_long_path(target)
            try:
                os.chmod(long_t, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            try:
                func(long_t)
            except OSError as inner_exc:
                last_exc = inner_exc

        try:
            shutil.rmtree(raw_target, onerror=_on_rm_error)
        except OSError as exc:
            last_exc = exc

        if not os.path.exists(raw_target):
            return
        now = time.monotonic()
        if now >= deadline or (last_exc is not None and not _is_transient_fs_lock_error(last_exc)):
            if last_exc is not None:
                raise last_exc
            raise PermissionError(f"Failed to remove directory tree: {path}")
        time.sleep(min(delay, max(0.05, deadline - now)))
        delay = min(delay * 1.8, 1.5)


def safe_replace_path(src: Path, dst: Path, *, max_wait_sec: float = 20.0) -> None:
    """Move/replace a file or directory with Windows AV/indexer transient-lock retry (`WinError 5/32/33/145`)."""
    raw_src = _win_long_path(str(src))
    raw_dst = _win_long_path(str(dst))
    is_dir = os.path.isdir(raw_src)
    deadline = time.monotonic() + max(0.0, float(max_wait_sec))
    delay = 0.15

    while True:
        try:
            if is_dir:
                if os.path.exists(raw_dst):
                    safe_rmtree(dst, max_wait_sec=max(1.0, deadline - time.monotonic()))
                os.rename(raw_src, raw_dst)
            else:
                os.replace(raw_src, raw_dst)
            return
        except OSError as exc:
            now = time.monotonic()
            if not _is_transient_fs_lock_error(exc) or now >= deadline:
                raise
            time.sleep(min(delay, max(0.05, deadline - now)))
            delay = min(delay * 1.6, 2.0)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # ERROR_ACCESS_DENIED (5) means process exists under another privilege; ERROR_INVALID_PARAMETER (87) means dead
            err = kernel32.GetLastError()
            return err == 5
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class StoreLockError(RuntimeError):
    """Raised when another active `ui_inventory` process holds the exclusive store lock."""


class StoreLock:
    """Process-level exclusive lock (`<store_dir>/.poll-sync.lock`) with automatic stale-lock takeover."""

    def __init__(self, store_dir: Path, lock_name: str = ".poll-sync.lock") -> None:
        self.store_dir = store_dir.resolve()
        self.lock_path = self.store_dir / lock_name
        self._token = uuid.uuid4().hex
        self._acquired = False

    def acquire(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        host = socket.gethostname()
        pid = os.getpid()
        payload = json.dumps(
            {
                "pid": pid,
                "host": host,
                "started_at": _utc_now_iso(),
                "token": self._token,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        for _ in range(3):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                info = self._read_lock_info()
                if self._can_takeover_stale(info, current_host=host):
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise StoreLockError(
                    f"Another ui_inventory run is already active on store '{self.store_dir}' "
                    f"(lock={self.lock_path.name}, pid={info.get('pid')}, host={info.get('host')}, "
                    f"started_at={info.get('started_at')})."
                )
        raise StoreLockError(f"Failed to acquire exclusive store lock after stale cleanup: {self.lock_path}")

    def _read_lock_info(self) -> Dict[str, Any]:
        try:
            raw = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _can_takeover_stale(self, info: Dict[str, Any], *, current_host: str) -> bool:
        if not info:
            try:
                age = time.time() - self.lock_path.stat().st_mtime
                return age > 60.0
            except OSError:
                return True
        lock_host = str(info.get("host") or "")
        try:
            lock_pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            lock_pid = 0
        if lock_host == current_host and lock_pid > 0:
            return not _is_pid_alive(lock_pid)
        return False

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        try:
            info = self._read_lock_info()
            if not info or info.get("token") == self._token:
                self.lock_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "StoreLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        self.release()


def _normalize_cache_mode(raw_mode: Any) -> str:
    mode = str(raw_mode or "persistent").strip().lower()
    if mode not in ("persistent", "ephemeral"):
        raise ValueError(f"Invalid repo_cache_mode {raw_mode!r}; expected 'persistent' or 'ephemeral'")
    return mode


@dataclass
class RepoConfigEntry:
    id: str
    url: str = ""
    branch: str = "master"
    role: str = "app"  # buildlist | shell | framework | app | third_party
    local_path: str = ""
    enabled: bool = True


@dataclass
class ReposRegistryConfig:
    store_dir: str = "artifacts/ui-inventory"
    poll_interval_seconds: int = 300
    default_locale: str = "en-US"
    repo_cache_mode: str = "persistent"  # persistent | ephemeral
    weknora_ui_kb_id: str = ""
    build_lists: List[str] = field(default_factory=list)
    exclude_path_patterns: List[str] = field(default_factory=list)
    reference_dll_dirs: List[str] = field(default_factory=list)
    repos: List[RepoConfigEntry] = field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_ci_dict(mapping: Any) -> Dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in mapping.items()}


def _parse_repo_entries(raw_entries: Any) -> List[RepoConfigEntry]:
    repos_list: List[RepoConfigEntry] = []
    if not isinstance(raw_entries, list):
        return repos_list
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        norm = _norm_ci_dict(item)
        rid = norm.get("repo_id") or norm.get("id")
        if not rid:
            continue
        enabled_raw = norm.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled_val = enabled_raw.strip().lower() not in ("0", "false", "no", "n")
        else:
            enabled_val = bool(enabled_raw)
        repos_list.append(
            RepoConfigEntry(
                id=str(rid).strip(),
                url=str(norm.get("url", "") or "").strip(),
                branch=str(norm.get("branch", "master") or "master").strip(),
                role=str(norm.get("role", "app") or "app").strip(),
                local_path=str(norm.get("local_path", "") or "").strip(),
                enabled=enabled_val,
            )
        )
    return repos_list


def load_repos_config_from_settings(settings_obj: Any = None) -> ReposRegistryConfig:
    """Build `ReposRegistryConfig` directly from project `Bdd/config.yaml` (`UI_INVENTORY_SETTING` + `WEKNORA_SETTING`)."""
    if settings_obj is None:
        from config.settings import settings as settings_obj

    return ReposRegistryConfig(
        store_dir=str(getattr(settings_obj, "ui_inventory_store_dir", "artifacts/ui-inventory") or "artifacts/ui-inventory").strip(),
        poll_interval_seconds=300,
        default_locale=str(getattr(settings_obj, "ui_inventory_default_locale", "en-US") or "en-US").strip(),
        repo_cache_mode=_normalize_cache_mode(getattr(settings_obj, "ui_inventory_repo_cache_mode", "persistent")),
        weknora_ui_kb_id=str(getattr(settings_obj, "weknora_static_ui_docs_kb", "") or "").strip(),
        build_lists=list(getattr(settings_obj, "ui_inventory_build_lists", []) or []),
        exclude_path_patterns=list(getattr(settings_obj, "ui_inventory_exclude_path_patterns", []) or []),
        reference_dll_dirs=list(getattr(settings_obj, "ui_inventory_reference_dll_dirs", []) or []),
        repos=_parse_repo_entries(getattr(settings_obj, "ui_inventory_repositories", []) or []),
    )


def load_repos_config(config_path: Path) -> ReposRegistryConfig:
    """Load either a project `Bdd/config.yaml` (`UI_INVENTORY_SETTING`) or a standalone `repos.yaml`."""
    raw_text = config_path.read_text(encoding="utf-8")
    data: Dict[str, Any]
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(raw_text) or {}
    except ImportError:
        data = json.loads(raw_text)

    norm_root = _norm_ci_dict(data)

    # Case 1: Unified project `Bdd/config.yaml` format containing `UI_INVENTORY_SETTING`
    if "ui_inventory_setting" in norm_root:
        ui_sec = _norm_ci_dict(norm_root.get("ui_inventory_setting"))
        wk_sec = _norm_ci_dict(norm_root.get("weknora_setting"))
        kb_sec = _norm_ci_dict(wk_sec.get("knowledge_bases"))
        raw_entries = ui_sec.get("repositories")
        if raw_entries is None:
            raw_entries = ui_sec.get("repos") or []
        return ReposRegistryConfig(
            store_dir=str(ui_sec.get("store_dir", "artifacts/ui-inventory") or "artifacts/ui-inventory").strip(),
            poll_interval_seconds=int(ui_sec.get("poll_interval_seconds", 300)),
            default_locale=str(ui_sec.get("default_locale", "en-US") or "en-US").strip(),
            repo_cache_mode=_normalize_cache_mode(ui_sec.get("repo_cache_mode", "persistent")),
            weknora_ui_kb_id=str(kb_sec.get("static_ui_docs", "") or "").strip(),
            build_lists=[str(x).strip() for x in (ui_sec.get("build_lists") or []) if str(x or "").strip()],
            exclude_path_patterns=[
                str(x).strip() for x in (ui_sec.get("exclude_path_patterns") or []) if str(x or "").strip()
            ],
            reference_dll_dirs=[
                str(x).strip() for x in (ui_sec.get("reference_dll_dirs") or []) if str(x or "").strip()
            ],
            repos=_parse_repo_entries(raw_entries),
        )

    # Case 2: Standalone `repos.yaml` format (backward compatible)
    weknora_sec = _norm_ci_dict(norm_root.get("weknora"))
    raw_entries = norm_root.get("repositories")
    if raw_entries is None:
        raw_entries = norm_root.get("repos") or []
    return ReposRegistryConfig(
        store_dir=str(norm_root.get("store_dir", "artifacts/ui-inventory") or "artifacts/ui-inventory").strip(),
        poll_interval_seconds=int(norm_root.get("poll_interval_seconds", 300)),
        default_locale=str(norm_root.get("default_locale", "en-US") or "en-US").strip(),
        repo_cache_mode=_normalize_cache_mode(norm_root.get("repo_cache_mode", "persistent")),
        weknora_ui_kb_id=str(weknora_sec.get("ui_kb_id", "") or "").strip(),
        build_lists=[str(x).strip() for x in (norm_root.get("build_lists") or []) if str(x or "").strip()],
        exclude_path_patterns=[
            str(x).strip() for x in (norm_root.get("exclude_path_patterns") or []) if str(x or "").strip()
        ],
        reference_dll_dirs=[
            str(x).strip() for x in (norm_root.get("reference_dll_dirs") or []) if str(x or "").strip()
        ],
        repos=_parse_repo_entries(raw_entries),
    )


def resolve_store_dir(
    explicit_store_dir: str = "",
    *,
    registry: Optional[ReposRegistryConfig] = None,
    settings_obj: Any = None,
) -> Path:
    """Resolve effective UI Inventory store directory (explicit arg > config/settings > `artifacts/ui-inventory`)."""
    raw = str(explicit_store_dir or "").strip()
    if not raw and registry is not None and registry.store_dir:
        raw = registry.store_dir
    if not raw:
        if settings_obj is None:
            try:
                from config.settings import settings as settings_obj
            except Exception:
                settings_obj = None
        if settings_obj is not None:
            raw = str(getattr(settings_obj, "ui_inventory_store_dir", "") or "").strip()
    if not raw:
        raw = "artifacts/ui-inventory"

    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    try:
        from config.paths import Paths
        return (Paths.BASE_DIR / candidate).resolve()
    except Exception:
        return candidate.resolve()


class SnapshotStore:
    """Manages `<store_dir>/manifest.json`, `snapshots/`, `repos/`, `buildlists/`, and `dist/`."""

    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir.resolve()
        self.snapshots_dir = self.store_dir / "snapshots"
        self.buildlists_dir = self.store_dir / "buildlists"
        self.repos_dir = self.store_dir / "repos"
        self.dist_dir = self.store_dir / "dist"
        self.manifest_path = self.store_dir / "manifest.json"

    def ensure_dirs(self) -> None:
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.buildlists_dir.mkdir(parents=True, exist_ok=True)
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self.dist_dir.mkdir(parents=True, exist_ok=True)

    def snapshot_path(self, repo_id: str) -> Path:
        return self.snapshots_dir / f"{repo_id}.snapshot.json.gz"

    def repo_cache_dir(self, repo_id: str) -> Path:
        return self.repos_dir / repo_id

    def cleanup_stale_staging_repos(self) -> int:
        """Remove any leftover `.staging_*` directories from interrupted clones."""
        if not self.repos_dir.is_dir():
            return 0
        cleaned = 0
        for item in sorted(self.repos_dir.iterdir(), key=lambda p: p.name.lower()):
            if item.is_dir() and item.name.startswith(".staging_"):
                try:
                    safe_rmtree(item)
                    cleaned += 1
                except OSError:
                    pass
        return cleaned

    def list_cached_repo_dirs(self) -> List[str]:
        if not self.repos_dir.is_dir():
            return []
        return sorted(
            p.name
            for p in self.repos_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def sync_repo_build_lists(self, repo_id: str, repo_root: Path) -> Tuple[List[Path], bool]:
        """Cache root/depth-1 `*BuildList*` files from `repo_root` into `<store>/buildlists/<repo_id>/`."""
        repo_bl_dir = self.buildlists_dir / repo_id
        found: Dict[str, bytes] = {}
        try:
            top_entries = sorted(repo_root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            top_entries = []

        candidates: List[Tuple[str, Path]] = []
        for entry in top_entries:
            if entry.name.startswith("."):
                continue
            if entry.is_file():
                if "buildlist" in entry.name.lower() and entry.suffix == "":
                    candidates.append((entry.name, entry))
            elif entry.is_dir() and entry.name.lower() not in {".git", "obj", "bin", "packages", "node_modules"}:
                try:
                    for sub in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                        if sub.is_file() and "buildlist" in sub.name.lower() and sub.suffix == "":
                            candidates.append((f"{entry.name}__{sub.name}", sub))
                except OSError:
                    continue

        for cache_name, src_path in candidates:
            try:
                if src_path.stat().st_size <= 512 * 1024:
                    found[cache_name] = src_path.read_bytes()
            except OSError:
                continue

        changed = False
        if not found:
            if repo_bl_dir.is_dir():
                safe_rmtree(repo_bl_dir)
                changed = True
            return [], changed

        repo_bl_dir.mkdir(parents=True, exist_ok=True)
        existing_names = {p.name for p in repo_bl_dir.iterdir() if p.is_file()}
        for stale in existing_names - set(found.keys()):
            try:
                (repo_bl_dir / stale).unlink()
                changed = True
            except OSError:
                pass

        written: List[Path] = []
        for cname, payload in sorted(found.items()):
            dst = repo_bl_dir / cname
            if not dst.is_file() or dst.read_bytes() != payload:
                dst.write_bytes(payload)
                changed = True
            written.append(dst)
        return written, changed

    def list_cached_build_lists(self) -> List[Path]:
        if not self.buildlists_dir.is_dir():
            return []
        return sorted(p for p in self.buildlists_dir.rglob("*") if p.is_file())

    def compute_buildlists_fingerprint(self, explicit_paths: Sequence[Path] = ()) -> str:
        """Compute deterministic SHA-256 over all effective BuildList files (explicit + cached)."""
        seen: set[str] = set()
        entries: List[Tuple[str, str]] = []
        for p in [*explicit_paths, *self.list_cached_build_lists()]:
            if not p.is_file():
                continue
            key = str(p.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            entries.append((p.name.lower(), digest))
        entries.sort()
        return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def compute_repo_scan_fingerprint(
        *,
        commit: str,
        branch: str,
        exclude_path_patterns: Sequence[str],
        buildlists_fingerprint: str,
        scanner_version: str = SCANNER_SCHEMA_VERSION,
    ) -> str:
        """Compute complete Layer-1 input fingerprint so config/BuildList/scanner changes trigger rescan cleanly."""
        norm_excludes = sorted({str(p).strip().lower() for p in exclude_path_patterns if str(p or "").strip()})
        payload = {
            "commit": str(commit or ""),
            "branch": str(branch or ""),
            "exclude_path_patterns": norm_excludes,
            "buildlists_fingerprint": str(buildlists_fingerprint or ""),
            "scanner_version": str(scanner_version or ""),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def compute_build_fingerprint(
        self,
        active_repo_ids: Iterable[str],
        default_locale: str,
        *,
        builder_version: str = SCHEMA_VERSION,
        reference_dll_dirs: Sequence[str] = (),
    ) -> str:
        """Compute Layer-2 build fingerprint directly from on-disk snapshots; returns empty string if incomplete."""
        pairs: List[Tuple[str, str]] = []
        for rid in sorted(set(active_repo_ids)):
            snap_file = self.snapshot_path(rid)
            if not snap_file.is_file():
                return ""
            snap_sha = hashlib.sha256(snap_file.read_bytes()).hexdigest()
            pairs.append((rid, snap_sha))
        from autowork_core.ui_inventory.scanner.repo_scanner import SKIP_DLL_PREFIXES
        from autowork_core.ui_inventory.scanner.scope_gate import iter_reference_dll_files

        dll_files, _ = iter_reference_dll_files(
            [Path(d) for d in reference_dll_dirs if str(d or "").strip()],
            skip_dll_prefixes=SKIP_DLL_PREFIXES,
        )
        ref_sigs: List[Tuple[str, int, int]] = []
        for dll_p, rel_lbl in dll_files:
            try:
                st = dll_p.stat()
                ref_sigs.append((rel_lbl.lower(), st.st_size, st.st_mtime_ns))
            except OSError:
                pass
        payload: Dict[str, Any] = {
            "repos": pairs,
            "locale": str(default_locale or "en-US"),
            "builder_version": str(builder_version or ""),
        }
        if ref_sigs:
            payload["reference_dlls"] = ref_sigs
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def load_manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.is_file():
            return {
                "schema_version": SCHEMA_VERSION,
                "updated_at": "",
                "repos": {},
                "last_build": {},
                "published_kb_docs": {},
            }
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: Dict[str, Any]) -> None:
        self.ensure_dirs()
        manifest["schema_version"] = SCHEMA_VERSION
        manifest["updated_at"] = _utc_now_iso()
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        safe_replace_path(tmp, self.manifest_path, max_wait_sec=10.0)

    def save_repo_snapshot(
        self,
        snapshot: Dict[str, Any],
        *,
        scan_fingerprint: str = "",
    ) -> Dict[str, Any]:
        self.ensure_dirs()
        repo_id = snapshot["repo_id"]
        out_file = self.snapshot_path(repo_id)
        sha256_hex = write_snapshot_file(snapshot, out_file)

        manifest = self.load_manifest()
        repos_sec = manifest.setdefault("repos", {})
        repos_sec[repo_id] = {
            "commit": snapshot["commit"],
            "branch": snapshot.get("branch", ""),
            "scan_fingerprint": scan_fingerprint,
            "snapshot_file": f"snapshots/{out_file.name}",
            "snapshot_sha256": sha256_hex,
            "stats": snapshot.get("stats", {}),
            "scanned_at": _utc_now_iso(),
        }
        self.save_manifest(manifest)
        return repos_sec[repo_id]

    def get_last_build_fingerprint(self) -> str:
        manifest = self.load_manifest()
        last_build = manifest.get("last_build") or {}
        return str(last_build.get("build_fingerprint") or "")

    def record_build_state(self, build_fingerprint: str, summary: Dict[str, Any]) -> None:
        manifest = self.load_manifest()
        manifest["last_build"] = {
            "build_fingerprint": build_fingerprint,
            "built_at": _utc_now_iso(),
            "kb_docs_count": summary.get("kb_docs_count", 0),
            "interactive_controls": summary.get("interactive_controls", 0),
            "surfaces": summary.get("surfaces", 0),
        }
        self.save_manifest(manifest)

    def load_repo_snapshot(self, repo_id: str) -> Dict[str, Any]:
        return read_snapshot_file(self.snapshot_path(repo_id))

    def remove_repo_snapshot(self, repo_id: str) -> bool:
        """Delete a repo's cached snapshot, persistent repo cache, cached buildlists, and manifest entry."""
        removed = False
        snap_file = self.snapshot_path(repo_id)
        if snap_file.is_file():
            snap_file.unlink()
            removed = True
        repo_bl_dir = self.buildlists_dir / repo_id
        if repo_bl_dir.is_dir():
            safe_rmtree(repo_bl_dir)
            removed = True
        repo_git_dir = self.repo_cache_dir(repo_id)
        if repo_git_dir.is_dir():
            safe_rmtree(repo_git_dir)
            removed = True
        manifest = self.load_manifest()
        repos_sec = manifest.get("repos") or {}
        if repo_id in repos_sec:
            repos_sec.pop(repo_id, None)
            manifest["repos"] = repos_sec
            self.save_manifest(manifest)
            removed = True
        return removed

    def list_available_repo_ids(self) -> List[str]:
        if not self.snapshots_dir.is_dir():
            return []
        ids: List[str] = []
        for p in sorted(self.snapshots_dir.glob("*.snapshot.json.gz")):
            ids.append(p.name[: -len(".snapshot.json.gz")])
        return ids


def export_standalone_scanner_pyz(out_pyz: Path) -> str:
    """Package `autowork_core/ui_inventory/scanner/` into a zero-dependency `.pyz` zipapp."""
    scanner_pkg_dir = Path(__file__).resolve().parent / "scanner"
    out_pyz.parent.mkdir(parents=True, exist_ok=True)
    tmp_pyz = out_pyz.with_suffix(".pyz.tmp")

    entry_script = (
        "from autowork_core.ui_inventory.scanner.cli_standalone import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )
    with zipfile.ZipFile(tmp_pyz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for init_rel in ("autowork_core/__init__.py", "autowork_core/ui_inventory/__init__.py"):
            zi = zipfile.ZipInfo(init_rel, date_time=(2026, 1, 1, 0, 0, 0))
            zf.writestr(zi, f'SCHEMA_VERSION = "{SCHEMA_VERSION}"\n')
        for py_file in sorted(scanner_pkg_dir.glob("*.py")):
            arcname = f"autowork_core/ui_inventory/scanner/{py_file.name}"
            zi = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, py_file.read_bytes())
        zi_main = zipfile.ZipInfo("__main__.py", date_time=(2026, 1, 1, 0, 0, 0))
        zi_main.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi_main, entry_script.encode("utf-8"))

    safe_replace_path(tmp_pyz, out_pyz, max_wait_sec=10.0)
    return hashlib.sha256(out_pyz.read_bytes()).hexdigest()
