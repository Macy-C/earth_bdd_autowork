"""项目侧最终报告合并入口。

右键运行本文件时：

1. 只在顶部改默认输入/输出路径。
2. 在下面对应方法里填写本次操作的参数。
3. 在文件底部选择要执行的方法。

需要命令行参数时，仍可使用 python -m Bdd.final_report merge ...。
"""

import sys

from autowork_core.runtime.reporting.final_report import (
    DEFAULT_FINAL_REPORT_TEXT,
    DEFAULT_SOURCE_REPORT_TEXT,
    create as _create,
    delete as _delete,
    main,
    merge as _merge,
    render as _render,
)


DEFAULT_INPUT_REPORT_JSON = DEFAULT_SOURCE_REPORT_TEXT
DEFAULT_FINAL_REPORT_JSON = DEFAULT_FINAL_REPORT_TEXT


def create_final_report():
    return _create(
        DEFAULT_INPUT_REPORT_JSON,
           final_report_json=DEFAULT_FINAL_REPORT_JSON,
    )


def merge_final_report():
    return _merge(
        DEFAULT_INPUT_REPORT_JSON,
            final_report_json=DEFAULT_FINAL_REPORT_JSON,
        allow_add=False,
        # 目标不唯一时再填写：
        # feature_file=r"Bdd\test_features\calc\calc.feature",
        # scenario_name="计算相加",
        # example_id="1.1",
    )


def delete_from_final_report():
    return _delete(
           final_report_json=DEFAULT_FINAL_REPORT_JSON,
        # 删除整个 Feature 时填 feature_file 或 feature_name。
        # 删除单个 Scenario/Example 时再填 scenario_name / example_id。
        # feature_file=r"Bdd\test_features\calc\calc.feature",
        # scenario_name="计算相加",
        # example_id="1.1",
    )


def render_final_report():
    # 通常不需要手动调用；create/merge/delete 会自动刷新 HTML。
    # 仅在手动修改最终 JSON 或 HTML 丢失时使用。
    return _render(
           final_report_json=DEFAULT_FINAL_REPORT_JSON,
    )


def _print_result(result):
    target, html = result
    print(f"Final report JSON: {target}")
    print(f"Final report HTML: {html}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main())

    raise SystemExit(_print_result(create_final_report()))
    # raise SystemExit(_print_result(merge_final_report()))
    # raise SystemExit(_print_result(delete_from_final_report()))
    # raise SystemExit(_print_result(render_final_report()))
