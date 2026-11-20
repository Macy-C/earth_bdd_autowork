import json
from pathlib import Path
import sys
from threading import RLock

from loguru import logger
from loguru._file_sink import FileSink

from .context import ACTIVE_ACTION, ACTIVE_COLLECTOR
from .models import redacted


def sanitize_record(record):
    record["message"] = redacted(record["message"])
    collector = ACTIVE_COLLECTOR.get()
    if collector is not None and collector.active:
        action = ACTIVE_ACTION.get()
        node = action[1] if action is not None and action[0] is collector else collector.current_step or collector.current_scenario or collector.current_feature
        record["extra"]["execution_id"] = collector.execution_id
        record["extra"]["scope_id"] = node["id"] if node is not None else collector.execution_id
    return True


def standalone_log_record(record):
    if ACTIVE_COLLECTOR.get() is not None:
        return False
    return sanitize_record(record)


class RunLogCapture:
    def __init__(self, collector, configuration):
        self.collector = collector
        self.configuration = configuration
        self.sink_id = None
        self.closed = False
        self.phase = "execution"
        self._lock = RLock()
        self._files = {}
        self._failed = set()
        self._warned = set()
        self._previous_patcher = None
        self._patcher = None
        self._filename = None
        self._console = not any(
            getattr(handler, "_filter", None) is None and getattr(getattr(handler, "_sink", None), "_stream", None)
            in (sys.stderr, sys.stdout, sys.__stderr__, sys.__stdout__)
            for handler in logger._core.handlers.values()
        )
        self.summary = {
            phase: {"status": "not_requested", "bytes": 0, "dropped": 0, "retained_removed": 0}
            for phase in ("execution", "delivery")
        }
        collector.record["logging"] = {"execution": self.summary["execution"]}
        try:
            filename = str(configuration["log_file"])
            if not filename or filename in {".", ".."} or any(part in filename for part in ("/", "\\", ":", "{", "}")):
                raise ValueError("Run log filename must be a plain filename")
            if Path(filename).name != filename:
                raise ValueError("Run log filename escapes the evidence directory")
            self._filename = filename
            if collector.directory is not None:
                self._open_file("execution")
        except Exception as error:
            self._failure("execution", "initialize", error)

    def install(self):
        if self.sink_id is not None or self.closed:
            return self.sink_id
        def patch_record(record):
            if self._previous_patcher is not None:
                self._previous_patcher(record)
            if not self.closed and ACTIVE_COLLECTOR.get() is self.collector:
                try:
                    self._prepare_record(record)
                except Exception as error:
                    record["message"] = redacted(record["message"])
                    record["exception"] = None
                    self._failure(self.phase, "prepare", error)

        self._patcher = patch_record
        patch_record._autowork_capture = self
        with logger._core.lock:
            self._previous_patcher = logger._core.patcher
            logger._core.patcher = self._patcher
        try:
            self.sink_id = logger.add(
                self, level=self.configuration["log_level"],
                filter=lambda record: ACTIVE_COLLECTOR.get() is self.collector and not self.closed,
                catch=False, diagnose=False, backtrace=False, format="{message}",
            )
        except BaseException:
            self.stop()
            self.restore()
            raise
        try:
            directory = str(self.collector.directory) if self.collector.directory is not None else "in-memory"
            sys.stderr.write(f"Evidence run: {self.collector.execution_id} | directory: {directory}\n")
        except Exception as error:
            self._failure("execution", "console", error)
        return self.sink_id

    def _prepare_record(self, record):
        sanitize_record(record)
        collector = self.collector
        extra = record["extra"]
        action = ACTIVE_ACTION.get()
        node = None if self.phase != "execution" else (
            action[1] if action is not None and action[0] is collector
            else collector.current_step or collector.current_scenario or collector.current_feature
        )
        scope_id = node["id"] if node is not None else collector.execution_id
        context = [item.get("name", "") for item in (
            collector.current_feature, collector.current_scenario, collector.current_step,
        ) if item is not None] if self.phase == "execution" else [collector.delivery_name or "delivery"]
        if self.phase == "execution" and action is not None and action[0] is collector:
            context.append(action[1].get("operation", ""))
        text = record["message"]
        if record["exception"] is not None:
            error = record["exception"].value
            detail = collector.exception_record(error)
            text = redacted(text + "\n" + detail["traceback"])
            record["exception"] = None
        extra.update(
            execution_id=collector.execution_id, scope_id=scope_id,
            autowork_phase=self.phase, autowork_message=text,
            autowork_context=redacted(" / ".join(str(item) for item in context)),
        )
        if self.phase == "execution":
            console_context = [item.get("name", "") for item in (
                collector.current_feature, collector.current_scenario,
            ) if item is not None]
            if collector.current_step is not None:
                step_id = str(collector.current_step.get("id", "")).rsplit("/", 1)[-1]
                console_context.append(step_id.replace("step-", "Step ", 1) if step_id.startswith("step-") else "Step")
            if action is not None and action[0] is collector:
                console_context.append(action[1].get("operation", ""))
        else:
            console_context = ["delivery", collector.delivery_name or "reporting"]
        labels = []
        for value in console_context:
            label = redacted(str(value)).replace("\r", " ").replace("\n", " ")
            labels.append(label if len(label) <= 48 else label[:45] + "...")
        record["message"] = f"[{' / '.join(labels) or 'Run'}] {text}"

    def _open_file(self, phase):
        if self.collector.directory is None or self._filename is None or phase in self._failed:
            return None
        if phase not in self._files:
            path = self.collector.directory / "logs" / phase / self._filename
            retention = FileSink._make_retention_function(self.configuration["log_retention"])

            def retain(paths):
                owned = [str(candidate) for candidate in map(Path, paths)
                         if candidate.parent.resolve() == path.parent.resolve() and candidate.is_file()]
                before = set(owned)
                if retention is not None:
                    retention(owned)
                removed = sum(not Path(candidate).exists() for candidate in before)
                if removed:
                    self.summary[phase]["retained_removed"] += removed
                    self._missing(phase, "retention")

            self._files[phase] = FileSink(
                str(path), rotation=self.configuration["log_rotation"], retention=retain,
                encoding="utf-8", newline="", delay=True,
            )
            self.summary[phase].update(status="recording", path=path.relative_to(self.collector.directory).as_posix())
        return self._files[phase]

    def write(self, message):
        try:
            with self._lock:
                if self.closed or ACTIVE_COLLECTOR.get() is not self.collector:
                    return
                record = message.record
                extra = record["extra"]
                text = extra.get("autowork_message", redacted(record["message"]))
                phase = self.phase
                if phase == "execution" and self.collector.active:
                    self.collector.record_log(text, level=record["level"].name, source={
                        "module": record["name"], "function": record["function"], "line": record["line"],
                    })
                rendered = (
                    f"{record['time'].isoformat()} | {record['level'].name} | "
                    f"{self.collector.execution_id} | {extra.get('scope_id', self.collector.execution_id)} | "
                    f"{phase}:{extra.get('autowork_context', '')} | "
                    f"{record['name']}:{record['function']}:{record['line']} | {text}\n"
                )
                if self._console:
                    try:
                        console = (
                            f"{record['time'].strftime('%H:%M:%S.%f')[:-3]} | {record['level'].name} | "
                            f"{record['name']}:{record['function']}:{record['line']} | {record['message']}\n"
                        )
                        sys.stderr.write(console)
                    except Exception as error:
                        self._failure(phase, "console", error)
                limit = self.collector.max_execution_log_bytes if phase == "execution" else self.collector.max_delivery_log_bytes
                size = len(rendered.encode("utf-8"))
                summary = self.summary[phase]
                if summary.get("truncated") or summary["bytes"] + size > limit:
                    summary["dropped"] += 1
                    summary["truncated"] = True
                    self._missing(phase, "budget")
                    return
                try:
                    sink = self._open_file(phase)
                    if sink is not None:
                        payload = type(message)(rendered)
                        payload.record = record
                        sink.write(payload)
                        summary["bytes"] += size
                    elif self.collector.directory is not None:
                        summary["dropped"] += 1
                        if summary["status"] == "not_requested":
                            self._failure(phase, "unavailable", RuntimeError("Run log output is unavailable"))
                except Exception as error:
                    summary["dropped"] += 1
                    self._failure(phase, "write", error)
        except Exception as error:
            self._failure(self.phase, "capture", error)

    def _missing(self, phase, reason):
        self.summary[phase]["status"] = "incomplete"
        key = (phase, reason)
        if key in self._warned:
            return
        self._warned.add(key)
        error = RuntimeError(f"Run {phase} log {reason} caused missing records")
        if phase == "execution":
            self.collector.record_error("log_" + reason, error)
        self._emergency(phase, reason, error)

    def _failure(self, phase, stage, error):
        self.summary[phase]["status"] = "failed"
        if stage != "console":
            self._failed.add(phase)
        if phase == "execution":
            self.collector.record_error("log_" + stage, error)
        key = (phase, stage)
        if key not in self._warned:
            self._warned.add(key)
            self.summary[phase].setdefault("errors", []).append({
                "stage": stage, "type": type(error).__name__, "message": redacted(str(error))[:4096],
            })
            self._emergency(phase, stage, error)

    def _emergency(self, phase, stage, error):
        try:
            sys.stderr.write(f"Evidence log {phase}/{stage} unavailable: {type(error).__name__}\n")
        except Exception:
            pass

    def _close_file(self, phase):
        sink = self._files.pop(phase, None)
        if sink is not None:
            try:
                sink.stop()
            except Exception as error:
                self._failure(phase, "close", error)
        summary = self.summary[phase]
        if summary["status"] == "recording":
            summary["status"] = "completed" if summary["bytes"] else "not_requested"

    def seal_execution(self):
        with self._lock:
            self._close_file("execution")
            self.phase = "delivery"

    def stop(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            for phase in ("execution", "delivery"):
                self._close_file(phase)
            self._persist_delivery()

    def restore(self):
        with logger._core.lock:
            if logger._core.patcher is self._patcher:
                previous = self._previous_patcher
                while previous is not None:
                    capture = getattr(previous, "_autowork_capture", None)
                    if capture is None or not capture.closed:
                        break
                    previous = capture._previous_patcher
                logger._core.patcher = previous

    def _persist_delivery(self):
        if self.collector.directory is None or (
            self.summary["delivery"]["status"] == "not_requested"
            and not self.collector.deliveries and not self.collector.cleanup_errors
        ):
            return
        try:
            path = self.collector.directory / "logs/delivery-summary.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({
                "execution_id": self.collector.execution_id,
                "deliveries": self.collector.deliveries, "logging": self.summary["delivery"],
                "cleanup_errors": self.collector.cleanup_errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except Exception as error:
            self._emergency("delivery", "summary", error)


def install_log_capture(collector):
    configuration = collector._capture_settings
    if configuration is None:
        from config.settings import settings
        configuration = {name: getattr(settings, name) for name in (
            "log_file", "log_level", "log_rotation", "log_retention",
        )}
    if collector._log_capture is None:
        collector._log_capture = RunLogCapture(collector, configuration)
    return collector._log_capture.install()