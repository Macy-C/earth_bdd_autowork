from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROUTER_VERSION = "1.8"
BINDING_VERSION = "1.3"
AGENT_WAIT_LEDGER_VERSION = "1.0"
CANDIDATE_INDEX_MAX_LINES_PER_READ = 50
_ROUTING_DIRECTORY = Path(".copilot/recorder-routing")
_TERMINAL_STATUSES = {"completed", "failed"}
_ACTIVE_STATUSES = {"ready", "running"}
_EVENT_NAMES = {
    "userpromptsubmit": "UserPromptSubmit",
    "pretooluse": "PreToolUse",
    "posttooluse": "PostToolUse",
    "stop": "Stop",
}
_JOB_COMMANDS = {
    "inspect-job",
    "advance-job",
    "submit-business-review-answers",
    "submit-business-facts",
    "settle-job",
    "generate-job",
    "job-evidence",
    "job-compare-takes",
    "job-action-knowledge",
    "job-design-context",
    "job-task-bundle",
}
_JOB_RESULT_REPORT_COMMANDS = {
    "job-code-diff",
}
_REPORT_COMMANDS = {
    "job-implementation-packet",
    "job-implementation-candidate",
    "job-code-diff",
}
_TYPED_PATCH_DESIGN_REQUIRED_REASONS = frozenset({
    "system_baseline_needs_ai_naming",
    "system_baseline_needs_ai_ambiguity_choice",
    "system_baseline_needs_ai_assertion_choice",
    "system_baseline_needs_ai_method_choice",
    "system_baseline_needs_ai_operation_choice",
    "system_baseline_needs_ai_value_source_choice",
})
_COMMANDS_BY_PHASE = {
    "ready": {
        "inspect-job",
        "advance-job",
        "settle-job",
        "job-evidence",
        "job-compare-takes",
        "job-action-knowledge",
        "job-design-context",
        "job-task-bundle",
    },
    "design": {
        "inspect-job",
        "advance-job",
        "submit-business-review-answers",
        "submit-business-facts",
        "settle-job",
        "generate-job",
        "job-evidence",
        "job-compare-takes",
        "job-action-knowledge",
        "job-design-context",
        "job-task-bundle",
    },
    "implementation": {
        "inspect-job",
        "advance-job",
        "generate-job",
        "job-implementation-packet",
        "job-implementation-candidate",
        "job-code-diff",
    },
    "runtime": {
        "inspect-job",
        "job-code-diff",
    },
    "oracle": {
        "inspect-job",
        "job-code-diff",
    },
}
_READ_TOOL_MARKERS = (
    "read",
    "search",
    "find",
    "list",
    "get",
    "inspect",
    "view",
)
_WRITE_TOOL_MARKERS = (
    "edit",
    "write",
    "create",
    "delete",
    "rename",
    "move",
    "replace",
    "patch",
)
_TERMINAL_TOOL_MARKERS = (
    "terminal",
    "command",
    "execute",
    "shell",
)
_QUESTION_TOOL_MARKERS = ("askquestions",)
_PYTHON_ENVIRONMENT_TOOL_MARKERS = (
    "pylancepythonenvironments",
    "getpythonenvironmentdetails",
)
_PATH_FIELDS = {
    "file",
    "filepath",
    "file_path",
    "path",
    "uri",
}
_PATH_LIST_FIELDS = {
    "files",
    "filepaths",
    "file_paths",
    "paths",
    "uris",
}
_GENERATION_MODULE = (
    "autowork_core.utils.debug_tools.recorder.generation_workflow"
)
_PROMPT_PATTERN = re.compile(
    r"^\s*(?P<path>\"[^\"]+\"|'[^']+'|\S+)\s*$",
    flags=re.DOTALL,
)
_PROMPT_COMMAND_JOB_PATTERN = re.compile(
    r"(?P<path>\"[^\"\r\n]*[/\\]ai[/\\]generation-jobs[/\\][^\"\r\n]+?\.json\"|"
    r"'[^'\r\n]*[/\\]ai[/\\]generation-jobs[/\\][^'\r\n]+?\.json'|"
    r"\S*[/\\]ai[/\\]generation-jobs[/\\]\S+?\.json)",
    flags=re.IGNORECASE,
)
_PATCH_PATH_PATTERN = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File: (?P<path>.+?)(?: -> .+)?$",
    flags=re.MULTILINE,
)


def route_hook_event(event, payload, *, project_root):
    """Return a VS Code Hook response without mutating Recorder workflow state."""
    event = _normalize_event(event)
    payload = payload if isinstance(payload, dict) else {}
    project_root = Path(project_root).resolve()
    session_id = _session_id(payload)
    binding, binding_error = (
        _load_binding(project_root, session_id)
        if session_id
        else (None, None)
    )
    if event == "UserPromptSubmit":
        return _route_user_prompt(project_root, session_id, payload)
    if not session_id:
        return _unbound_event_response(event)
    if binding_error:
        result = _binding_error_response(event, binding_error)
    elif binding is None:
        return _unbound_event_response(event)
    else:
        control, control_error = _bound_job_control(
            project_root,
            session_id,
            binding,
        )
        if control_error:
            result = _binding_error_response(event, control_error)
        elif _is_terminal(control):
            _record_agent_wait_event(
                project_root,
                session_id,
                binding,
                control,
                event,
                payload,
                outcome="terminal",
            )
            _clear_binding(project_root, session_id)
            result = _terminal_event_response(event, control)
        elif event == "PreToolUse":
            result = _route_pre_tool(
                project_root,
                session_id,
                binding,
                control,
                payload,
            )
        elif event == "PostToolUse":
            result = _post_tool_response(control, binding)
        elif event == "Stop":
            result = _route_stop(control, payload)
        else:
            result = _unbound_event_response(event)
        if not control_error and not _is_terminal(control):
            _record_agent_wait_event(
                project_root,
                session_id,
                binding,
                control,
                event,
                payload,
                outcome=_hook_event_outcome(result),
            )
    return result


def _route_user_prompt(project_root, session_id, payload):
    submitted_at = datetime.now().isoformat(timespec="milliseconds")
    prompt = str(payload.get("prompt") or "")
    job_path = _prompt_job_path(project_root, prompt)
    if job_path is None:
        if not session_id:
            return _stop(
                "Recorder Generation Agent 只接受 Workbench 生成的 Job 路径，"
                "且必须取得 Copilot 会话标识。"
            )
        binding, binding_error = _load_binding(project_root, session_id)
        if binding_error:
            return _stop("Recorder Generation会话绑定无效。请新开会话后重新生成。")
        if binding is None:
            return _stop(
                "Recorder Generation Agent 只接受 Workbench 生成的 Job 路径。"
            )
        control, control_error = _bound_job_control(
            project_root,
            session_id,
            binding,
        )
        if control_error:
            return _stop(control_error)
        if _is_terminal(control):
            _clear_binding(project_root, session_id)
            return _allow(
                "此前的 Recorder Generation Job 已结束；当前会话已解除绑定。"
            )
        return _stop(
            "当前会话正在完成 Recorder 全场景生成；请继续同一个 Job，"
            "不要插入其他任务。"
        )
    if not session_id:
        return _stop("无法取得 Copilot 会话标识，Recorder 生成没有启动。")
    control, error = _inspect_bound_job(job_path)
    if error:
        return _stop(error)
    if _is_terminal(control):
        return _allow("该 Recorder Generation Job 已结束；可以查看其结果。")
    if str(control.get("status") or "") not in _ACTIVE_STATUSES:
        return _stop("该 Recorder Generation Job 当前不能开始或继续生成。")
    existing, binding_error = _load_binding(project_root, session_id)
    if binding_error:
        return _stop("Recorder Generation会话绑定无效。请新开会话后重新生成。")
    if existing is not None:
        existing_path = _binding_job_path(project_root, existing)
        if existing_path != job_path:
            return _stop("一个 Copilot 会话只能绑定一个未结束的 Recorder Generation Job。")
        return _allow(_bound_context(control, existing, resumed=True))
    if _job_phase(control) == "ready":
        return _stop(
            "Recorder Generation Job 尚未由 Workbench claim；请从 Workbench "
            "重新发起生成。"
        )
    binding = _new_binding(project_root, session_id, job_path, control)
    try:
        _write_binding(project_root, session_id, binding)
    except OSError as error:
        return _stop(
            "无法建立 Recorder Generation 会话绑定："
            f"{type(error).__name__}。生成没有启动。"
        )
    _record_agent_wait_event(
        project_root,
        session_id,
        binding,
        control,
        "UserPromptSubmit",
        payload,
        outcome="allowed",
    )
    return _allow(_bound_context(control, binding, resumed=False))


