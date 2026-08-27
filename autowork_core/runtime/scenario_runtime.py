"""管理单个 Scenario 的 RootStore、进程、录屏和应用资源。

Manages per-scenario roots, processes, recording, and application resources.
"""

import ntpath
import subprocess
from dataclasses import dataclass, field
from importlib import import_module
from time import sleep
from typing import TYPE_CHECKING

import psutil
import win32gui
from loguru import logger

from autowork_core.common.root_store import RootStore
from autowork_core.runtime.application_lifecycle import (
    notify_application_started,
    prepare_application_lifecycle,
)
from autowork_core.runtime.status import should_keep_artifacts
from config.settings import settings

if TYPE_CHECKING:
    from autowork_core.runtime.tag_manager import RuntimeTagDecision

STEP_PROCESS_SNAPSHOT_WAIT = 1
PROCESS_CLEANUP_ATTEMPTS = 3
PROCESS_CLEANUP_WAIT_SECONDS = 1

@dataclass
class ScenarioRuntimeState:
    windows: RootStore = field(default_factory=RootStore)
    pages: dict = field(default_factory=dict)
    variables: dict[str, object] = field(default_factory=dict)
    tag_decision: "RuntimeTagDecision | None" = None
    root_pid: int | None = None
    window_handles_before: set[int] = field(default_factory=set)
    process_snapshot_before: set[int] = field(default_factory=set)
    process_tracking_pending: bool = False
    started_pids: set[int] = field(default_factory=set)
    started_processes: dict[int, psutil.Process] = field(default_factory=dict)
    record_started: bool = False
    record_path: str | None = None
    application_cleanup_required: bool = False
    application_lifecycle_active: bool = False

def get_scenario_state(context):
    state = getattr(context, "autowork_scenario", None)
    if state is None:
        raise RuntimeError("Autowork scenario context is not initialized")
    return state

def initialize_ui_scenario(context, scenario):
    state = get_scenario_state(context)
    auto_mode = settings.app_launch_mode != "attach"

    prepare_application_lifecycle(
        context,
        scenario,
        launch_mode=settings.app_launch_mode,
    )

    _start_scenario_recording(context, scenario, state)

    if auto_mode:
        _start_managed_application(context, scenario, state)

    prepare_project_scenario(context, scenario)

def _start_managed_application(context, scenario, state):
    state.window_handles_before = get_top_level_window_handles()
    state.windows.set_launch_handle_baseline(state.window_handles_before)
    begin_process_tracking(context)
    state.root_pid = start_configured_application()
    state.application_cleanup_required = True
    finalize_non_snapshot_tracking(context)
    notify_application_started(context, scenario)

def _project_hook(name):
    try:
        application = import_module("Bdd.application")
    except ModuleNotFoundError as error:
        if error.name not in {"Bdd", "Bdd.application"}:
            raise
        return None

    hook = getattr(application, name, None)
    return hook if callable(hook) else None

def _require_project_hook(name):
    hook = _project_hook(name)
    if hook is None:
        raise RuntimeError(
            f"APP_PATH=runtime要求Bdd.application.{name}"
        )
    return hook

def prepare_project_scenario(context, scenario):
    hook = _project_hook("prepare_scenario")
    if hook is not None:
        hook(context, scenario)

def _start_scenario_recording(context, scenario, state):
    if not settings.record_enabled:
        return
    state.record_path = context.autowork_run.recorder.start(
        context.feature.name,
        scenario.name,
        mode=settings.effective_record_mode,
    )
    state.record_started = bool(
        getattr(context.autowork_run.recorder, "_running", False)
    )

def finish_scenario_recording(context, scenario):
    state = get_scenario_state(context)
    recorder = context.autowork_run.recorder
    if not settings.record_enabled or not state.record_started:
        return None

    record_path = None
    if settings.record_all:
        record_path = recorder.stop()
        if should_keep_artifacts(scenario.status):
            if record_path:
                logger.error(f"[场景失败，录屏已保存] {record_path}")
            else:
                logger.warning("[场景失败，但未生成录屏]")
        else:
            recorder.delete()
            logger.debug(f"[录屏已删除] {record_path}")
    elif settings.record_failed:
        recorder.stop()
        if should_keep_artifacts(scenario.status):
            record_path = recorder.save_buffered_video()
            if record_path:
                logger.error(f"[场景失败，最近录屏已保存] {record_path}")
        else:
            recorder.clear()
            logger.debug("[场景通过，录屏缓存已清空]")
    return record_path

