from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import uuid
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

import psutil


LEASE_VERSION = "2.0"
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_INFINITE = 0xFFFFFFFF
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_WINDOWS_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class GenerationFileConflict(RuntimeError):
    pass


class _ProjectMutex(AbstractContextManager):
    def __init__(self, project_root):
        identity = hashlib.sha256(
            str(_canonical_project_root(project_root)).casefold().encode("utf-8")
        ).hexdigest()
        self.name = f"Local\\BddAutoworkGeneration-{identity}"
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
            raise OSError("无法创建项目生成互斥锁")
        result = int(wait(self.handle, _INFINITE))
        if result not in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
            raise OSError(f"无法等待项目生成互斥锁: {result}")
        return self

    def __exit__(self, _error_type, _error, _traceback):
        if self.handle:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def acquire_generation_file_lease(
        project_root,
        *,
        transaction_id,
    request_id=None,
        report_path,
        target_files,
):
    project_root = _canonical_project_root(project_root)
    files = _normalize_files(target_files)
    transaction_id = str(transaction_id)
    lease_token = uuid.uuid4().hex
    owner = _owner_identity()

    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        conflicts = []
        for file_value in files:
            lock_path = _lock_path(lock_dir, file_value)
            existing = _read_json(lock_path)
            if not existing:
                continue
            if _lease_is_active(existing, receipt_dir):
                conflicts.append(existing)
            else:
                lock_path.unlink(missing_ok=True)
        if conflicts:
            details = ", ".join(
                f"{item.get('target_file')} ({item.get('transaction_id')})"
                for item in conflicts
            )
            raise GenerationFileConflict(
                f"目标文件正由其他生成事务写入: {details}"
            )

        receipt_path = _receipt_path(receipt_dir, transaction_id)
        receipt = {
            "lease_version": LEASE_VERSION,
            "transaction_id": transaction_id,
            "request_id": str(request_id or ""),
            "lease_token": lease_token,
            "report_path": str(Path(report_path).resolve()),
            "files": files,
            "owner": owner,
            "committed": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        created_locks = []
        try:
            for file_value in files:
                payload = {
                    "lease_version": LEASE_VERSION,
                    "transaction_id": transaction_id,
                    "lease_token": lease_token,
                    "target_file": file_value,
                    "owner": owner,
                    "committed": False,
                }
                lock_path = _lock_path(lock_dir, file_value)
                _write_json(lock_path, payload)
                created_locks.append(lock_path)
            _write_json(receipt_path, receipt)
        except Exception:
            for lock_path in created_locks:
                current = _read_json(lock_path)
                if current.get("lease_token") == lease_token:
                    lock_path.unlink(missing_ok=True)
            current_receipt = _read_json(receipt_path)
            if current_receipt.get("lease_token") == lease_token:
                receipt_path.unlink(missing_ok=True)
            raise

    lease = {
        "lease_version": LEASE_VERSION,
        "transaction_id": transaction_id,
        "lease_token": lease_token,
        "files": files,
        "committed": False,
    }
    lease["fingerprint"] = _fingerprint(lease)
    return lease


def find_committed_generation_file_lease_report(project_root, request_id):
    project_root = _canonical_project_root(project_root)
    request_id = str(request_id)
    candidates = []
    with _ProjectMutex(project_root):
        _lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        for receipt_path in receipt_dir.glob("*.json"):
            receipt = _read_json(receipt_path)
            if (
                    receipt.get("committed") is not True
                    or receipt.get("request_id") != request_id
            ):
                continue
            report_path = Path(str(receipt.get("report_path") or ""))
            report = _read_json(report_path)
            if any((
                not report.get("status"),
                report.get("request_id") != request_id,
                report.get("transaction_id")
                != receipt.get("transaction_id"),
            )):
                raise GenerationFileConflict(
                    "已提交的生成文件 lease 缺少有效 running 报告: "
                    f"{receipt.get('transaction_id')}"
                )
            candidates.append((report_path.resolve(), report))
    if len(candidates) > 1:
        raise GenerationFileConflict(
            f"Request 存在多个已提交生成文件 lease: {request_id}"
        )
    return candidates[0] if candidates else None


def commit_generation_file_lease(project_root, lease):
    project_root = _canonical_project_root(project_root)
    _validate_public_lease_shape(lease)
    transaction_id = str(lease["transaction_id"])
    lease_token = str(lease["lease_token"])
    files = _normalize_files(lease.get("files") or ())

    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        receipt_path = _receipt_path(receipt_dir, transaction_id)
        receipt = _read_json(receipt_path)
        _require_matching_receipt(receipt, transaction_id, lease_token, files)
        receipt["committed"] = True
        receipt["committed_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(receipt_path, receipt)
        for file_value in files:
            lock_path = _lock_path(lock_dir, file_value)
            payload = _read_json(lock_path)
            _require_matching_lock(payload, transaction_id, lease_token, file_value)
            payload["committed"] = True
            _write_json(lock_path, payload)

    committed = {
        key: value
        for key, value in lease.items()
        if key != "fingerprint"
    }
    committed["committed"] = True
    committed["fingerprint"] = _fingerprint(committed)
    return committed


def finalize_generation_file_lease(project_root, lease):
    project_root = _canonical_project_root(project_root)
    _validate_public_lease_shape(lease)
    transaction_id = str(lease["transaction_id"])
    lease_token = str(lease["lease_token"])
    files = _normalize_files(lease.get("files") or ())

    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        receipt_path = _receipt_path(receipt_dir, transaction_id)
        receipt = _read_json(receipt_path)
        _require_matching_receipt(receipt, transaction_id, lease_token, files)
        if receipt.get("committed") is not True:
            raise ValueError("Generation transaction 文件 lease 尚未提交")
        receipt["finalized"] = True
        receipt["finalized_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(receipt_path, receipt)
        for file_value in files:
            lock_path = _lock_path(lock_dir, file_value)
            payload = _read_json(lock_path)
            _require_matching_lock(payload, transaction_id, lease_token, file_value)
            payload["finalized"] = True
            _write_json(lock_path, payload)

    finalized = {
        key: value
        for key, value in lease.items()
        if key != "fingerprint"
    }
    finalized["finalized"] = True
    finalized["fingerprint"] = _fingerprint(finalized)
    return finalized


def validate_generation_file_lease(project_root, lease):
    if not lease:
        return []
    project_root = _canonical_project_root(project_root)
    try:
        _validate_public_lease_shape(lease)
        files = _normalize_files(lease.get("files") or ())
    except (TypeError, ValueError) as error:
        return [str(error)]
    expected_fingerprint = _fingerprint({
        key: value
        for key, value in lease.items()
        if key != "fingerprint"
    })
    if lease.get("fingerprint") != expected_fingerprint:
        return ["Generation transaction 文件 lease 指纹无效"]

    transaction_id = str(lease["transaction_id"])
    lease_token = str(lease["lease_token"])
    errors = []
    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        receipt = _read_json(_receipt_path(receipt_dir, transaction_id))
        try:
            _require_matching_receipt(
                receipt,
                transaction_id,
                lease_token,
                files,
            )
        except ValueError as error:
            errors.append(str(error))
        if receipt.get("finalized") is True:
            errors.append("Generation transaction 文件 lease 已终结")
        if not receipt.get("committed"):
            errors.append("Generation transaction 文件 lease 尚未提交")
        for file_value in files:
            payload = _read_json(_lock_path(lock_dir, file_value))
            try:
                _require_matching_lock(
                    payload,
                    transaction_id,
                    lease_token,
                    file_value,
                )
            except ValueError as error:
                errors.append(str(error))
                continue
            if payload.get("finalized") is True:
                errors.append(f"生成文件 lease 已终结: {file_value}")
            if not payload.get("committed"):
                errors.append(f"生成文件 lease 尚未提交: {file_value}")
    return errors


@contextmanager
def generation_file_lease_write_guard(project_root, lease):
    project_root = _canonical_project_root(project_root)
    _validate_public_lease_shape(lease)
    transaction_id = str(lease["transaction_id"])
    lease_token = str(lease["lease_token"])
    files = _normalize_files(lease.get("files") or ())
    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        receipt = _read_json(_receipt_path(receipt_dir, transaction_id))
        _require_matching_receipt(
            receipt,
            transaction_id,
            lease_token,
            files,
        )
        if receipt.get("committed") is not True or receipt.get(
                "finalized") is True:
            raise ValueError(
                "Generation transaction 文件 lease 不可用于系统写入"
            )
        for file_value in files:
            payload = _read_json(_lock_path(lock_dir, file_value))
            _require_matching_lock(
                payload,
                transaction_id,
                lease_token,
                file_value,
            )
            if payload.get("committed") is not True or payload.get(
                    "finalized") is True:
                raise ValueError(
                    f"生成文件 lease 不可用于系统写入: {file_value}"
                )
        yield


@contextmanager
def generation_file_lease_publish_guard(project_root, lease):
    project_root = _canonical_project_root(project_root)
    _validate_public_lease_shape(lease)
    transaction_id = str(lease["transaction_id"])
    lease_token = str(lease["lease_token"])
    files = _normalize_files(lease.get("files") or ())
    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        receipt_path = _receipt_path(receipt_dir, transaction_id)
        receipt = _read_json(receipt_path)
        _require_matching_receipt(
            receipt,
            transaction_id,
            lease_token,
            files,
        )
        if receipt.get("committed") is not True:
            raise ValueError("Generation transaction 文件 lease 尚未提交")
        receipt["finalized"] = True
        receipt["finalized_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(receipt_path, receipt)
        lock_paths = []
        for file_value in files:
            lock_path = _lock_path(lock_dir, file_value)
            payload = _read_json(lock_path)
            _require_matching_lock(
                payload,
                transaction_id,
                lease_token,
                file_value,
            )
            payload["finalized"] = True
            _write_json(lock_path, payload)
            lock_paths.append(lock_path)
        try:
            yield
        except Exception:
            raise
        else:
            for lock_path in lock_paths:
                payload = _read_json(lock_path)
                if payload.get("lease_token") == lease_token:
                    lock_path.unlink(missing_ok=True)
            current = _read_json(receipt_path)
            if current.get("lease_token") == lease_token:
                receipt_path.unlink(missing_ok=True)


def generation_path_has_reparse_point(project_root, relative):
    project_root = _canonical_project_root(project_root)
    normalized = _normalize_files([relative])[0]
    current = project_root
    for part in PurePosixPath(normalized).parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            return True
    return False


def release_generation_file_lease(project_root, lease):
    if not lease:
        return
    project_root = Path(project_root).resolve()
    try:
        _validate_public_lease_shape(lease)
    except (TypeError, ValueError):
        return
    _release_transaction_receipt(
        project_root,
        str(lease["transaction_id"]),
        expected_token=str(lease["lease_token"]),
    )


def generation_file_lease_release_is_complete(project_root, lease):
    project_root = _canonical_project_root(project_root)
    _validate_public_lease_shape(lease)
    transaction_id = str(lease["transaction_id"])
    files = _normalize_files(lease.get("files") or ())
    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        if _receipt_path(receipt_dir, transaction_id).exists():
            return False
        return not any(
            _lock_path(lock_dir, file_value).exists()
            for file_value in files
        )


def release_generation_file_lease_for_transaction(project_root, transaction_id):
    _release_transaction_receipt(
        _canonical_project_root(project_root),
        str(transaction_id),
        expected_token=None,
    )


def _release_transaction_receipt(
        project_root,
        transaction_id,
        *,
        expected_token,
):
    with _ProjectMutex(project_root):
        lock_dir, receipt_dir = _ensure_lock_directories(project_root)
        receipt_path = _receipt_path(receipt_dir, transaction_id)
        receipt = _read_json(receipt_path)
        receipt_token = str(receipt.get("lease_token") or "")
        if expected_token and receipt_token != expected_token:
            return
        token = expected_token or receipt_token
        for lock_path in lock_dir.glob("*.json"):
            payload = _read_json(lock_path)
            if (
                    payload.get("transaction_id") == transaction_id
                    and (not token or payload.get("lease_token") == token)
            ):
                lock_path.unlink(missing_ok=True)
        if (
                receipt.get("transaction_id") == transaction_id
                and (not token or receipt.get("lease_token") == token)
        ):
            receipt_path.unlink(missing_ok=True)


def _lease_is_active(payload, receipt_dir):
    transaction_id = str(payload.get("transaction_id") or "")
    lease_token = str(payload.get("lease_token") or "")
    receipt = _read_json(_receipt_path(receipt_dir, transaction_id))
    if payload.get("finalized") is True or receipt.get("finalized") is True:
        return False
    if payload.get("committed") is True:
        return True
    if (
            receipt.get("transaction_id") == transaction_id
            and receipt.get("lease_token") == lease_token
            and receipt.get("committed") is True
    ):
        return True
    return _owner_is_alive(payload.get("owner") or receipt.get("owner") or {})


def _validate_public_lease_shape(lease):
    if not isinstance(lease, dict):
        raise TypeError("Generation transaction 文件 lease 必须是对象")
    if lease.get("lease_version") != LEASE_VERSION:
        raise ValueError("Generation transaction 文件 lease 版本无效")
    if not lease.get("transaction_id") or not lease.get("lease_token"):
        raise ValueError("Generation transaction 文件 lease 身份不完整")


def _require_matching_receipt(
        receipt,
        transaction_id,
        lease_token,
        files,
):
    if any((
        receipt.get("lease_version") != LEASE_VERSION,
        receipt.get("transaction_id") != transaction_id,
        receipt.get("lease_token") != lease_token,
        _normalize_files(receipt.get("files") or ()) != files,
    )):
        raise ValueError("Generation transaction 文件 lease receipt 无效")


def _require_matching_lock(payload, transaction_id, lease_token, file_value):
    if any((
        payload.get("lease_version") != LEASE_VERSION,
        payload.get("transaction_id") != transaction_id,
        payload.get("lease_token") != lease_token,
        payload.get("target_file") != file_value,
    )):
        raise ValueError(f"生成文件 lease 已丢失或被替换: {file_value}")


def _normalize_files(values):
    normalized_by_key = {}
    for value in values or ():
        text = str(value).replace("\\", "/")
        path = PurePosixPath(text)
        if (
                not text
                or text.startswith("/")
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"生成 lease 路径必须是项目内相对路径: {value}")
        for part in path.parts:
            if (
                    ":" in part
                    or re.match(r"^[^~]{1,6}~\d", part, re.IGNORECASE)
                    or part.rstrip(" .") != part
                    or part.split(".", 1)[0].casefold()
                    in _INVALID_WINDOWS_NAMES
            ):
                raise ValueError(f"生成 lease 路径不符合Windows规则: {value}")
        normalized = path.as_posix()
        normalized_by_key.setdefault(normalized.casefold(), normalized)
    return sorted(normalized_by_key.values(), key=str.casefold)


def _ensure_lock_directories(project_root):
    artifacts_dir = Path(project_root) / "artifacts"
    lock_dir = artifacts_dir / "generation-locks"
    receipt_dir = lock_dir / "transactions"
    for directory in (artifacts_dir, lock_dir, receipt_dir):
        if directory.exists() and _is_reparse_point(directory):
            raise ValueError(f"生成 lease 目录不能是重解析点: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(directory):
            raise ValueError(f"生成 lease 目录不能是重解析点: {directory}")
    return lock_dir, receipt_dir


def _is_reparse_point(path):
    path = Path(path)
    if path.is_symlink():
        return True
    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    attributes = int(get_attributes(str(path)))
    return attributes != 0xFFFFFFFF and bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _canonical_project_root(value):
    path = Path(value).resolve()
    get_long_path = ctypes.windll.kernel32.GetLongPathNameW
    get_long_path.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    get_long_path.restype = ctypes.c_uint32
    required = int(get_long_path(str(path), None, 0))
    if required <= 0:
        return path
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(get_long_path(str(path), buffer, len(buffer)))
    return Path(buffer.value).resolve() if written else path


def _lock_path(lock_dir, file_value):
    digest = hashlib.sha256(file_value.casefold().encode("utf-8")).hexdigest()
    return Path(lock_dir) / f"{digest}.json"


def _receipt_path(receipt_dir, transaction_id):
    digest = hashlib.sha256(str(transaction_id).encode("utf-8")).hexdigest()
    return Path(receipt_dir) / f"{digest}.json"


def _owner_identity():
    process = psutil.Process(os.getpid())
    return {
        "pid": process.pid,
        "create_time": process.create_time(),
    }


def _owner_is_alive(owner):
    try:
        process = psutil.Process(int(owner.get("pid") or 0))
        return abs(
            process.create_time() - float(owner.get("create_time") or 0)
        ) < 0.001
    except (psutil.Error, TypeError, ValueError):
        return False


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
