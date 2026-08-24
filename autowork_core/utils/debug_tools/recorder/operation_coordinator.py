from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        self._event.set()

    def raise_if_cancelled(self):
        if self.cancelled:
            raise OperationCancelled("操作已取消")


class OperationCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    key: str
    revision: int
    context: Any
    status: str
    submitted_at: str
    started_at: str
    finished_at: str
    duration_ms: int
    value: Any = None
    error: Exception | None = None


@dataclass
class _Operation:
    operation_id: str
    key: str
    revision: int
    context: Any
    token: CancellationToken
    submitted_at: str
    future: Any = None


class OperationCoordinator:
    """Bounded in-process work coordinator with latest-revision semantics."""

    def __init__(self, max_workers=3, thread_name_prefix="recorder"):
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = threading.RLock()
        self._operations = {}
        self._latest = {}
        self._revisions = {}
        self._results = deque()
        self._discarded_operation_ids = set()
        self._closed = False

    def submit(
            self,
            key,
            function: Callable,
            *args,
            context=None,
            pass_token=False,
            **kwargs,
        ):
        key = str(key)
        with self._lock:
            if self._closed:
                raise RuntimeError("任务协调器已经关闭")
            revision = self._revisions.get(key, 0) + 1
            self._revisions[key] = revision
            self._cancel_older_locked(key, revision)
            operation = _Operation(
                operation_id=f"operation-{uuid.uuid4().hex[:16]}",
                key=key,
                revision=revision,
                context=context,
                token=CancellationToken(),
                submitted_at=datetime.now().isoformat(timespec="milliseconds"),
            )
            self._operations[operation.operation_id] = operation
            self._latest[key] = operation.operation_id
            operation.future = self._executor.submit(
                self._run,
                operation,
                function,
                args,
                kwargs,
                pass_token,
            )
            return operation.operation_id

    def cancel(self, operation_id):
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return False
            operation.token.cancel()
            return True

    def cancel_key(self, key):
        with self._lock:
            operation_id = self._latest.get(str(key))
        return self.cancel(operation_id) if operation_id else False

    def cancel_prefix(self, key_prefix):
        with self._lock:
            operation_ids = [
                operation.operation_id
                for operation in self._operations.values()
                if operation.key.startswith(str(key_prefix))
            ]
        for operation_id in operation_ids:
            self.cancel(operation_id)
        return len(operation_ids)

    def abandon_prefix(self, key_prefix, *, wait=False):
        key_prefix = str(key_prefix)
        with self._lock:
            operations = [
                operation
                for operation in self._operations.values()
                if operation.key.startswith(key_prefix)
            ]
            for operation in operations:
                operation.token.cancel()
                self._discarded_operation_ids.add(operation.operation_id)
            self._results = deque(
                result
                for result in self._results
                if not result.key.startswith(key_prefix)
            )
            for key in tuple(self._latest):
                if key.startswith(key_prefix):
                    self._latest.pop(key, None)
                    self._revisions.pop(key, None)
            futures = [
                operation.future
                for operation in operations
                if operation.future is not None
            ]
        if wait:
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
        return len(operations)

    def next_result(self, key=None, key_prefix=None):
        with self._lock:
            for index, result in enumerate(self._results):
                if key is not None and result.key != str(key):
                    continue
                if (
                        key_prefix is not None
                        and not result.key.startswith(str(key_prefix))
                ):
                    continue
                del self._results[index]
                return result
        return None

    def drain(self, key=None, key_prefix=None):
        results = []
        while True:
            result = self.next_result(key=key, key_prefix=key_prefix)
            if result is None:
                return results
            results.append(result)

    def has_results(self, key=None, key_prefix=None):
        with self._lock:
            return any(
                (key is None or result.key == str(key))
                and (
                    key_prefix is None
                    or result.key.startswith(str(key_prefix))
                )
                for result in self._results
            )

    def list_active(self, key=None, key_prefix=None):
        with self._lock:
            values = []
            for operation in self._operations.values():
                if key is not None and operation.key != str(key):
                    continue
                if (
                        key_prefix is not None
                        and not operation.key.startswith(str(key_prefix))
                ):
                    continue
                if operation.future is None or not operation.future.done():
                    values.append(operation.operation_id)
            return tuple(values)

    def shutdown(self, wait=False):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for operation in self._operations.values():
                operation.token.cancel()
                if operation.future is not None:
                    operation.future.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _run(self, operation, function, args, kwargs, pass_token):
        started_at = datetime.now().isoformat(timespec="milliseconds")
        started_monotonic = time.monotonic()
        try:
            operation.token.raise_if_cancelled()
            if pass_token:
                value = function(operation.token, *args, **kwargs)
            else:
                value = function(*args, **kwargs)
            status = "completed"
            error = None
        except OperationCancelled as caught:
            value = None
            status = "cancelled"
            error = caught
        except Exception as caught:
            value = None
            status = "failed"
            error = caught
        with self._lock:
            is_latest = self._latest.get(operation.key) == operation.operation_id
        if not is_latest or operation.token.cancelled:
            status = "superseded"
            value = None
        finished_at = datetime.now().isoformat(timespec="milliseconds")
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        with self._lock:
            if operation.operation_id in self._discarded_operation_ids:
                self._discarded_operation_ids.discard(operation.operation_id)
                self._operations.pop(operation.operation_id, None)
                return
            self._results.append(OperationResult(
                operation_id=operation.operation_id,
                key=operation.key,
                revision=operation.revision,
                context=operation.context,
                status=status,
                submitted_at=operation.submitted_at,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                value=value,
                error=error,
            ))
            self._operations.pop(operation.operation_id, None)

    def _cancel_older_locked(self, key, revision):
        for operation in self._operations.values():
            if operation.key != key or operation.revision >= revision:
                continue
            operation.token.cancel()
