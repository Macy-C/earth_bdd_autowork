from pathlib import Path
import sys

from config.paths import Paths
from config.settings import settings
from .collector import RunEvidenceCollector, UnavailableEvidence
from .context import ACTIVE_COLLECTOR, collector_for, collector_scope


def dispatch_evidence(context, name, *args, **kwargs):
    collector = collector_for(context)
    if collector is None:
        return
    try:
        return getattr(collector, name)(context, *args, **kwargs)
    except Exception as error:
        collector.record_error(name, error)


def execute_with_evidence(runner, callback):
    existing = ACTIVE_COLLECTOR.get()
    if existing is not None and getattr(existing, "_runner", None) is runner:
        return callback()
    try:
        collector = RunEvidenceCollector(
            Path(Paths.ARTIFACTS_DIR) / "runtime-evidence",
            detailed=settings.knowledge_capture_enabled,
        )
    except Exception as failure:
        collector = UnavailableEvidence(failure)
        print(f"Evidence capture disabled: {type(failure).__name__}", file=sys.stderr)
    collector._runner = runner
    result = None
    error = None
    with collector_scope(collector):
        try:
            try:
                collector.start(settings)
            except Exception as failure:
                collector.record_error("start", failure)
            result = callback()
            return result
        except BaseException as caught:
            error = caught
            raise
        finally:
            try:
                aborted = isinstance(error, (KeyboardInterrupt, SystemExit)) or bool(getattr(runner, "aborted", False))
                outcome = "aborted" if aborted else "failed" if error is not None or result else "passed"
                collector.finalize_run(outcome, error)
                from autowork_core.runtime.context_runtime import finish_run_state
                context = getattr(runner, "context", None)
                if context is not None:
                    finish_run_state(context)
            except Exception as failure:
                print(f"Evidence finalization unavailable: {type(failure).__name__}", file=sys.stderr)
            finally:
                collector.close()