def log_scenario_runtime_state(context):
    run = context.autowork_run
    feature = context.autowork_feature
    scenario = get_scenario_state(context)
    logger.opt(lazy=True).debug(" ++++++ config     data  |{}", lambda: settings.as_dict())
    logger.opt(lazy=True).debug(" ++++++ public     data  |{}", lambda: run.public_data.public_data)
    logger.opt(lazy=True).debug(" ++++++ windows    root  |{}", lambda: scenario.windows)
    logger.opt(lazy=True).debug(" ++++++ test    locator  |{}", lambda: feature.locators)
    logger.opt(lazy=True).debug(" ++++++ test       data  |{}", lambda: feature.data)

def get_process_snapshot():
    return {
        process.pid: process
        for process in psutil.process_iter(attrs=["pid"])
    }


def get_top_level_window_handles():
    handles = set()

    def collect(handle, _data):
        handles.add(int(handle))
        return True

    win32gui.EnumWindows(collect, None)
    return handles

def get_pid_snapshot():
    return set(get_process_snapshot())

def begin_process_tracking(context):
    state = get_scenario_state(context)
    mode = str(settings.app_process_track_mode or "snapshot").strip().lower()
    if mode not in ("root", "none"):
        state.process_snapshot_before = get_pid_snapshot()
    state.process_tracking_pending = True

def safe_process_name(pid):
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "unknown"

def start_configured_application():
    if str(settings.app_path).strip().lower() == "runtime":
        _require_project_hook("start_application")()
        root_pid = None
    else:
        root_pid = start_app_path(settings.app_path)

    return root_pid

def start_app_path(app_path):
    app_path = str(app_path).strip()
    if not app_path:
        raise ValueError("APP_SETTING.APP_PATH 不能为空")

    app_dir = ntpath.dirname(app_path)
    if app_path.lower().endswith((".cmd", ".bat")):
        process = subprocess.Popen(app_path, cwd=app_dir or None, shell=True)
    else:
        process = subprocess.Popen([app_path], cwd=app_dir or None, shell=False)
    return process.pid

def track_started_processes(
        before_pids,
        root_pid=None,
        process_track_mode="snapshot",
    current_pids=None,
):
    process_track_mode = str(process_track_mode or "snapshot").strip().lower()

    if process_track_mode == "none":
        started_pids = set()
    elif process_track_mode == "root":
        started_pids = {root_pid} if root_pid is not None else set()
    else:
        if process_track_mode != "snapshot":
            logger.warning(
                f"未知进程追踪模式 process_track_mode={process_track_mode}, 已按 snapshot 处理"
            )
        started_pids = (
            set(current_pids)
            if current_pids is not None
            else get_pid_snapshot()
        ) - before_pids

    logger.debug(
        f"process_track_mode={process_track_mode}, "
        f"started_pids={[(pid, safe_process_name(pid)) for pid in sorted(started_pids)]}"
    )
    return started_pids

def finalize_process_tracking(context):
    state = get_scenario_state(context)
    if not state.process_tracking_pending:
        return state.started_pids

    mode = str(settings.app_process_track_mode or "snapshot").strip().lower()
    current_processes = (
        get_process_snapshot()
        if mode not in ("root", "none")
        else None
    )
    state.started_pids = track_started_processes(
        state.process_snapshot_before,
        root_pid=state.root_pid,
        process_track_mode=mode,
        current_pids=(
            current_processes.keys()
            if current_processes is not None
            else None
        ),
    )
    state.started_processes = (
        {
            pid: current_processes[pid]
            for pid in state.started_pids
            if pid in current_processes
        }
        if current_processes is not None
        else _capture_processes(state.started_pids)
    )
    state.process_tracking_pending = False
    return state.started_pids

def finalize_process_tracking_before_step(context):
    state = get_scenario_state(context)
    if not state.process_tracking_pending:
        return state.started_pids

    mode = str(settings.app_process_track_mode or "snapshot").strip().lower()
    if mode not in ("root", "none"):
        sleep(STEP_PROCESS_SNAPSHOT_WAIT)
    return finalize_process_tracking(context)

def finalize_non_snapshot_tracking(context):
    mode = str(settings.app_process_track_mode or "snapshot").strip().lower()
    if mode in ("root", "none"):
        finalize_process_tracking(context)

