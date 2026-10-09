import subprocess
from time import sleep
from loguru import logger
from autowork_core.page import get_page
from autowork_core.runtime.tag_manager import normalize_tag
from config.settings import settings


def _uses_gantry(scenario):
    return "gantry" in {
        normalize_tag(tag)
        for tag in getattr(scenario, "effective_tags", ())
    }

def start_simulator():
    # sleep(5)
    # _start_hidden_file(SIMULATOR_ENGINE)
    # sleep(5)
    # _start_file(SIMULATORS)
    pass



def start_application():
    """Start the host application when APP_PATH is set to runtime."""
    raise NotImplementedError(
        "Implement Bdd.application.start_application when APP_PATH=runtime"
    )


def stop_application():
    """Stop resources owned by start_application."""
    raise NotImplementedError(
        "Implement Bdd.application.stop_application when APP_PATH=runtime"
    )


def prepare_scenario(context, scenario):
    """
    Prepare optional project resources before scenario steps.
    Behave before_scenario
  -> 跳过 @maint/@skip/@rep
  -> @api 则跳过所有 UI 初始化
  -> before_app_start 回调
  -> 启动录屏
  -> auto 模式启动主应用
  -> after_app_start 回调
  -> Bdd.application.prepare_scenario
  -> 执行第一个 Step
  真实入口在 environment.py:82-112
  具体顺序由 scenario_runtime.py:44-74 控制。
    
    """
    auto_mode = settings.app_launch_mode != "attach"
    if _uses_gantry(scenario):
        from Bdd.page_obj.simulator.page import SimulatorPage

        if auto_mode:
            sleep(2)
            start_simulator()
        simulator = get_page(context, SimulatorPage)
        context.autowork_scenario.simulator = simulator
        try:
            # simulator.initialize(auto_mode=auto_mode, timeout=60)
            simulator.wait_until_open(timeout=20)
        except Exception:
            cleanup_scenario(context, scenario)
            raise
    # if settings.app_launch_mode == "auto":
    #     # 软件已经启动，在这里等待登录界面并执行登录
    #     pass
    # elif settings.app_launch_mode == "attach":
    #     # 使用已打开的软件，在这里执行 attach 所需的准备操作
    #     pass


def cleanup_scenario(context, scenario):
    """Release resources acquired by prepare_scenario."""
    state = context.autowork_scenario
    state.simulator = None

    # simulator = getattr(state, "simulator", None)
    # try:
    #     if simulator is not None:
    #         simulator.close()
    # except Exception as error:
    #     logger.warning(f"释放 SimulatorPage 失败: {error}")


def _run_command(command, cwd=None):
    logger.info("> {}", subprocess.list2cmdline(command))
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        startupinfo=_hidden_startupinfo(),
    )
    output = (completed.stdout or completed.stderr).strip()
    if output:
        logger.info(output)
    return completed

def _hidden_startupinfo():
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo