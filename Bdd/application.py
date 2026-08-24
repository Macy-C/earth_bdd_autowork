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
    pass


def cleanup_scenario(context, scenario):
    """Release resources acquired by prepare_scenario."""
    pass