def _route_pre_tool(project_root, session_id, binding, control, payload):
    tool_name = str(
        payload.get("tool_name") or payload.get("toolName") or ""
    )
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = payload.get("toolInput")
    if not tool_name or not isinstance(tool_input, dict):
        return _deny("Recorder Generation 无法识别本次工具调用，已拒绝执行。")
    if binding.get("python_selection_status") == "pending":
        return _route_python_selection_tool(
            project_root,
            session_id,
            binding,
            control,
            tool_name,
            tool_input,
        )
    tool_kind = _tool_kind(tool_name, tool_input)
    if tool_kind == "read":
        error = _validate_read_tool(
            project_root,
            session_id,
            binding,
            control,
            tool_input,
        )
        return _deny(error) if error else _pre_tool_allow(control, binding)
    if tool_kind == "question":
        error = _validate_question_tool(control, tool_input)
        return _deny(error) if error else _pre_tool_allow(control, binding)
    if tool_kind == "terminal":
        error = _validate_terminal_command(
            project_root,
            binding,
            control,
            tool_input,
        )
        return _deny(error) if error else _pre_tool_allow(control, binding)
    if tool_kind == "write":
        error = _validate_write_paths(
            project_root,
            binding,
            control,
            tool_name,
            tool_input,
        )
        return _deny(error) if error else _pre_tool_allow(control, binding)
    return _deny(
        "Recorder Generation 只允许读取、受限原生文件编辑和单条"
        " generation_workflow 命令。"
    )


def _route_python_selection_tool(
        project_root,
        session_id,
        binding,
        control,
        tool_name,
        tool_input,
    ):
    normalized = re.sub(r"[^a-z0-9]", "", str(tool_name).casefold())
    if any(marker in normalized for marker in _PYTHON_ENVIRONMENT_TOOL_MARKERS):
        workspace_root = tool_input.get("workspaceRoot")
        if workspace_root:
            try:
                requested_root = _resolve_project_path(
                    project_root,
                    workspace_root,
                )
            except ValueError:
                return _deny("Python解释器候选只能查询当前工作区。")
            if requested_root != project_root:
                return _deny("Python解释器候选只能查询当前工作区。")
        return _pre_tool_allow(control, binding)
    if _tool_kind(tool_name, tool_input) == "question":
        error = _validate_python_selection_question(tool_input)
        return _deny(error) if error else _pre_tool_allow(control, binding)
    if _tool_kind(tool_name, tool_input) == "terminal":
        error, executable = _validate_python_selection_command(
            project_root,
            tool_input,
        )
        if error:
            return _deny(error)
        selected = {
            **binding,
            "python_executable": str(executable),
            "python_selection_status": "selected",
            "python_selected_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            _write_binding(project_root, session_id, selected)
        except OSError as error:
            return _deny(
                "无法保存Recorder Generation Python选择："
                f"{type(error).__name__}。"
            )
        return _pre_tool_allow(control, selected)
    return _deny(
        "请先选择并验证本次Recorder Generation使用的Python解释器。"
    )


def _validate_python_selection_question(tool_input):
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or len(questions) != 1:
        return "Python解释器选择必须是一个单选问题。"
    question = questions[0] if isinstance(questions[0], dict) else {}
    question_text = " ".join((
        str(question.get("header") or ""),
        str(question.get("question") or ""),
    )).casefold()
    if "python" not in question_text:
        return "当前只允许询问Python解释器选择。"
    if question.get("multiSelect") is True:
        return "Python解释器只能选择一个。"
    if question.get("allowFreeformInput") is not True:
        return "Python解释器选择必须保留绝对路径输入入口。"
    options = question.get("options")
    if not isinstance(options, list) or not options:
        return "Python解释器选择必须展示至少一个已发现候选。"
    return None


def _validate_python_selection_command(project_root, tool_input):
    command = _terminal_command(tool_input)
    if not command or re.search(r"[;|`<>]", command):
        return "Python解释器验证命令格式无效。", None
    try:
        tokens = [_strip_quotes(item) for item in shlex.split(
            command,
            posix=False,
        )]
    except ValueError:
        return "Python解释器验证命令格式无效。", None
    if tokens[:1] == ["&"]:
        tokens = tokens[1:]
    if len(tokens) != 5 or tokens[1:] != [
        "-B",
        "-m",
        _GENERATION_MODULE,
        "design-contract",
    ]:
        return "选择Python后必须只执行完整generation_workflow design-contract。", None
    executable = Path(tokens[0])
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or executable.name.casefold() not in {
            "python.exe",
            "python3.exe",
            "python",
            "python3",
        }
    ):
        return "请选择存在的Python绝对路径。", None
    try:
        probe = subprocess.run(
            [
                str(executable.resolve()),
                "-B",
                "-m",
                _GENERATION_MODULE,
                "design-contract",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "所选Python无法验证完整generation_workflow。", None
    if probe.returncode != 0:
        return "所选Python无法加载完整generation_workflow。", None
    return None, executable.resolve()


def _route_stop(control, payload):
    if _payload_bool(payload, "stop_hook_active", "stopHookActive"):
        return _allow()
    phase = _job_phase(control)
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "decision": "block",
            "reason": (
                "Recorder 全场景生成尚未结束；请继续完成同一个 Job。"
                f" 当前阶段：{phase or 'unknown'}。"
            ),
        },
    }


