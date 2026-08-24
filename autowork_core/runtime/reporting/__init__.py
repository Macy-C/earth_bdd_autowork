"""报告子系统公共入口，暴露 SparkReporter 和无状态 Hook 分发函数。

Public reporting entry point for SparkReporter and stateless hook dispatch.
"""

from loguru import logger

from autowork_core.runtime.reporting.spark import SparkReporter


def dispatch_report_hook(reporter, hook_name, *args, **kwargs):
	if reporter is None:
		return None
	hook = getattr(reporter, hook_name, None)
	if hook is None:
		return None
	try:
		return hook(*args, **kwargs)
	except Exception as error:
		logger.debug(
			f"Spark 报告 Hook 执行失败: hook={hook_name}, err={error}"
		)
		return None


__all__ = ["SparkReporter", "dispatch_report_hook"]