def _kill_process(process, tracked_pid):
    try:
        process.kill()
    except psutil.NoSuchProcess:
        pass
    except Exception as error:
        logger.warning(
            f"清理 APP_PATH 进程失败 pid={getattr(process, 'pid', tracked_pid)}, "
            f"tracked_pid={tracked_pid}: {error}"
        )

def _process_pid(process, fallback=None):
    try:
        return int(process.pid)
    except (AttributeError, TypeError, ValueError, psutil.Error):
        return fallback

def _capture_processes(pids):
    processes = {}
    for tracked_pid in sorted(set(pids)):
        try:
            process = psutil.Process(tracked_pid)
        except psutil.NoSuchProcess:
            continue
        except Exception as error:
            logger.warning(f"读取 APP_PATH 进程失败 pid={tracked_pid}: {error}")
            continue
        processes[tracked_pid] = process
    return processes

def _collect_process_tree(roots):
    processes = {}
    uncertain_pids = set()
    for process in roots:
        tracked_pid = _process_pid(process)
        if tracked_pid is None:
            continue
        try:
            if not process.is_running():
                continue
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except Exception as error:
            logger.warning(
                f"验证 APP_PATH 进程身份失败 pid={tracked_pid}: {error}"
            )
            uncertain_pids.add(tracked_pid)
            continue
        processes[tracked_pid] = process
        try:
            descendants = process.children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        except Exception as error:
            descendants = []
            logger.warning(
                f"读取 APP_PATH 子进程失败 pid={tracked_pid}: {error}"
            )
            uncertain_pids.add(tracked_pid)
        for descendant in descendants:
            descendant_pid = _process_pid(descendant)
            if descendant_pid is not None:
                processes[descendant_pid] = descendant

    parent_by_pid = {}
    for pid, process in processes.items():
        try:
            parent_by_pid[pid] = int(process.ppid())
        except (AttributeError, TypeError, ValueError, psutil.Error):
            parent_by_pid[pid] = None

    def process_depth(pid):
        depth = 0
        visited = {pid}
        parent_pid = parent_by_pid.get(pid)
        while parent_pid in processes and parent_pid not in visited:
            depth += 1
            visited.add(parent_pid)
            parent_pid = parent_by_pid.get(parent_pid)
        return depth

    ordered = [
        processes[pid]
        for pid in sorted(
            processes,
            key=lambda item: (-process_depth(item), item),
        )
    ]
    return ordered, uncertain_pids

def _kill_processes(processes):
    pending = list(processes)
    uncertain_pids = set()
    for attempt in range(1, PROCESS_CLEANUP_ATTEMPTS + 1):
        process_tree, current_uncertain = _collect_process_tree(pending)
        uncertain_pids.update(current_uncertain)
        if not process_tree:
            return uncertain_pids
        for process in process_tree:
            _kill_process(process, _process_pid(process))
        try:
            _gone, alive = psutil.wait_procs(
                process_tree,
                timeout=PROCESS_CLEANUP_WAIT_SECONDS,
            )
        except Exception as error:
            alive = process_tree
            logger.warning(f"等待 APP_PATH 进程退出失败: {error}")
        pending_pids = {
            pid
            for process in alive
            if (pid := _process_pid(process)) is not None
        }
        if not pending_pids:
            return uncertain_pids
        pending = alive
        logger.warning(
            f"APP_PATH 进程清理第 {attempt} 轮后仍存活: "
            f"{sorted(pending_pids)}"
        )
    return pending_pids | uncertain_pids

def close_app_and_release_resource(context):
    state = get_scenario_state(context)
    finalize_process_tracking_before_step(context)
    if settings.app_launch_mode == "attach":
        logger.debug("attach 模式，保留已运行应用")
    elif str(settings.app_path).strip().lower() == "runtime":
        _require_project_hook("stop_application")()
    elif state.started_processes:
        remaining_pids = _kill_processes(state.started_processes.values())
        if remaining_pids:
            logger.warning(
                "APP_PATH 进程清理结束后仍有残留或无法确认完整退出: "
                f"{[(pid, safe_process_name(pid)) for pid in sorted(remaining_pids)]}"
            )

    sleep(1)
    state.application_cleanup_required = False

def cleanup_project_scenario(context, scenario):
    hook = _project_hook("cleanup_scenario")
    if hook is not None:
        hook(context, scenario)