def _validate_terminal_command(project_root, binding, control, tool_input):
    command = _terminal_command(tool_input)
    if not command:
        return "Recorder Generation 终端调用缺少命令。"
    if re.search(r"[;|`<>]", command):
        return "Recorder Generation 不允许链式、重定向或 Shell 拼接命令。"
    try:
        tokens = [_strip_quotes(item) for item in shlex.split(
            command,
            posix=False,
        )]
    except ValueError:
        return "Recorder Generation 终端命令格式无效。"
    if tokens[:1] == ["&"]:
        tokens = tokens[1:]
    if any("&" in token for token in tokens):
        return "Recorder Generation 不允许链式、重定向或 Shell 拼接命令。"
    if not tokens:
        return "Recorder Generation 终端命令为空。"
    executable = Path(tokens[0]).name.casefold()
    if executable not in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        return "Recorder Generation 只允许 Python generation_workflow 命令。"
    try:
        actual_executable = Path(tokens[0]).resolve()
        expected_executable = Path(binding["python_executable"]).resolve()
    except (KeyError, OSError, ValueError):
        return "Recorder Generation 无法验证绑定的 Python 解释器。"
    if not Path(tokens[0]).is_absolute() or actual_executable != expected_executable:
        return "Recorder Generation 必须使用会话绑定的绝对 Python 路径。"
    try:
        module_index = tokens.index("-m")
    except ValueError:
        return (
            "Recorder Generation 命令必须以 python -m "
            f"{_GENERATION_MODULE} 执行。"
        )
    if module_index + 2 >= len(tokens) or tokens[module_index + 1] != _GENERATION_MODULE:
        return "Recorder Generation 命令必须调用 generation_workflow。"
    prefix = tokens[1:module_index]
    if any(value not in {"-B", "-3.11"} for value in prefix):
        return "Recorder Generation 命令包含不允许的 Python 参数。"
    subcommand = tokens[module_index + 2]
    arguments = tokens[module_index + 3:]
    if subcommand not in _JOB_COMMANDS | _REPORT_COMMANDS | {"design-contract"}:
        return f"Recorder Generation 不允许命令：{subcommand or 'unknown'}。"
    if subcommand == "job-implementation-candidate" and "--all" in arguments:
        return (
            "Recorder Generation 禁止 --all 大候选输出；"
            "请使用返回的紧凑 candidate index，优先使用content，"
            "缺失时读取candidate_manifest中的source_workspace_path。"
        )
    business_question_error = _validate_business_question_terminal_command(
        control,
        subcommand,
        arguments,
    )
    if business_question_error:
        return business_question_error
    if _typed_patch_design_required(control) and not any((
        subcommand == "advance-job",
        subcommand == "job-design-context"
        and _option_value(arguments, "--step-id"),
    )):
        return (
            "Recorder Generation 当前已有有界 typed patch；只允许直接执行"
            " advance-job，或运行携带 --step-id 的有界 job-design-context。"
        )
    phase = _job_phase(control)
    if subcommand != "design-contract" and subcommand not in (
            _COMMANDS_BY_PHASE.get(phase) or set()
    ):
        return (
            "Recorder Generation 当前阶段不允许命令："
            f"{subcommand}。"
        )
    if subcommand == "design-contract":
        return None if not arguments else "design-contract 不能携带额外参数。"
    positional = _first_positional_argument(arguments)
    if not positional:
        return f"Recorder Generation 命令缺少目标：{subcommand}。"
    if subcommand in _JOB_COMMANDS:
        expected = _binding_job_path(project_root, binding)
        actual = _resolve_project_path(project_root, positional)
        if actual != expected:
            return "Recorder Generation 命令引用了当前会话以外的 Job。"
    if subcommand in _REPORT_COMMANDS:
        expected, error = _active_report_path(project_root, binding, control)
        if error:
            return error
        actual = _resolve_project_path(project_root, positional)
        if actual != expected and not (
                subcommand in _JOB_RESULT_REPORT_COMMANDS
                and _matches_bound_job_result_path(project_root, binding, actual)
        ):
            return "Recorder Generation 命令引用了当前 Job 以外的事务报告。"
        if subcommand == "job-implementation-packet":
            packet_error = _validate_packet_query_path(
                project_root,
                expected,
                _option_value(arguments, "--path"),
            )
            if packet_error:
                return packet_error
    project_root_value = _option_value(arguments, "--project-root")
    if project_root_value and _resolve_project_path(project_root, project_root_value) != project_root:
        return "Recorder Generation 的 --project-root 必须是当前工作区。"
    if subcommand in {"generate-job", "advance-job"}:
        if _option_value(arguments, "--design-json") is not None:
            return "Recorder Generation 完整 Design 产品入口已退役。"
        design_path = _option_value(arguments, "--design-file")
        if design_path is not None:
            return "Recorder Generation 完整 Design 产品入口已退役。"
        naming_patch_path = _option_value(arguments, "--naming-patch-file")
        if naming_patch_path is not None:
            return (
                "Recorder Generation 产品路径不允许提交 NamingPatch 文件；"
                "请使用 --target-name/--business-name 直接提交最小命名输入。"
            )
    return None


def _validate_question_tool(control, tool_input):
    if _business_answers_required(control):
        return _validate_business_answer_questions(control, tool_input)
    return (
        "Recorder Generation 只允许在当前Job投影了business_questions时"
        "使用 vscode_askQuestions；review和技术阶段禁止提问。"
    )


def _validate_business_answer_questions(control, tool_input):
    expected_questions = [
        question for question in (control or {}).get("business_questions") or ()
        if isinstance(question, dict) and question.get("question_id")
    ]
    expected = [str(question.get("question_id") or "") for question in expected_questions]
    expected_by_id = {
        str(question.get("question_id") or ""): question
        for question in expected_questions
    }
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or len(questions) != len(expected):
        return "业务答案必须在一次 vscode_askQuestions 调用中提交全部问题。"
    seen = []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            return f"业务答案问题 {index} 必须是object。"
        if question.get("multiSelect") is True:
            return f"业务答案问题 {index} 不能多选。"
        options = question.get("options")
        if not isinstance(options, list) or not options:
            return f"业务答案问题 {index} 必须展示可点击选项。"
        question_id = str(
            question.get("id")
            or question.get("question_id")
            or question.get("header")
            or ""
        )
        seen.append(question_id)
        expected_question = expected_by_id.get(question_id) or {}
        expected_freeform = _business_question_allows_freeform(expected_question)
        if bool(question.get("allowFreeformInput")) != expected_freeform:
            return (
                f"业务答案问题 {index} 自由输入开关必须与当前Job问题一致。"
            )
        for option in options:
            if not isinstance(option, dict) or not str(option.get("label") or ""):
                return f"业务答案问题 {index} 选项必须有业务文本。"
    missing = sorted(set(expected) - set(seen))
    if missing:
        return f"业务答案问题缺少当前Job问题: {missing}"
    return None


def _business_question_allows_freeform(question):
    if not isinstance(question, dict) or question.get("allow_freeform") is not True:
        return False
    first_option = next((
        option for option in question.get("options") or ()
        if isinstance(option, dict)
    ), {})
    fact = first_option.get("business_fact") or {}
    applies_to = fact.get("applies_to") or {}
    return bool(
        fact.get("fact_type") == "value_authority"
        and applies_to.get("scope") == "action_value"
        and applies_to.get("action_id")
    )


