"""Multi-repo incremental poller with persistent shallow Git caches, process-tree-safe timeouts, and atomic checkpoints.

Design (Route 2 — pure standard Git, zero vendor lock-in):
1. Checks each enabled repo via `git ls-remote --heads <url> refs/heads/<branch>` and computes full Layer-1
   `scan_fingerprint = hash(commit, branch, exclude_patterns, buildlists_hash, scanner_version)`.
2. Persistent shallow cache (`<store>/repos/<repo_id>`, default):
   - First cold start clones into `.staging_<id>_<pid>` with `--depth 1 --single-branch --no-tags --no-recurse-submodules`,
     then atomically promotes to `repos/<id>`.
   - Subsequent updates run `git fetch --depth 1 --prune --no-tags` + `reset --hard FETCH_HEAD` + `clean -ffdx`
     + lightweight `git prune --expire=now` (never `git gc --prune=now` on large repos).
   - If only local rules (`EXCLUDE_PATH_PATTERNS` / `BuildList` / scanner version) changed while `HEAD == remote_sha`,
     rescans directly from the local shallow cache with ZERO network transfer.
   - Corrupted cache dirs self-heal by wiping via Windows long-path-safe `safe_rmtree` and re-cloning once.
3. Ephemeral mode (`repo_cache_mode == "ephemeral"`): clones into staging dir, scans, and deletes immediately.
4. Enforces an atomic consistency gate: individual repo snapshots checkpoint immediately, while build/sync
   downstream callbacks run ONLY when zero repos failed and all enabled repos have snapshots present.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from autowork_core.ui_inventory.scanner import SCANNER_SCHEMA_VERSION
from autowork_core.ui_inventory.scanner.repo_scanner import scan_repository
from autowork_core.ui_inventory.store import (
    RepoConfigEntry,
    ReposRegistryConfig,
    SnapshotStore,
    safe_replace_path,
    safe_rmtree,
)


DEFAULT_LS_REMOTE_TIMEOUT_SEC = 60
DEFAULT_CLONE_TIMEOUT_SEC = 3600
DEFAULT_FETCH_TIMEOUT_SEC = 600
DEFAULT_LOCAL_GIT_TIMEOUT_SEC = 120
DEFAULT_HEARTBEAT_SEC = 30.0
LOW_DISK_WARNING_GB = 5.0

_NON_RETRYABLE_GIT_HINTS = (
    "authentication failed",
    "could not read username",
    "access denied",
    "401",
    "403",
    "404",
    "repository not found",
    "not found on remote",
    "did not match any",
)


@dataclass
class RepoPollOutcome:
    repo_id: str
    status: str  # unchanged | updated | removed | error | skipped
    old_commit: str = ""
    new_commit: str = ""
    snapshot_sha256: str = ""
    sync_mode: str = ""  # local | cloned | fetched | reused_local | recloned_after_recovery | ephemeral_clone
    elapsed_seconds: float = 0.0
    error: str = ""


@dataclass
class PollSyncReport:
    checked_count: int = 0
    repo_cache_mode: str = "persistent"
    changed_repos: List[str] = field(default_factory=list)
    unchanged_repos: List[str] = field(default_factory=list)
    removed_repos: List[str] = field(default_factory=list)
    failed_repos: List[str] = field(default_factory=list)
    missing_active_repos: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    outcomes: List[RepoPollOutcome] = field(default_factory=list)


def _emit_progress(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[ui-inventory poll] {message}", file=sys.stderr, flush=True)


def _force_rmtree(path: Path) -> None:
    """Backward-compatible wrapper around Windows long-path-safe `safe_rmtree`."""
    safe_rmtree(path)


def _redact_git_secrets(text: str, secrets: Sequence[str]) -> str:
    redacted = text
    for sec in secrets:
        if sec:
            redacted = redacted.replace(sec, "***")
    return redacted


def _git_auth_args_and_env(url: str) -> Tuple[List[str], Dict[str, str], List[str]]:
    """Return `(git_prefix_args, env, secrets_to_redact)` supporting ADS `$(System.AccessToken)` & PAT seamlessly."""
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if not url.lower().startswith(("http://", "https://")):
        return [], env, []

    token = (os.environ.get("UI_INVENTORY_GIT_TOKEN") or "").strip()
    if not token:
        return [], env, []

    auth_type = (os.environ.get("UI_INVENTORY_GIT_AUTH_TYPE") or "").strip().lower()
    secrets: List[str] = [token]
    if auth_type == "basic" or (not auth_type and ":" in token):
        b64 = base64.b64encode(token.encode("utf-8")).decode("ascii")
        header_val = f"AUTHORIZATION: Basic {b64}"
        secrets.append(b64)
    else:
        header_val = f"AUTHORIZATION: bearer {token}"

    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    git_args = ["-c", "credential.helper=", "-c", f"http.extraheader={header_val}"]
    return git_args, env, secrets


def _git_base_config_args() -> List[str]:
    """Disable fsmonitor daemon & background auto-gc/maintenance so no stray Git child holds Windows dir locks."""
    return [
        "-c", "core.longpaths=true",
        "-c", "safe.directory=*",
        "-c", "core.fsmonitor=false",
        "-c", "maintenance.auto=false",
        "-c", "gc.auto=0",
    ]


def _git_transport_config_args(url: str, *, force_http11: bool = False) -> List[str]:
    args = _git_base_config_args()
    if url.lower().startswith(("http://", "https://")):
        args.extend(["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=180"])
        if force_http11:
            args.extend(["-c", "http.version=HTTP/1.1"])
    return args


def _normalize_clone_url(url: str) -> str:
    local_candidate = Path(url)
    if "://" not in url and not url.startswith("git@") and local_candidate.exists():
        return local_candidate.resolve().as_uri()
    return url


def _kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the entire Git subprocess tree (including `git-remote-https.exe` on Windows)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _pack_bytes_in_git_dir(repo_dir: Optional[Path]) -> int:
    """Fast O(1) estimate of downloaded git pack bytes (checks only `.git/objects/pack`, never walks worktree)."""
    if repo_dir is None:
        return 0
    pack_dir = repo_dir / ".git" / "objects" / "pack"
    if not pack_dir.is_dir():
        return 0
    total = 0
    try:
        for entry in pack_dir.iterdir():
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _run_git_command(
    cmd: Sequence[str],
    *,
    env: Dict[str, str],
    timeout_sec: int,
    secrets: Sequence[str] = (),
    show_progress: bool = False,
    progress_label: str = "",
    monitor_dir: Optional[Path] = None,
    heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
) -> Tuple[int, str, str]:
    """Run a Git command with continuous pipe draining, periodic heartbeat, and process-tree kill on timeout."""
    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(list(cmd), **popen_kwargs)

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    max_stdout = 512 * 1024
    max_stderr = 64 * 1024

    def _drain(pipe: Any, target: bytearray, cap: int) -> None:
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                target.extend(chunk)
                if len(target) > cap:
                    del target[: len(target) - cap]
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_buf, max_stdout), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf, max_stderr), daemon=True)
    t_out.start()
    t_err.start()

    start = time.monotonic()
    last_hb = start
    deadline = start + max(1, timeout_sec)
    timed_out = False

    while proc.poll() is None:
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            _kill_process_tree(proc)
            break
        if show_progress and progress_label and heartbeat_sec > 0 and (now - last_hb) >= heartbeat_sec:
            last_hb = now
            pack_b = _pack_bytes_in_git_dir(monitor_dir)
            size_note = f", pack={pack_b / (1024 * 1024):.1f} MB" if pack_b > 0 else ""
            _emit_progress(True, f"{progress_label} still running ({now - start:.0f}s{size_note})...")
        time.sleep(0.15)

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    t_out.join(timeout=3.0)
    t_err.join(timeout=3.0)

    out_text = _redact_git_secrets(stdout_buf.decode("utf-8", errors="replace"), secrets)
    err_text = _redact_git_secrets(stderr_buf.decode("utf-8", errors="replace"), secrets)
    if timed_out:
        tail = err_text.strip()[-300:]
        raise TimeoutError(
            f"git command timed out after {timeout_sec}s"
            + (f" ({tail})" if tail else "")
        )
    return int(proc.returncode or 0), out_text, err_text


def _is_retryable_git_failure(err_text: str) -> bool:
    low = (err_text or "").lower()
    if any(hint in low for hint in _NON_RETRYABLE_GIT_HINTS):
        return False
    return True


def _detect_local_working_tree_token(local_dir: Path) -> str:
    """Return `<HEAD_SHA>` when clean, or `<HEAD_SHA>+dirty-<10hex>` when uncommitted edits exist."""
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    base_cfg = _git_base_config_args()
    rc, out, _ = _run_git_command(
        ["git", *base_cfg, "-C", str(local_dir), "rev-parse", "HEAD"],
        env=env,
        timeout_sec=15,
    )
    current_head = out.strip() if rc == 0 else ""
    if not current_head:
        return ""

    rc_st, out_st, _ = _run_git_command(
        ["git", *base_cfg, "-C", str(local_dir), "status", "--porcelain"],
        env=env,
        timeout_sec=30,
    )
    if rc_st != 0:
        return current_head
    dirty_lines = sorted(ln for ln in out_st.splitlines() if ln.strip())
    if not dirty_lines:
        return current_head

    h = hashlib.sha256()
    for ln in dirty_lines:
        h.update(ln.encode("utf-8", errors="replace"))
        h.update(b"\n")
        rel_part = ln[3:].strip()
        if " -> " in rel_part:
            rel_part = rel_part.split(" -> ", 1)[1].strip()
        rel_part = rel_part.strip('"')
        fpath = local_dir / rel_part
        if fpath.is_file():
            try:
                st = fpath.stat()
                if st.st_size <= 4 * 1024 * 1024:
                    h.update(fpath.read_bytes())
                else:
                    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode("ascii"))
            except OSError:
                pass
    return f"{current_head}+dirty-{h.hexdigest()[:10]}"


def ls_remote_branch_head(url: str, branch: str, timeout_sec: int = DEFAULT_LS_REMOTE_TIMEOUT_SEC) -> str:
    """Return remote commit SHA for `refs/heads/<branch>` via `git ls-remote` (with 1 HTTP/1.1 retry on transient error)."""
    target_url = _normalize_clone_url(url)
    auth_args, env, secrets = _git_auth_args_and_env(target_url)
    is_http = target_url.lower().startswith(("http://", "https://"))
    attempts = 2 if is_http else 1

    last_err = ""
    for attempt in range(attempts):
        cfg_args = _git_transport_config_args(target_url, force_http11=(attempt > 0))
        try:
            rc, out, err = _run_git_command(
                ["git", *cfg_args, *auth_args, "ls-remote", "--heads", target_url, f"refs/heads/{branch}"],
                env=env,
                timeout_sec=timeout_sec,
                secrets=secrets,
            )
        except TimeoutError as exc:
            last_err = str(exc)
            if attempt + 1 < attempts:
                time.sleep(2.0)
                continue
            raise RuntimeError(f"git ls-remote timed out: {last_err}") from None

        if rc == 0:
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
                    return parts[0]
            raise RuntimeError(f"Branch refs/heads/{branch} not found on remote {url}")

        last_err = (err or out).strip()
        if attempt + 1 < attempts and _is_retryable_git_failure(last_err):
            time.sleep(2.0)
            continue
        raise RuntimeError(f"git ls-remote failed ({rc}): {last_err}")

    raise RuntimeError(f"git ls-remote failed: {last_err}")


def shallow_clone_repo(
    url: str,
    branch: str,
    dest_dir: Path,
    timeout_sec: int = DEFAULT_CLONE_TIMEOUT_SEC,
    *,
    show_progress: bool = False,
    progress_label: str = "",
) -> str:
    """Shallow-clone (`--depth 1 --single-branch --no-recurse-submodules`) one branch into `dest_dir` and return HEAD SHA."""
    clone_url = _normalize_clone_url(url)
    auth_args, env, secrets = _git_auth_args_and_env(clone_url)
    is_http = clone_url.lower().startswith(("http://", "https://"))
    attempts = 2 if is_http else 1

    last_err = ""
    for attempt in range(attempts):
        if attempt > 0 and dest_dir.exists():
            safe_rmtree(dest_dir)
        cfg_args = _git_transport_config_args(clone_url, force_http11=(attempt > 0))
        cmd = [
            "git",
            *cfg_args,
            *auth_args,
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            "--no-tags",
            "--no-recurse-submodules",
            clone_url,
            str(dest_dir),
        ]
        try:
            rc, out, err = _run_git_command(
                cmd,
                env=env,
                timeout_sec=timeout_sec,
                secrets=secrets,
                show_progress=show_progress,
                progress_label=progress_label or f"cloning {dest_dir.name}",
                monitor_dir=dest_dir,
            )
        except TimeoutError as exc:
            last_err = str(exc)
            if attempt + 1 < attempts:
                _emit_progress(show_progress, f"{progress_label or dest_dir.name}: clone timed out, retrying with HTTP/1.1...")
                time.sleep(3.0)
                continue
            raise RuntimeError(f"git clone --depth 1 failed: {last_err}") from None

        if rc == 0:
            break
        last_err = (err or out).strip()
        if attempt + 1 < attempts and _is_retryable_git_failure(last_err):
            _emit_progress(show_progress, f"{progress_label or dest_dir.name}: transient clone error, retrying with HTTP/1.1...")
            time.sleep(3.0)
            continue
        raise RuntimeError(f"git clone --depth 1 failed ({rc}): {last_err}")

    rc_h, out_h, err_h = _run_git_command(
        ["git", *_git_base_config_args(), "-C", str(dest_dir), "rev-parse", "HEAD"],
        env=env,
        timeout_sec=15,
    )
    if rc_h != 0 or not out_h.strip():
        raise RuntimeError(f"Failed to read HEAD SHA after shallow clone: {err_h.strip()}")
    return out_h.strip()


def _remove_stale_git_locks(repo_dir: Path) -> None:
    """Remove leftover `.git/*.lock` files before running git under the exclusive `StoreLock`."""
    git_dir = repo_dir / ".git"
    if not git_dir.is_dir():
        return
    for candidate in (
        git_dir / "index.lock",
        git_dir / "HEAD.lock",
        git_dir / "FETCH_HEAD.lock",
        git_dir / "shallow.lock",
        git_dir / "packed-refs.lock",
        git_dir / "config.lock",
    ):
        if candidate.is_file():
            try:
                candidate.unlink()
            except OSError:
                pass
    refs_dir = git_dir / "refs"
    if refs_dir.is_dir():
        for lock_f in refs_dir.rglob("*.lock"):
            if lock_f.is_file():
                try:
                    lock_f.unlink()
                except OSError:
                    pass


def _sync_persistent_repo_cache(
    url: str,
    branch: str,
    repo_id: str,
    store: SnapshotStore,
    *,
    expected_sha: str = "",
    clone_timeout_sec: int = DEFAULT_CLONE_TIMEOUT_SEC,
    fetch_timeout_sec: int = DEFAULT_FETCH_TIMEOUT_SEC,
    show_progress: bool = False,
) -> Tuple[Path, str, str]:
    """Maintain `<store>/repos/<repo_id>` via incremental `git fetch --depth 1`, with automatic self-healing."""
    store.ensure_dirs()
    cache_dir = store.repo_cache_dir(repo_id)
    clone_url = _normalize_clone_url(url)
    auth_args, env, secrets = _git_auth_args_and_env(clone_url)

    def _stage_fresh_clone(mode_label: str) -> Tuple[Path, str, str]:
        staging = store.repos_dir / f".staging_{repo_id}_{os.getpid()}_{int(time.time() * 1000)}"
        try:
            head_sha = shallow_clone_repo(
                url,
                branch,
                staging,
                timeout_sec=clone_timeout_sec,
                show_progress=show_progress,
                progress_label=f"{repo_id}: initial shallow clone",
            )
            if cache_dir.exists():
                safe_rmtree(cache_dir)
            safe_replace_path(staging, cache_dir, max_wait_sec=30.0)
            return cache_dir, head_sha, mode_label
        except Exception:
            if staging.exists():
                safe_rmtree(staging)
            raise

    # Try reusing existing cached worktree first
    if (cache_dir / ".git").exists():
        base_cfg = _git_base_config_args()
        try:
            _remove_stale_git_locks(cache_dir)
            rc_chk, out_chk, _ = _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "rev-parse", "--is-inside-work-tree"],
                env=env,
                timeout_sec=15,
            )
            if rc_chk != 0 or out_chk.strip().lower() != "true":
                raise RuntimeError("Cached repository worktree check failed")

            # Align origin URL if changed
            rc_url, cur_url, _ = _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "remote", "get-url", "origin"],
                env=env,
                timeout_sec=15,
            )
            if rc_url != 0 or cur_url.strip() != clone_url:
                rc_set, _, err_set = _run_git_command(
                    ["git", *base_cfg, "-C", str(cache_dir), "remote", "set-url", "origin", clone_url],
                    env=env,
                    timeout_sec=15,
                )
                if rc_set != 0:
                    raise RuntimeError(f"git remote set-url failed: {err_set.strip()}")

            # Zero-network local rescan if HEAD already equals `expected_sha` (e.g. only BuildList/exclude changed)
            rc_hd, cur_head, _ = _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "rev-parse", "HEAD"],
                env=env,
                timeout_sec=15,
            )
            if rc_hd == 0 and expected_sha and cur_head.strip() == expected_sha:
                rc_rst, _, err_rst = _run_git_command(
                    ["git", *base_cfg, "-C", str(cache_dir), "reset", "--hard", "HEAD"],
                    env=env,
                    timeout_sec=DEFAULT_LOCAL_GIT_TIMEOUT_SEC,
                )
                if rc_rst != 0:
                    raise RuntimeError(f"git reset --hard HEAD failed: {err_rst.strip()}")
                rc_cln, _, err_cln = _run_git_command(
                    ["git", *base_cfg, "-C", str(cache_dir), "clean", "-ffdx"],
                    env=env,
                    timeout_sec=DEFAULT_LOCAL_GIT_TIMEOUT_SEC,
                )
                if rc_cln != 0:
                    raise RuntimeError(f"git clean -ffdx failed: {err_cln.strip()}")
                return cache_dir, expected_sha, "reused_local"

            # Incremental shallow fetch (`--depth 1 --prune --no-tags --recurse-submodules=no`) with 1 HTTP/1.1 retry
            is_http = clone_url.lower().startswith(("http://", "https://"))
            attempts = 2 if is_http else 1
            fetch_ok = False
            last_fetch_err = ""
            for attempt in range(attempts):
                cfg_args = _git_transport_config_args(clone_url, force_http11=(attempt > 0))
                cmd_fetch = [
                    "git",
                    *cfg_args,
                    *auth_args,
                    "-C",
                    str(cache_dir),
                    "fetch",
                    "--depth",
                    "1",
                    "--prune",
                    "--no-tags",
                    "--recurse-submodules=no",
                    "origin",
                    f"refs/heads/{branch}",
                ]
                try:
                    rc_f, out_f, err_f = _run_git_command(
                        cmd_fetch,
                        env=env,
                        timeout_sec=fetch_timeout_sec,
                        secrets=secrets,
                        show_progress=show_progress,
                        progress_label=f"{repo_id}: incremental fetch",
                        monitor_dir=cache_dir,
                    )
                except TimeoutError as exc:
                    last_fetch_err = str(exc)
                    if attempt + 1 < attempts:
                        time.sleep(3.0)
                        continue
                    raise RuntimeError(last_fetch_err) from None

                if rc_f == 0:
                    fetch_ok = True
                    break
                last_fetch_err = (err_f or out_f).strip()
                if attempt + 1 < attempts and _is_retryable_git_failure(last_fetch_err):
                    time.sleep(3.0)
                    continue
                break

            if not fetch_ok:
                raise RuntimeError(f"git fetch --depth 1 failed: {last_fetch_err}")

            rc_rst, _, err_rst = _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "reset", "--hard", "FETCH_HEAD"],
                env=env,
                timeout_sec=DEFAULT_LOCAL_GIT_TIMEOUT_SEC,
            )
            if rc_rst != 0:
                raise RuntimeError(f"git reset --hard FETCH_HEAD failed: {err_rst.strip()}")

            rc_cln, _, err_cln = _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "clean", "-ffdx"],
                env=env,
                timeout_sec=DEFAULT_LOCAL_GIT_TIMEOUT_SEC,
            )
            if rc_cln != 0:
                raise RuntimeError(f"git clean -ffdx failed: {err_cln.strip()}")
            # Lightweight orphan loose-object cleanup only (NEVER `git gc --prune=now` on multi-GB binary repos!)
            _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "prune", "--expire=now"],
                env=env,
                timeout_sec=60,
            )

            rc_new, new_head, err_new = _run_git_command(
                ["git", *base_cfg, "-C", str(cache_dir), "rev-parse", "HEAD"],
                env=env,
                timeout_sec=15,
            )
            if rc_new != 0 or not new_head.strip():
                raise RuntimeError(f"Failed to read HEAD after fetch: {err_new.strip()}")
            return cache_dir, new_head.strip(), "fetched"

        except Exception as exc:
            # Auth errors won't be fixed by wiping & re-cloning; fail fast
            if not _is_retryable_git_failure(str(exc)):
                raise
            _emit_progress(
                show_progress,
                f"{repo_id}: cached repo recovery triggered ({str(exc)[:140]}); wiping & re-cloning...",
            )
            safe_rmtree(cache_dir)
            return _stage_fresh_clone("recloned_after_recovery")

    return _stage_fresh_clone("cloned")


def _repo_priority(entry: RepoConfigEntry, store: SnapshotStore) -> int:
    role = entry.role.strip().lower()
    if role == "buildlist" or entry.id.lower() == "buildlists" or (store.buildlists_dir / entry.id).is_dir():
        return 0
    if role == "third_party":
        return 2
    return 1


def poll_and_sync_repos(
    registry: ReposRegistryConfig,
    store: SnapshotStore,
    *,
    force_rescan: bool = False,
    only_repos: Optional[List[str]] = None,
    on_after_updates: Optional[Callable[[List[str]], None]] = None,
    repo_cache_mode: Optional[str] = None,
    clone_timeout_sec: int = DEFAULT_CLONE_TIMEOUT_SEC,
    fetch_timeout_sec: int = DEFAULT_FETCH_TIMEOUT_SEC,
    ls_remote_timeout_sec: int = DEFAULT_LS_REMOTE_TIMEOUT_SEC,
    show_progress: bool = False,
) -> PollSyncReport:
    """Layer-1 poller: check all enabled repos, incrementally sync & checkpoint changed repos, enforce consistency gate."""
    store.ensure_dirs()
    cleaned_staging = store.cleanup_stale_staging_repos()
    if cleaned_staging:
        _emit_progress(show_progress, f"Cleaned {cleaned_staging} leftover staging clone dir(s)")

    effective_cache_mode = (repo_cache_mode or registry.repo_cache_mode or "persistent").strip().lower()
    if effective_cache_mode not in ("persistent", "ephemeral"):
        raise ValueError(f"Unsupported repo_cache_mode: {effective_cache_mode!r}")

    report = PollSyncReport(repo_cache_mode=effective_cache_mode)

    # Disk space preflight check
    try:
        free_gb = shutil.disk_usage(store.store_dir).free / (1024 ** 3)
        _emit_progress(show_progress, f"Store dir: {store.store_dir} (cache_mode={effective_cache_mode}, free={free_gb:.1f} GB)")
        if free_gb < LOW_DISK_WARNING_GB:
            warn_msg = f"Low disk space on store drive ({free_gb:.2f} GB free < {LOW_DISK_WARNING_GB:.0f} GB threshold): {store.store_dir}"
            report.warnings.append(warn_msg)
            _emit_progress(True, f"WARNING: {warn_msg}")
    except OSError:
        pass

    manifest = store.load_manifest()
    recorded_repos: Dict[str, Any] = manifest.get("repos") or {}

    explicit_build_lists = [Path(p) for p in registry.build_lists if Path(p).is_file()]
    only_filter = set(only_repos) if only_repos else None

    def _effective_build_lists() -> List[Path]:
        seen: set[str] = set()
        merged: List[Path] = []
        for p in [*explicit_build_lists, *store.list_cached_build_lists()]:
            if p.is_file():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    merged.append(p)
        return merged

    # Order: BuildList repos (0) -> Core UI repos (1) -> third_party repos (2, last so core checkpoints are always saved first!)
    ordered_repos = sorted(
        registry.repos,
        key=lambda e: _repo_priority(e, store),
    )
    active_entries = [e for e in ordered_repos if e.enabled and (only_filter is None or e.id in only_filter)]
    total_to_check = len(active_entries)

    check_idx = 0
    for entry in ordered_repos:
        if not entry.enabled:
            report.outcomes.append(RepoPollOutcome(repo_id=entry.id, status="skipped"))
            continue
        if only_filter is not None and entry.id not in only_filter:
            continue

        check_idx += 1
        report.checked_count += 1
        repo_t0 = time.monotonic()
        prev_meta = recorded_repos.get(entry.id) or {}
        old_commit = str(prev_meta.get("commit", ""))
        prev_scan_fp = str(prev_meta.get("scan_fingerprint", ""))
        has_snapshot_file = store.snapshot_path(entry.id).is_file()
        is_bl_repo = _repo_priority(entry, store) == 0

        try:
            # Mode A: Direct local working tree configured (`local_path` without remote URL)
            if entry.local_path and not entry.url:
                local_dir = Path(entry.local_path)
                if not local_dir.is_dir():
                    raise RuntimeError(f"Configured local_path does not exist: {local_dir}")
                store.sync_repo_build_lists(entry.id, local_dir)
                current_token = _detect_local_working_tree_token(local_dir)
                bl_fp = "" if is_bl_repo else store.compute_buildlists_fingerprint(explicit_build_lists)
                expected_fp = store.compute_repo_scan_fingerprint(
                    commit=current_token,
                    branch=entry.branch,
                    exclude_path_patterns=registry.exclude_path_patterns,
                    buildlists_fingerprint=bl_fp,
                    scanner_version=SCANNER_SCHEMA_VERSION,
                )
                if (
                    not force_rescan
                    and current_token
                    and current_token == old_commit
                    and prev_scan_fp == expected_fp
                    and has_snapshot_file
                ):
                    dt = round(time.monotonic() - repo_t0, 2)
                    _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: unchanged ({current_token[:10]}, {dt}s)")
                    report.unchanged_repos.append(entry.id)
                    report.outcomes.append(
                        RepoPollOutcome(
                            repo_id=entry.id,
                            status="unchanged",
                            old_commit=old_commit,
                            new_commit=current_token,
                            snapshot_sha256=str(prev_meta.get("snapshot_sha256", "")),
                            sync_mode="local",
                            elapsed_seconds=dt,
                        )
                    )
                    continue

                _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: scanning local worktree ({current_token[:10] or 'uncommitted'})...")
                snap = scan_repository(
                    repo_root=local_dir,
                    repo_id=entry.id,
                    branch=entry.branch,
                    commit_override=current_token,
                    external_build_lists=_effective_build_lists(),
                    exclude_path_patterns=registry.exclude_path_patterns,
                )
                saved = store.save_repo_snapshot(snap, scan_fingerprint=expected_fp)
                dt = round(time.monotonic() - repo_t0, 2)
                _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: snapshot saved ({dt}s)")
                report.changed_repos.append(entry.id)
                report.outcomes.append(
                    RepoPollOutcome(
                        repo_id=entry.id,
                        status="updated",
                        old_commit=old_commit,
                        new_commit=snap["commit"],
                        snapshot_sha256=saved["snapshot_sha256"],
                        sync_mode="local",
                        elapsed_seconds=dt,
                    )
                )
                continue

            # Mode B: Remote Git URL (`git ls-remote` -> persistent shallow fetch or ephemeral clone)
            if not entry.url:
                raise RuntimeError(f"Repo '{entry.id}' has neither `url` nor `local_path` configured")

            remote_sha = ls_remote_branch_head(entry.url, entry.branch, timeout_sec=ls_remote_timeout_sec)
            bl_fp = "" if is_bl_repo else store.compute_buildlists_fingerprint(explicit_build_lists)
            expected_fp = store.compute_repo_scan_fingerprint(
                commit=remote_sha,
                branch=entry.branch,
                exclude_path_patterns=registry.exclude_path_patterns,
                buildlists_fingerprint=bl_fp,
                scanner_version=SCANNER_SCHEMA_VERSION,
            )

            if (
                not force_rescan
                and remote_sha == old_commit
                and prev_scan_fp == expected_fp
                and has_snapshot_file
            ):
                dt = round(time.monotonic() - repo_t0, 2)
                _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: unchanged ({remote_sha[:10]}, {dt}s)")
                report.unchanged_repos.append(entry.id)
                report.outcomes.append(
                    RepoPollOutcome(
                        repo_id=entry.id,
                        status="unchanged",
                        old_commit=old_commit,
                        new_commit=remote_sha,
                        snapshot_sha256=str(prev_meta.get("snapshot_sha256", "")),
                        sync_mode=effective_cache_mode,
                        elapsed_seconds=dt,
                    )
                )
                continue

            reason = "force" if force_rescan else ("new/changed commit" if remote_sha != old_commit else "config/buildlist/schema changed")
            _emit_progress(
                show_progress,
                f"[{check_idx}/{total_to_check}] {entry.id}: updating ({reason}: {old_commit[:10] or 'none'} -> {remote_sha[:10]})...",
            )

            if effective_cache_mode == "persistent":
                repo_dir, synced_sha, sync_action = _sync_persistent_repo_cache(
                    entry.url,
                    entry.branch,
                    entry.id,
                    store,
                    expected_sha=remote_sha,
                    clone_timeout_sec=clone_timeout_sec,
                    fetch_timeout_sec=fetch_timeout_sec,
                    show_progress=show_progress,
                )
                store.sync_repo_build_lists(entry.id, repo_dir)
                final_bl_fp = "" if is_bl_repo else store.compute_buildlists_fingerprint(explicit_build_lists)
                final_fp = store.compute_repo_scan_fingerprint(
                    commit=synced_sha,
                    branch=entry.branch,
                    exclude_path_patterns=registry.exclude_path_patterns,
                    buildlists_fingerprint=final_bl_fp,
                    scanner_version=SCANNER_SCHEMA_VERSION,
                )
                _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: {sync_action} ({synced_sha[:10]}), scanning...")
                snap = scan_repository(
                    repo_root=repo_dir,
                    repo_id=entry.id,
                    branch=entry.branch,
                    commit_override=synced_sha,
                    external_build_lists=_effective_build_lists(),
                    exclude_path_patterns=registry.exclude_path_patterns,
                )
                saved = store.save_repo_snapshot(snap, scan_fingerprint=final_fp)
                dt = round(time.monotonic() - repo_t0, 2)
                _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: done ({sync_action}, {dt}s)")
                report.changed_repos.append(entry.id)
                report.outcomes.append(
                    RepoPollOutcome(
                        repo_id=entry.id,
                        status="updated",
                        old_commit=old_commit,
                        new_commit=synced_sha,
                        snapshot_sha256=saved["snapshot_sha256"],
                        sync_mode=sync_action,
                        elapsed_seconds=dt,
                    )
                )
            else:
                staging_dir = store.repos_dir / f".staging_{entry.id}_{os.getpid()}_{int(time.time() * 1000)}"
                try:
                    cloned_sha = shallow_clone_repo(
                        entry.url,
                        entry.branch,
                        staging_dir,
                        timeout_sec=clone_timeout_sec,
                        show_progress=show_progress,
                        progress_label=f"{entry.id}: ephemeral shallow clone",
                    )
                    store.sync_repo_build_lists(entry.id, staging_dir)
                    final_bl_fp = "" if is_bl_repo else store.compute_buildlists_fingerprint(explicit_build_lists)
                    final_fp = store.compute_repo_scan_fingerprint(
                        commit=cloned_sha,
                        branch=entry.branch,
                        exclude_path_patterns=registry.exclude_path_patterns,
                        buildlists_fingerprint=final_bl_fp,
                        scanner_version=SCANNER_SCHEMA_VERSION,
                    )
                    _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: cloned ({cloned_sha[:10]}), scanning...")
                    snap = scan_repository(
                        repo_root=staging_dir,
                        repo_id=entry.id,
                        branch=entry.branch,
                        commit_override=cloned_sha,
                        external_build_lists=_effective_build_lists(),
                        exclude_path_patterns=registry.exclude_path_patterns,
                    )
                    saved = store.save_repo_snapshot(snap, scan_fingerprint=final_fp)
                    dt = round(time.monotonic() - repo_t0, 2)
                    _emit_progress(show_progress, f"[{check_idx}/{total_to_check}] {entry.id}: done (ephemeral_clone, {dt}s)")
                    report.changed_repos.append(entry.id)
                    report.outcomes.append(
                        RepoPollOutcome(
                            repo_id=entry.id,
                            status="updated",
                            old_commit=old_commit,
                            new_commit=cloned_sha,
                            snapshot_sha256=saved["snapshot_sha256"],
                            sync_mode="ephemeral_clone",
                            elapsed_seconds=dt,
                        )
                    )
                finally:
                    safe_rmtree(staging_dir)

        except Exception as exc:
            dt = round(time.monotonic() - repo_t0, 2)
            _emit_progress(True, f"[{check_idx}/{total_to_check}] {entry.id}: ERROR ({dt}s): {exc}")
            report.failed_repos.append(entry.id)
            report.outcomes.append(
                RepoPollOutcome(
                    repo_id=entry.id,
                    status="error",
                    old_commit=old_commit,
                    elapsed_seconds=dt,
                    error=str(exc),
                )
            )

    # During full poll (no `--repo` filter), prune stale snapshots & repo caches for removed/disabled repos
    active_ids = {entry.id for entry in registry.repos if entry.enabled}
    if only_filter is None:
        cached_all = set(store.list_available_repo_ids()) | set(store.list_cached_repo_dirs())
        for cached_id in sorted(cached_all):
            if cached_id not in active_ids:
                if store.remove_repo_snapshot(cached_id):
                    report.removed_repos.append(cached_id)
                    report.outcomes.append(RepoPollOutcome(repo_id=cached_id, status="removed"))

    # Atomic consistency gate check: verify ALL enabled repos have valid snapshots on disk
    report.missing_active_repos = sorted(
        rid for rid in active_ids if not store.snapshot_path(rid).is_file()
    )

    if (
        (report.changed_repos or report.removed_repos)
        and not report.failed_repos
        and not report.missing_active_repos
        and on_after_updates is not None
    ):
        on_after_updates(report.changed_repos + report.removed_repos)

    return report
