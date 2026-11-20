from contextlib import contextmanager
from contextvars import ContextVar


ACTIVE_COLLECTOR = ContextVar("autowork_evidence_collector", default=None)
ACTIVE_ACTION = ContextVar("autowork_evidence_action", default=None)
EXECUTION_ORIGIN = ContextVar("autowork_evidence_origin", default=None)


@contextmanager
def collector_scope(collector):
    token = ACTIVE_COLLECTOR.set(collector)
    action_token = ACTIVE_ACTION.set(None)
    try:
        yield collector
    finally:
        ACTIVE_ACTION.reset(action_token)
        ACTIVE_COLLECTOR.reset(token)


def collector_for(context):
    run = getattr(context, "autowork_run", None)
    return getattr(run, "collector", None)


def debug_image_directory(default):
    collector = ACTIVE_COLLECTOR.get()
    if collector is not None and collector.active and collector.asset_directory is not None:
        return collector.asset_directory / "debug"
    return default


def register_debug_image(path, name):
    collector = ACTIVE_COLLECTOR.get()
    if collector is not None and collector.active:
        try:
            collector.register_artifact(path, name=name)
        except Exception as error:
            collector.record_error("debug_image", error)