def _validate_read_tool(
        project_root,
        session_id,
        binding,
        control,
        tool_input,
    ):
    paths = []
    _collect_tool_paths(tool_input, paths)
    if paths and any(
            _is_current_session_resource_path(value, session_id)
            for value in paths
    ):
        return (
            "Recorder Generation禁止读取chat-session-resources或外部终端输出；"
            "当前命令必须返回紧凑产品契约。"
        )
    for value in paths:
        try:
            _resolve_project_path(project_root, value)
        except ValueError:
            return "Recorder Generation禁止读取当前工作区外文件。"
    if _job_phase(control) == "implementation":
        index_paths = [
            value for value in paths
            if _is_current_candidate_index_path(
                project_root,
                binding,
                control,
                value,
            )
        ]
        if index_paths:
            if len(index_paths) != len(paths):
                return (
                    "Recorder Generation候选索引读取不能混合其他路径。"
                )
            return _validate_candidate_index_line_window(
                project_root,
                binding,
                control,
                index_paths[0],
                tool_input,
            )
        if paths and all(
                _is_current_candidate_source_path(
                    project_root,
                    binding,
                    control,
                    value,
                )
                for value in paths
        ):
            return None
        return (
            "Recorder Generation实现阶段只允许读取当前事务的"
            "candidate-index.jsonl或native-edit-sources；禁止源码搜索和结果文件解析。"
        )
    if _business_questions_required(control):
        return (
            "Recorder Generation业务问题阶段禁止读取或搜索源码、规则、"
            "记忆和工具输出；请使用当前返回的 BusinessReview/Answers 命令。"
        )
    if not _typed_patch_design_required(control):
        return None
    return (
        "Recorder Generation 当前已有有界 typed patch；禁止读取或搜索项目"
        "源码、规则和记忆。请使用返回的直接参数或唯一有界查询。"
    )


def _is_current_candidate_source_path(
        project_root,
        binding,
        control,
        value,
    ):
    report_path, error = _active_report_path(
        project_root,
        binding,
        control,
    )
    if error:
        return False
    try:
        report = _read_json_object(report_path)
        fingerprint = str(
            (((report.get("system_materialization") or {}).get(
                "candidate"
            ) or {}).get("candidate_fingerprint"))
            or ""
        )
        path = _resolve_project_path(project_root, value)
        expected_root = (
            report_path.parent / "native-edit-sources" / fingerprint
        ).resolve()
        path.relative_to(expected_root)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(fingerprint and path.is_file())


def _is_current_candidate_index_path(
        project_root,
        binding,
        control,
        value,
    ):
    report_path, error = _active_report_path(
        project_root,
        binding,
        control,
    )
    if error:
        return False
    try:
        path = _resolve_project_path(project_root, value)
    except ValueError:
        return False
    expected = (report_path.parent / "candidate-index.jsonl").resolve()
    return path == expected and path.is_file()


def _validate_candidate_index_line_window(
        project_root,
        binding,
        control,
        value,
        tool_input,
    ):
    try:
        path = _resolve_project_path(project_root, value)
    except ValueError:
        return "Recorder Generation候选索引路径无效。"
    if not _is_current_candidate_index_path(project_root, binding, control, value):
        return "Recorder Generation候选索引不属于当前事务。"
    start_line = _tool_int(tool_input, "startLine", "start_line")
    end_line = _tool_int(tool_input, "endLine", "end_line")
    if start_line is None or end_line is None:
        return "Recorder Generation读取candidate-index.jsonl必须携带行范围。"
    if start_line < 1 or end_line < start_line:
        return "Recorder Generation候选索引行范围无效。"
    if end_line - start_line + 1 > CANDIDATE_INDEX_MAX_LINES_PER_READ:
        return "Recorder Generation单次候选索引读取超过行数上限。"
    observed_lines = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for observed_lines, _line in enumerate(handle, start=1):
                if observed_lines >= end_line:
                    break
    except (OSError, UnicodeError):
        return "Recorder Generation无法读取候选索引。"
    if observed_lines < end_line:
        return "Recorder Generation候选索引行范围越界。"
    return None


def _tool_int(tool_input, *names):
    for name in names:
        value = tool_input.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return None


def _validate_business_question_terminal_command(control, subcommand, arguments):
    if not _business_questions_required(control):
        return None
    if subcommand == "advance-job":
        if len(arguments) != 1:
            return "业务问题阶段只允许无额外参数的当前 advance-job。"
        return None
    if subcommand == "submit-business-review-answers":
        return None
    return (
        "业务问题阶段禁止终端解析或查询 Job 输出；请使用当前 Job 返回的 "
        "BusinessReview/Answers 命令一次性提交。"
    )


def _business_questions_required(control):
    return bool(
        control
        and str((control or {}).get("status") or "") == "running"
        and _job_phase(control) == "design"
        and (
            control.get("business_questions")
            or control.get("next_action") == "answer_generation_decision_batch"
        )
    )


def _business_answers_required(control):
    return bool(
        control
        and str((control or {}).get("status") or "") == "running"
        and _job_phase(control) == "design"
        and control.get("business_questions")
    )


def _typed_patch_design_required(control):
    execution = (control or {}).get("job_execution") or {}
    marker = execution.get("design_required") or {}
    return bool(
        _job_phase(control) == "design"
        and marker.get("status") == "required"
        and marker.get("reason") in _TYPED_PATCH_DESIGN_REQUIRED_REASONS
    )


def _is_current_session_resource_path(value, session_id):
    normalized = str(value or "").replace("\\", "/").casefold()
    marker = (
        "/github.copilot-chat/chat-session-resources/"
        + str(session_id or "").casefold()
        + "/"
    )
    return bool(session_id and marker in normalized)


