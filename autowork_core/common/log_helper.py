import inspect
from loguru import logger

def log_call(entry_point=None, **kwargs):
    func_name = inspect.currentframe().f_back.f_code.co_name
    entry_point = entry_point or func_name

    color = "yellow" if entry_point == func_name else "magenta"

    logger.opt(colors=True, lazy=True).debug(
        f"<{color}>[entry_point: {{}}][func: {{}}] {{}}</{color}>",
        lambda: entry_point,
        lambda: func_name,
        lambda: " | ".join(f"{k}={v}" for k, v in kwargs.items())
    )
    return entry_point

"""
log_call()：函数调用日志
log_return()：返回值日志
log_error()：异常日志
format_kv()：格式化参数
get_caller_name()：获取调用函数名
get_log_colors()：统一颜色策略
"""

