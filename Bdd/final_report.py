"""项目侧最终报告合并入口。

右键运行本文件时，先改 ACTION，再只看对应的配置块：

- ACTION = "create": 只看 CREATE。
- ACTION = "merge": 只看 MERGE。
- ACTION = "delete": 只看 DELETE。
- ACTION = "render": 只看 RENDER。

配置键：

- report_json: create / merge 的操作依据报告 JSON。
- feature_file / feature_name: 可选；报告里有多个 Feature 时用于指定 Feature。
- scenario_name: 可选；只合并或删除这个 Scenario/Outline；merge 不填时由报告内容推断。
- example_id: 可选；Scenario Outline 的 Example 行，例如 1.1。
- allow_add: 仅 merge 使用；目标报告没有对应项时是否允许新增，默认 True。
- target_report: delete / render 可用；自定义最终报告 JSON 路径，通常不用填。
- html_report / merge_log: 可选；自定义最终 HTML 或内部操作日志路径，通常不用填。

也可以在 Python 里直接调用 create(...) / merge(...) / delete(...) / render(...)，第一个参数都是报告 JSON。
需要命令行参数时，仍可使用 python -m Bdd.final_report merge ...。
"""

from autowork_core.runtime.reporting.final_report import (
    DEFAULT_SOURCE_REPORT_TEXT,create,delete,main,merge,render,run_entrypoint,)


DEFAULT_SOURCE_REPORT = DEFAULT_SOURCE_REPORT_TEXT
ACTION = "create"

CREATE = {
    "report_json": DEFAULT_SOURCE_REPORT,
}

MERGE = {
    "report_json": DEFAULT_SOURCE_REPORT,
    "allow_add": False,
}

DELETE = {
    # 默认从 artifacts/final-reports/autowork-final-report.json 删除。
    # 删除整个 Feature 时填 feature_file 或 feature_name。
    # 删除单个 Scenario/Example 时再填 scenario_name / example_id。
    # "feature_file": r"Bdd\test_features\calc\calc.feature",
    # "scenario_name": "计算相加",
}

RENDER = {
    # 默认渲染 artifacts/final-reports/autowork-final-report.json。
    # "target_report": r"C:\Users\320321651\Messy\projects\bdd_autowork\artifacts\final-reports\autowork-final-report.json",
}


if __name__ == "__main__":
    raise SystemExit(run_entrypoint(globals()))