def _validate_packet_query_path(project_root, report_path, value):
    if value is None:
        return None
    try:
        requested = _resolve_project_path(project_root, value)
        report = _read_json_object(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return "Recorder Generation 无法验证 Implementation Packet 查询范围。"
    manifest = report.get("implementation_manifest") or {}
    allowed = set()
    for path in manifest.get("ai_editable_changes") or ():
        if not isinstance(path, str) or not path:
            continue
        try:
            allowed.add(_resolve_project_path(project_root, path))
        except ValueError:
            return "Recorder Generation 的 AI 编辑范围无效。"
    if requested not in allowed:
        return (
            "Implementation Packet 仅允许读取冻结 Manifest 的 "
            "ai_editable_changes；系统候选请使用 job-implementation-candidate。"
        )
    return None


def _validate_write_paths(project_root, binding, control, tool_name, tool_input):
    paths = _tool_write_paths(tool_name, tool_input)
    if not paths:
        return "Recorder Generation 无法确认编辑文件，已拒绝写入。"
    allowed, error = _allowed_write_paths(project_root, binding, control)
    if error:
        return error
    candidate_operations, error = _candidate_write_operations(
        project_root,
        binding,
        control,
    )
    if error:
        return error
    tool_operation = _editor_tool_operation(tool_name)
    for value in paths:
        try:
            resolved = _resolve_project_path(project_root, value)
        except ValueError:
            return "Recorder Generation 拒绝项目目录外的文件编辑。"
        if resolved not in allowed:
            return "Recorder Generation 拒绝冻结范围外的文件编辑。"
        required_operation = candidate_operations.get(resolved)
        if required_operation == "create_file" and tool_operation != "create_file":
            return (
                "Recorder Generation 新目标必须使用 host create-file 工具；"
                "禁止用 apply-patch/edit 插入不存在的文件，以免旧编辑器文本重复。"
            )
        if required_operation == "replace_file" and tool_operation == "create_file":
            return "Recorder Generation 已有目标必须使用整文件替换，不能重新创建。"
    return None


def _candidate_write_operations(project_root, binding, control):
    if _job_phase(control) != "implementation":
        return {}, None
    report_path, error = _active_report_path(project_root, binding, control)
    if error:
        return {}, error
    try:
        report = _read_json_object(report_path)
        pointer = (report.get("system_materialization") or {}).get(
            "candidate"
        ) or {}
        if not pointer:
            return {}, None
        session_dir = _binding_session_dir(project_root, binding)
        candidate = _load_implementation_candidate(
            session_dir,
            pointer,
            transaction_id=report.get("transaction_id"),
        )
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        return {}, (
            "Recorder Generation 无法验证当前候选文件操作："
            f"{type(error).__name__}。"
        )
    operations = {}
    for item in candidate.get("files") or ():
        if not isinstance(item, dict):
            continue
        try:
            target = _resolve_project_path(project_root, item.get("path"))
        except ValueError:
            return {}, "Recorder Generation 候选包含项目目录外目标。"
        operations[target] = (
            "replace_file" if item.get("before_exists") else "create_file"
        )
    return operations, None


def _load_implementation_candidate(session_dir, pointer, *, transaction_id):
    from autowork_core.utils.debug_tools.recorder.implementation_materializer import (
        load_implementation_scaffold_candidate,
    )

    return load_implementation_scaffold_candidate(
        session_dir,
        pointer,
        transaction_id=transaction_id,
    )


def _editor_tool_operation(tool_name):
    normalized = re.sub(r"[^a-z0-9]", "", str(tool_name).casefold())
    return "create_file" if "createfile" in normalized else "replace_file"


def _allowed_write_paths(project_root, binding, control):
    phase = _job_phase(control)
    if phase == "design":
        return set(), (
            "Recorder Generation 当前Design阶段不允许编辑草稿文件；"
            "业务问题必须通过 Job-bound BusinessReview/Answers 命令提交，"
            "typed patch 必须通过 advance-job 直接参数提交。"
        )
    if phase != "implementation":
        return set(), "Recorder Generation 当前阶段不允许编辑文件。"
    report_path, error = _active_report_path(project_root, binding, control)
    if error:
        return set(), error
    try:
        report = _read_json_object(report_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return set(), (
            "Recorder Generation 无法读取当前事务范围："
            f"{type(error).__name__}。"
        )
    lease = report.get("generation_job_lease") or {}
    if any((
            report.get("request_id") != binding.get("request_id"),
            lease.get("job_id") != binding.get("job_id"),
            lease.get("job_fingerprint") != binding.get("job_fingerprint"),
    )):
        return set(), "Recorder Generation 当前事务与会话绑定不一致。"
    manifest = report.get("implementation_manifest") or {}
    allowed_changes = manifest.get("allowed_changes")
    if not isinstance(allowed_changes, list) or not allowed_changes:
        return set(), "Recorder Generation 当前事务缺少可编辑文件范围。"
    allowed = set()
    for value in allowed_changes:
        if not isinstance(value, str) or not value:
            return set(), "Recorder Generation 当前事务文件范围无效。"
        try:
            allowed.add(_resolve_project_path(project_root, value))
        except ValueError:
            return set(), "Recorder Generation 当前事务包含项目目录外文件。"
    return allowed, None


def _active_report_path(project_root, binding, control):
    transaction = (control.get("job_execution") or {}).get(
        "transaction"
    ) or {}
    transaction_id = str(transaction.get("transaction_id") or "")
    path_value = str(transaction.get("path") or "")
    if not transaction_id or not path_value:
        return None, "Recorder Generation 当前没有可用事务报告。"
    session_dir = _binding_session_dir(project_root, binding)
    try:
        report_path = _resolve_relative_path(session_dir, path_value)
    except ValueError:
        return None, "Recorder Generation 事务报告路径无效。"
    expected = (
        session_dir
        / "ai"
        / "generation-transactions"
        / transaction_id
        / "report.json"
    ).resolve()
    if report_path != expected or not report_path.is_file():
        return None, "Recorder Generation 当前事务报告不存在或不匹配。"
    return report_path, None


def _matches_bound_job_result_path(project_root, binding, path):
    path = Path(path).resolve()
    session_dir = _binding_session_dir(project_root, binding)
    job_id = str(binding.get("job_id") or "")
    if not job_id:
        return False
    expected_root = (
        session_dir / "ai" / "generation-job-results" / job_id
    ).resolve()
    try:
        path.relative_to(expected_root)
    except ValueError:
        return False
    return path.is_file() and path.name.startswith("result-") and path.suffix == ".json"


def _tool_kind(tool_name, tool_input):
    normalized = re.sub(r"[^a-z0-9]", "", str(tool_name).casefold())
    if any(marker in normalized for marker in _QUESTION_TOOL_MARKERS):
        return "question"
    if any(marker in normalized for marker in _TERMINAL_TOOL_MARKERS):
        return "terminal"
    if "command" in tool_input and isinstance(tool_input.get("command"), str):
        return "terminal"
    if any(marker in normalized for marker in _WRITE_TOOL_MARKERS):
        return "write"
    if any(marker in normalized for marker in _READ_TOOL_MARKERS):
        return "read"
    return "unknown"


def _tool_write_paths(tool_name, tool_input):
    paths = []
    _collect_tool_paths(tool_input, paths)
    normalized = re.sub(r"[^a-z0-9]", "", str(tool_name).casefold())
    if "patch" in normalized:
        for value in (tool_input.get("input"), tool_input.get("patch")):
            if not isinstance(value, str):
                continue
            paths.extend(
                match.group("path").strip()
                for match in _PATCH_PATH_PATTERN.finditer(value)
            )
    return list(dict.fromkeys(path for path in paths if path))


def _collect_tool_paths(value, paths, *, field_name=None):
    if isinstance(value, dict):
        for key, item in value.items():
            _collect_tool_paths(item, paths, field_name=str(key).casefold())
        return
    if isinstance(value, list):
        for item in value:
            _collect_tool_paths(item, paths, field_name=field_name)
        return
    if not isinstance(value, str):
        return
    if field_name in _PATH_FIELDS or field_name in _PATH_LIST_FIELDS:
        paths.append(value)


def _terminal_command(tool_input):
    for key in ("command", "terminal_command", "terminalCommand"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _first_positional_argument(arguments):
    for value in arguments:
        if not value.startswith("-"):
            return value
    return None


def _option_value(arguments, option):
    for index, value in enumerate(arguments):
        if value == option:
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if value.startswith(option + "="):
            return value.split("=", 1)[1]
    return None


def _bound_job_control(project_root, session_id, binding):
    try:
        _validate_binding(project_root, session_id, binding)
        job_path = _binding_job_path(project_root, binding)
    except ValueError as error:
        return None, f"Recorder Generation会话绑定无效：{error}"
    control, error = _inspect_bound_job(job_path)
    if error:
        return None, error
    if any((
            control.get("job_id") != binding.get("job_id"),
            control.get("job_fingerprint") != binding.get("job_fingerprint"),
            control.get("request_id") != binding.get("request_id"),
    )):
        return None, "Recorder Generation Job 与会话绑定不一致。"
    if not _is_terminal(control) and str(control.get("status") or "") not in _ACTIVE_STATUSES:
        return None, "Recorder Generation Job 当前不处于可继续阶段。"
    return control, None


def _inspect_bound_job(job_path):
    try:
        control = _inspect_generation_job_control(job_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, (
            "Recorder Generation Job 无法验证："
            f"{type(error).__name__}。"
        )
    if not isinstance(control, dict) or not all((
            control.get("job_id"),
            control.get("job_fingerprint"),
            control.get("request_id"),
            control.get("job_path"),
    )):
        return None, "Recorder Generation Job 控制信息无效。"
    return control, None


def _inspect_generation_job_control(job_path):
    """Read only the signed Job/Workflow control facts needed by the host."""
    job_path = Path(job_path).resolve()
    session_dir = _session_dir_for_job(job_path)
    job = _read_json_object(job_path)
    _validate_job_identity(job_path, session_dir, job)
    request = job.get("request") or {}
    request_id = str(request.get("request_id") or "")
    workflow_path = session_dir / "ai" / "workflow" / f"{request_id}.json"
    workflow = _read_json_object(workflow_path)
    if str(workflow.get("request_id") or "") != request_id:
        raise ValueError("Workflow Request 不匹配")
    current = workflow.get("current_job")
    execution = workflow.get("job_execution") or {}
    if _job_pointer_matches(current, job, session_dir, job_path):
        status = str(workflow.get("status") or "")
        phase = str(execution.get("phase") or "")
        if status == "ready" and phase != "ready":
            raise ValueError("ready Job 阶段无效")
        if status == "running" and phase not in {
                "design", "implementation", "runtime", "oracle"
        }:
            raise ValueError("running Job 阶段无效")
        if status not in _ACTIVE_STATUSES:
            raise ValueError("当前 Job 状态无效")
        return _control_projection(job_path, job, workflow, execution)
    for retired in reversed(list(workflow.get("retired_jobs") or ())):
        if not isinstance(retired, dict) or not _job_pointer_matches(
                retired.get("job"),
                job,
                session_dir,
                job_path,
        ):
            continue
        status = str(retired.get("status") or "")
        retired_execution = retired.get("job_execution") or {}
        if status not in _TERMINAL_STATUSES:
            raise ValueError("已归档 Job 状态无效")
        return _control_projection(
                job_path,
                job,
                {
                    "status": status,
                    "next_action": retired.get("next_action"),
                },
                retired_execution,
        )
    raise ValueError("Job 不是当前 Workflow Job")


def _validate_job_identity(job_path, session_dir, job):
    if not isinstance(job, dict):
        raise ValueError("Job 内容无效")
    request = job.get("request") or {}
    request_id = str(request.get("request_id") or "")
    fingerprint = str(job.get("job_fingerprint") or "")
    if any((
            not request_id,
            "/" in request_id,
            "\\" in request_id,
            len(fingerprint) != 64,
            any(character not in "0123456789abcdef" for character in fingerprint),
            str(job.get("job_id") or "") != "job-" + fingerprint[:16],
            str(job.get("activation") or "") != "active",
            not str(job.get("nonce") or ""),
            not str(request.get("request_fingerprint") or ""),
            not str(request.get("revision_seal") or ""),
            fingerprint != _job_fingerprint(job),
    )):
        raise ValueError("Job 身份无效")
    expected = (
        session_dir
        / "ai"
        / "generation-jobs"
        / request_id
        / f"job-{fingerprint}.json"
    ).resolve()
    if job_path != expected:
        raise ValueError("Job 路径与身份不匹配")


def _job_pointer_matches(pointer, job, session_dir, job_path):
    if not isinstance(pointer, dict):
        return False
    request = job.get("request") or {}
    profile = job.get("profile_lease") or {}
    expected_path = job_path.relative_to(session_dir).as_posix()
    return all((
        str(pointer.get("path") or "").replace("\\", "/")
        == expected_path,
        pointer.get("job_id") == job.get("job_id"),
        pointer.get("job_fingerprint") == job.get("job_fingerprint"),
        pointer.get("nonce") == job.get("nonce"),
        pointer.get("request_id") == request.get("request_id"),
        pointer.get("profile_lease_fingerprint")
        == profile.get("profile_fingerprint"),
        pointer.get("activation") == job.get("activation"),
    ))


def _control_projection(job_path, job, workflow, execution):
    execution = execution if isinstance(execution, dict) else {}
    status = str(workflow.get("status") or "")
    return {
        "status": status,
        "next_action": workflow.get("next_action"),
        "request_id": (job.get("request") or {}).get("request_id"),
        "job_id": job.get("job_id"),
        "job_path": str(job_path),
        "job_fingerprint": job.get("job_fingerprint"),
        "job_execution": {
            "phase": execution.get("phase"),
            "epoch": execution.get("epoch"),
            "claim_id": execution.get("claim_id"),
            "attempt_no": execution.get("attempt_no"),
            "transaction": execution.get("transaction") or {},
            "design_required": dict(execution.get("design_required") or {}),
        },
        "job_transition": {
            "phase": execution.get("phase"),
            "epoch": execution.get("epoch"),
            "claim_id": execution.get("claim_id"),
            "attempt_no": execution.get("attempt_no"),
            "next_action": workflow.get("next_action"),
        },
        "business_answers_submitted": bool(
            ((workflow.get("decision") or {}).get("answers") or {}).get(
                "path"
            )
        ),
    }


def _job_fingerprint(job):
    return _canonical_fingerprint({
        key: value
        for key, value in dict(job or {}).items()
        if key not in {"job_id", "job_fingerprint", "job_path"}
    })


def _canonical_fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _new_binding(project_root, session_id, job_path, control):
    session_dir = _session_dir_for_job(job_path)
    return {
        "binding_version": BINDING_VERSION,
        "router_version": ROUTER_VERSION,
        "session_sha256": _session_digest(session_id),
        "project_root": str(project_root),
        "session_path": session_dir.relative_to(project_root).as_posix(),
        "job_path": job_path.relative_to(project_root).as_posix(),
        "job_id": str(control["job_id"]),
        "job_fingerprint": str(control["job_fingerprint"]),
        "request_id": str(control["request_id"]),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_selection_status": "pending",
        "python_selected_at": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def _validate_binding(project_root, session_id, binding):
    expected_fields = {
        "binding_version",
        "router_version",
        "session_sha256",
        "project_root",
        "session_path",
        "job_path",
        "job_id",
        "job_fingerprint",
        "request_id",
        "python_executable",
        "python_selection_status",
        "python_selected_at",
        "created_at",
    }
    if not isinstance(binding, dict) or set(binding) != expected_fields:
        raise ValueError("字段不完整")
    if binding.get("binding_version") != BINDING_VERSION:
        raise ValueError("版本不支持")
    if binding.get("session_sha256") != _session_digest(session_id):
        raise ValueError("会话身份不匹配")
    if Path(str(binding.get("project_root") or "")).resolve() != project_root:
        raise ValueError("项目根目录不匹配")
    if binding.get("python_selection_status") not in {"pending", "selected"}:
        raise ValueError("Python选择状态无效")
    if binding.get("python_selection_status") == "selected" and not (
            binding.get("python_selected_at")
    ):
        raise ValueError("Python选择时间缺失")
    python_executable = Path(str(binding.get("python_executable") or ""))
    if not python_executable.is_absolute() or not python_executable.is_file():
        raise ValueError("Python解释器绑定无效")
    job_path = _binding_job_path(project_root, binding)
    session_dir = _binding_session_dir(project_root, binding)
    if _session_dir_for_job(job_path) != session_dir:
        raise ValueError("Job 与会话目录不匹配")


def _load_binding(project_root, session_id):
    path = _binding_path(project_root, session_id)
    if not path.exists():
        return None, None
    if not path.is_file() or path.is_symlink():
        return None, "Recorder Generation会话绑定不是普通文件。"
    try:
        return _read_json_object(path), None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, "Recorder Generation会话绑定无法读取。"


def _write_binding(project_root, session_id, binding):
    path = _binding_path(project_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() != (project_root / _ROUTING_DIRECTORY).resolve():
        raise OSError("Recorder Generation会话绑定目录越界")
    _write_json_atomic(path, binding)


def _write_json_atomic(path, value):
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tmp-recorder-routing-",
        suffix=".json",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _clear_binding(project_root, session_id):
    _binding_path(project_root, session_id).unlink(missing_ok=True)


def _binding_path(project_root, session_id):
    return (
        project_root
        / _ROUTING_DIRECTORY
        / f"session-{_session_digest(session_id)[:24]}.json"
    )


def _binding_job_path(project_root, binding):
    return _resolve_project_path(project_root, binding.get("job_path"))


def _binding_session_dir(project_root, binding):
    return _resolve_project_path(project_root, binding.get("session_path"))


def _session_dir_for_job(job_path):
    job_path = Path(job_path).resolve()
    if len(job_path.parents) < 4 or job_path.parent.parent.parent.name != "ai":
        raise ValueError("Job 路径无效")
    return job_path.parents[3]


def _prompt_job_path(project_root, prompt):
    prompt = str(prompt or "")
    match = _PROMPT_PATTERN.fullmatch(prompt)
    candidates = []
    if match is not None:
        candidates.append(match.group("path"))
    elif (
            _GENERATION_MODULE in prompt
            and re.search(r"(?:^|\s)advance-job(?:\s|$)", prompt)
    ):
        candidates.extend(
            item.group("path")
            for item in _PROMPT_COMMAND_JOB_PATTERN.finditer(prompt)
        )
    paths = []
    for candidate in candidates:
        try:
            path = _resolve_project_path(
                project_root,
                _strip_quotes(candidate),
            )
        except ValueError:
            continue
        if path.name.startswith("job-") and path.suffix == ".json":
            paths.append(path)
    unique = list(dict.fromkeys(paths))
    return unique[0] if len(unique) == 1 else None


def _design_draft_path(project_root, binding):
    job_id = str(binding.get("job_id") or "")
    if not job_id:
        raise ValueError("Job 身份缺失")
    return (
        project_root / ".copilot" / "recorder-drafts" / job_id / "design.json"
    ).resolve()


def _naming_patch_draft_path(project_root, binding):
    job_id = str(binding.get("job_id") or "")
    if not job_id:
        raise ValueError("Job 身份缺失")
    return (
        project_root
        / ".copilot"
        / "recorder-drafts"
        / job_id
        / "naming-patch.json"
    ).resolve()


def _resolve_project_path(project_root, value):
    path = Path(str(value or ""))
    if not str(value or ""):
        raise ValueError("路径为空")
    resolved = path.resolve() if path.is_absolute() else (project_root / path).resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as error:
        raise ValueError("路径越出项目目录") from error
    return resolved


def _resolve_relative_path(root, value):
    path = Path(str(value or ""))
    if not str(value or "") or path.is_absolute() or ".." in path.parts:
        raise ValueError("相对路径无效")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("路径越界") from error
    return resolved


def _read_json_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON 必须是 object")
    return value


def _normalize_event(event):
    normalized = re.sub(r"[^a-z]", "", str(event or "").casefold())
    try:
        return _EVENT_NAMES[normalized]
    except KeyError as error:
        raise ValueError(f"不支持的 Hook 事件：{event}") from error


def _session_id(payload):
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _session_digest(session_id):
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()


def _job_phase(control):
    return str((control.get("job_execution") or {}).get("phase") or "")


def _is_terminal(control):
    return str((control or {}).get("status") or "") in _TERMINAL_STATUSES


def _payload_bool(payload, *names):
    return any(payload.get(name) is True for name in names)


def _strip_quotes(value):
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _allow(message=None):
    result = {"continue": True}
    if message:
        result["systemMessage"] = message
    return result


def _stop(reason):
    return {
        "continue": False,
        "stopReason": str(reason),
    }


def _deny(reason):
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": str(reason),
        },
    }


def _pre_tool_allow(control, binding):
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _bound_context(
                control,
                binding,
                resumed=True,
            ),
        },
    }


def _post_tool_response(control, binding):
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": _bound_context(
                control,
                binding,
                resumed=True,
            ),
        },
    }


