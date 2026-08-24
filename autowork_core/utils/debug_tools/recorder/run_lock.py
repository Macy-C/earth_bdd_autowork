from __future__ import annotations

import ctypes
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import psutil


_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_INFINITE = 0xFFFFFFFF


class _RunLockMutex:
    def __init__(self, session_dir):
        identity = hashlib.sha256(
            str(Path(session_dir).resolve()).casefold().encode("utf-8")
        ).hexdigest()
        self.name = f"Local\\BddAutoworkRecorder-{identity}"
        self.handle = None

    def __enter__(self):
        create_mutex = ctypes.windll.kernel32.CreateMutexW
        create_mutex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        create_mutex.restype = ctypes.c_void_p
        wait = ctypes.windll.kernel32.WaitForSingleObject
        wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait.restype = ctypes.c_uint32
        self.handle = create_mutex(None, False, self.name)
        if not self.handle:
            raise OSError("无法创建录制任务互斥锁")
        result = int(wait(self.handle, _INFINITE))
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
            raise OSError(f"无法等待录制任务互斥锁: {result}")
        return self

    def __exit__(self, _error_type, _error, _traceback):
        if not self.handle:
            return
        ctypes.windll.kernel32.ReleaseMutex(self.handle)
        ctypes.windll.kernel32.CloseHandle(self.handle)
        self.handle = None


class RunWriteLock:
    def __init__(self, session_dir):
        self.session_dir = Path(session_dir).resolve()
        self.path = self.session_dir / ".recorder.lock"
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self):
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with _RunLockMutex(self.session_dir):
            payload = {
                "pid": os.getpid(),
                "token": self.token,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
            if self.path.exists():
                owner = _read_lock(self.path)
                pid = int(owner.get("pid") or 0)
                if pid and psutil.pid_exists(pid):
                    raise RuntimeError(
                        f"录制任务正被其他进程写入: pid={pid}"
                    )
                self.path.unlink(missing_ok=True)
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            self.acquired = True
            return self

    def release(self):
        if not self.acquired:
            return
        owner = _read_lock(self.path)
        if owner.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False


def _read_lock(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def active_run_lock(session_dir):
    path = Path(session_dir).resolve() / ".recorder.lock"
    if not path.exists():
        return None
    owner = _read_lock(path)
    pid = int(owner.get("pid") or 0)
    return owner if pid and psutil.pid_exists(pid) else None