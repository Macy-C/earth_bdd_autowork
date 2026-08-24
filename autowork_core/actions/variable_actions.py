import re

from autowork_core.actions.element_actions import get_attr, get_text
from autowork_core.common.log_helper import log_call


_SCENARIO_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def save_text(
        context,
        locator,
        variable_name,
        *,
        timeout=3,
        first_only=True,
        overwrite=False,
        allow_empty=False,
    ):
    value = get_text(
        context,
        locator,
        timeout=timeout,
        first_only=first_only,
    )
    return set_variable(
        context,
        variable_name,
        value,
        overwrite=overwrite,
        allow_empty=allow_empty,
    )


def save_attr(
        context,
        locator,
        attr_name,
        variable_name,
        *,
        timeout=3,
        default=None,
        first_only=True,
        overwrite=False,
        allow_empty=False,
    ):
    value = get_attr(
        context,
        locator,
        attr_name,
        timeout=timeout,
        default=default,
        first_only=first_only,
    )
    return set_variable(
        context,
        variable_name,
        value,
        overwrite=overwrite,
        allow_empty=allow_empty,
    )


def set_variable(
        context,
        variable_name,
        value,
        *,
        overwrite=False,
        allow_empty=False,
    ):
    variable_name = _validate_variable_name(variable_name)
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite 必须是 bool")
    if not isinstance(allow_empty, bool):
        raise TypeError("allow_empty 必须是 bool")
    if not allow_empty and (
        value is None or (isinstance(value, str) and value == "")
    ):
        raise ValueError(f"Scenario 变量不允许为空: {variable_name}")

    variables = _scenario_variables(context)
    if variable_name in variables and not overwrite:
        raise KeyError(f"Scenario 变量已存在: {variable_name}")
    log_call(
        variable_name=variable_name,
        overwrite=overwrite,
        allow_empty=allow_empty,
    )
    variables[variable_name] = value
    return value


def get_variable(context, variable_name):
    variable_name = _validate_variable_name(variable_name)
    variables = _scenario_variables(context)
    if variable_name not in variables:
        raise KeyError(f"Scenario 变量不存在: {variable_name}")
    log_call(variable_name=variable_name)
    return variables[variable_name]


def _scenario_variables(context):
    scenario = getattr(context, "autowork_scenario", None)
    if scenario is None:
        raise RuntimeError("Autowork scenario context is not initialized")
    variables = getattr(scenario, "variables", None)
    if not isinstance(variables, dict):
        raise RuntimeError("Autowork scenario variables are not initialized")
    return variables


def _validate_variable_name(variable_name):
    if (
        not isinstance(variable_name, str)
        or _SCENARIO_VARIABLE_NAME.fullmatch(variable_name) is None
    ):
        raise ValueError(
            "Scenario 变量名必须是最多 64 位的 ASCII 标识符: "
            f"{variable_name!r}"
        )
    return variable_name