def _record_agent_wait_event(
        project_root,
        session_id,
        binding,
        control,
        event,
        payload,
        *,
        outcome,
    ):
    if not isinstance(binding, dict) or not binding.get("job_id"):
        return
    try:
        path = _agent_wait_ledger_path(project_root, binding)
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = _read_agent_wait_ledger(path, binding)
        events = list(ledger.get("events") or [])
        events.append(_agent_wait_event_item(
            project_root,
            session_id,
            binding,
            control,
            event,
            payload,
            outcome=outcome,
            previous=events[-1] if events else None,
            events=events,
        ))
        ledger["events"] = events
        ledger["summary"] = _agent_wait_summary(events)
        _write_json_atomic(path, ledger)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return


def _agent_wait_ledger_path(project_root, binding):
    job_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(binding.get("job_id") or ""))
    if not job_id:
        raise ValueError("Job identity missing")
    return (
        project_root / _ROUTING_DIRECTORY / f"agent-wait-{job_id}.json"
    ).resolve()


def _read_agent_wait_ledger(path, binding):
    if not path.exists():
        return {
            "agent_wait_ledger_version": AGENT_WAIT_LEDGER_VERSION,
            "job_id": binding.get("job_id"),
            "job_fingerprint": binding.get("job_fingerprint"),
            "request_id": binding.get("request_id"),
            "events": [],
            "summary": {},
        }
    value = _read_json_object(path)
    if value.get("agent_wait_ledger_version") != AGENT_WAIT_LEDGER_VERSION:
        raise ValueError("Agent wait ledger version invalid")
    if value.get("job_id") != binding.get("job_id"):
        raise ValueError("Agent wait ledger job mismatch")
    if not isinstance(value.get("events"), list):
        raise ValueError("Agent wait ledger events invalid")
    return value


def _agent_wait_event_item(
        project_root,
        session_id,
        binding,
        control,
        event,
        payload,
        *,
        outcome,
        previous,
        events,
    ):
    now = datetime.now().isoformat(timespec="milliseconds")
    tool_name = str(
        payload.get("tool_name") or payload.get("toolName") or ""
    )
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = payload.get("toolInput")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    item = {
        "at": now,
        "hook_event_name": event,
        "session_sha256": _session_digest(session_id),
        "job_id": binding.get("job_id"),
        "request_id": binding.get("request_id"),
        "phase": _job_phase(control) or "unknown",
        "next_action": (control.get("job_transition") or {}).get("next_action")
        or control.get("next_action"),
        "tool_use_id": _tool_use_id(payload),
        "tool_name": tool_name or None,
        "tool_kind": _agent_tool_stage(
            project_root,
            binding,
            control,
            tool_name,
            tool_input,
        ),
        "outcome": str(outcome or "unknown"),
    }
    if previous:
        item["since_previous_event_ms"] = _elapsed_ms(previous.get("at"), now)
    if event == "PreToolUse" and previous and previous.get("hook_event_name") != "PreToolUse":
        item["agent_wait_ms"] = _elapsed_ms(previous.get("at"), now)
        wait_segment = _agent_wait_segment(previous, item)
        if wait_segment:
            item["agent_wait_segment"] = wait_segment
    if event == "PostToolUse":
        item["tool_execution_ms"] = _matching_pre_tool_duration_ms(
            events,
            item,
        )
    return item


def _agent_wait_segment(previous, item):
    if (
            previous.get("hook_event_name") == "PostToolUse"
            and previous.get("tool_kind") == "editor_edits"
            and item.get("tool_kind") == "after_native_edit"
    ):
        return "editor_edits_done_to_after_native_edit_request"
    return None


def _tool_use_id(payload):
    for key in ("tool_use_id", "toolUseId", "tool_call_id", "toolCallId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _matching_pre_tool_duration_ms(events, item):
    for event in reversed(events or []):
        if event.get("hook_event_name") != "PreToolUse":
            continue
        if item.get("tool_use_id") and event.get("tool_use_id"):
            if item.get("tool_use_id") != event.get("tool_use_id"):
                continue
        elif item.get("tool_name") != event.get("tool_name"):
            continue
        return _elapsed_ms(event.get("at"), item.get("at"))
    return None


def _elapsed_ms(started_at, finished_at):
    if not started_at or not finished_at:
        return None
    try:
        started = datetime.fromisoformat(str(started_at))
        finished = datetime.fromisoformat(str(finished_at))
    except (TypeError, ValueError):
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _agent_tool_stage(project_root, binding, control, tool_name, tool_input):
    if not tool_name:
        return None
    tool_kind = _tool_kind(tool_name, tool_input)
    if tool_kind == "terminal":
        command = _terminal_command(tool_input)
        if _GENERATION_MODULE in command and "advance-job" in command:
            if _advance_command_has_typed_patch_args(command):
                return "typed_patch_submit"
            if _job_phase(control) == "implementation":
                return "after_native_edit"
            return "advance_job"
        if _GENERATION_MODULE in command:
            return "generation_workflow_command"
        return "terminal"
    if tool_kind == "read":
        paths = []
        _collect_tool_paths(tool_input, paths)
        if paths and all(
            _is_current_candidate_index_path(
                project_root,
                binding,
                control,
                path,
            )
            for path in paths
        ):
            return "candidate_index_read"
        if any(
                "candidate-delivery-manifest.json" in str(path).replace("\\", "/")
                for path in paths
        ):
            return "manifest_read"
        if paths and all(
                _is_current_candidate_source_path(
                    project_root,
                    binding,
                    control,
                    path,
                )
                for path in paths
        ):
            return "source_reads"
        return "read"
    if tool_kind == "write":
        return "editor_edits" if _job_phase(control) == "implementation" else "write"
    if tool_kind == "question":
        return "question"
    return tool_kind


def _advance_command_has_typed_patch_args(command):
    return any(
        marker in str(command or "")
        for marker in (
            "--target-name",
            "--business-name",
            "--ambiguity-choice",
            "--assertion-choice",
            "--method-choice",
            "--operation-choice",
            "--value-source-choice",
        )
    )


def _agent_wait_summary(events):
    events = [event for event in events or [] if isinstance(event, dict)]
    tool_requests = [
        event for event in events
        if event.get("hook_event_name") == "PreToolUse"
    ]
    waits = [
        event for event in tool_requests
        if isinstance(event.get("agent_wait_ms"), int)
    ]
    tool_exec = [
        event for event in events
        if event.get("hook_event_name") == "PostToolUse"
        and isinstance(event.get("tool_execution_ms"), int)
    ]
    by_kind = {}
    for event in tool_requests:
        kind = event.get("tool_kind") or "unknown"
        bucket = by_kind.setdefault(kind, {"request_count": 0, "agent_wait_ms": 0})
        bucket["request_count"] += 1
        if isinstance(event.get("agent_wait_ms"), int):
            bucket["agent_wait_ms"] += event["agent_wait_ms"]
    by_segment = {}
    for event in waits:
        segment = event.get("agent_wait_segment")
        if not segment:
            continue
        bucket = by_segment.setdefault(
            segment,
            {"request_count": 0, "agent_wait_ms": 0},
        )
        bucket["request_count"] += 1
        bucket["agent_wait_ms"] += event["agent_wait_ms"]
    max_wait = max(waits, key=lambda item: item["agent_wait_ms"]) if waits else None
    summary = {
        "event_count": len(events),
        "tool_request_count": len(tool_requests),
        "observed_tool_execution_count": len(tool_exec),
        "total_agent_wait_ms": sum(event["agent_wait_ms"] for event in waits),
        "total_tool_execution_ms": sum(event["tool_execution_ms"] for event in tool_exec),
        "max_agent_wait_ms": max_wait.get("agent_wait_ms") if max_wait else None,
        "max_agent_wait_tool_kind": max_wait.get("tool_kind") if max_wait else None,
        "max_agent_wait_segment": (
            max_wait.get("agent_wait_segment") if max_wait else None
        ),
        "by_tool_kind": by_kind,
    }
    if by_segment:
        summary["by_wait_segment"] = by_segment
        segment = "editor_edits_done_to_after_native_edit_request"
        if segment in by_segment:
            summary[f"{segment}_ms"] = by_segment[segment]["agent_wait_ms"]
    return summary


def _hook_event_outcome(result):
    output = (result or {}).get("hookSpecificOutput") or {}
    decision = output.get("permissionDecision") or output.get("decision")
    if decision:
        return str(decision)
    if result and result.get("continue") is False:
        return "stopped"
    return "allowed"


def _binding_error_response(event, reason):
    if event == "PreToolUse":
        return _deny(reason)
    if event == "Stop":
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "decision": "block",
                "reason": reason,
            },
        }
    if event == "PostToolUse":
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": reason,
            },
        }
    return _stop(reason)


def _terminal_event_response(event, control):
    if event == "PostToolUse":
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "Recorder Generation Job 已结束。",
            },
        }
    return _allow("Recorder Generation Job 已结束。")


def _unbound_event_response(event):
    if event == "Stop":
        return _allow()
    return _allow()


def _bound_context(control, binding, *, resumed):
    action = str((control.get("job_transition") or {}).get("next_action") or "")
    phase = _job_phase(control) or "unknown"
    prefix = "继续" if resumed else "已绑定"
    if binding.get("python_selection_status") == "pending":
        return (
            f"Recorder Generation Job {prefix}：{control.get('job_id')}；"
            f"阶段：{phase}；epoch：{(control.get('job_execution') or {}).get('epoch')}；"
            f"claim：{(control.get('job_execution') or {}).get('claim_id') or '未认领'}；"
            f"推荐Python：{binding.get('python_executable')}；"
            "下一动作：查询当前工作区Python候选，向用户提供单选和绝对路径输入，"
            "再执行完整generation_workflow design-contract验证。"
        )
    return (
        f"Recorder Generation Job {prefix}：{control.get('job_id')}；"
        f"阶段：{phase}；epoch：{(control.get('job_execution') or {}).get('epoch')}；"
        f"claim：{(control.get('job_execution') or {}).get('claim_id') or '未认领'}；"
        f"Python：{binding.get('python_executable')}；"
        f"下一动作：{action or '按 Job 状态执行'}。"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Route bound Recorder Generation host Hook events"
    )
    parser.add_argument("--event", required=True)
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(_read_hook_payload() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Hook input 必须是 JSON object")
        result = route_hook_event(
            args.event,
            payload,
            project_root=args.project_root,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        event = _normalize_event(args.event)
        result = _binding_error_response(
            event,
            "Recorder Generation Hook 无法验证当前操作："
            f"{type(error).__name__}。",
        )
    print(json.dumps(result, ensure_ascii=False), end="")
    return 0


def _read_hook_payload():
    stream = getattr(sys.stdin, "buffer", None)
    return stream.read() if stream is not None else sys.stdin.read()


if __name__ == "__main__":
    raise SystemExit(main())