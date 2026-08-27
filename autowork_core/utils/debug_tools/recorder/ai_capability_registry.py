from __future__ import annotations

from dataclasses import dataclass


AI_CAPABILITY_REGISTRY_VERSION = "1.5"
_UNSET = object()
_AI_EXCLUSION_POLICIES = frozenset({
    "direct_pic",
    "framework_support",
})


@dataclass(frozen=True)
class AICapability:
    name: str
    api_name: str
    category: str
    plan_enabled: bool = False
    debug_only: bool = False
    ai_exclusion: str | None = None
    value_argument: tuple[int, str] | None = None
    table_value_argument: tuple[int, str] | None = None
    requires_value_action: bool = False
    recorded_action_types: frozenset[str] = frozenset()
    plan_validation_profile: str | None = None
    ast_match_profile: str | None = None
    ambiguity_parameters_exact: bool = False
    purpose: str = ""
    use_when: tuple[str, ...] = ()
    avoid_when: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    required_control_types: frozenset[str] = frozenset()


def _capability(
        name,
        category,
        *,
    plan_enabled=_UNSET,
        debug_only=False,
        ai_exclusion=None,
        value_argument=None,
        table_value_argument=None,
        requires_value_action=False,
        recorded_action_types=(),
        plan_validation_profile=None,
        ast_match_profile=None,
        ambiguity_parameters_exact=False,
        purpose="",
        use_when=(),
        avoid_when=(),
        alternatives=(),
        required_control_types=(),
    ):
    if not isinstance(debug_only, bool):
        raise TypeError(f"debug_only必须为bool: {name}")
    if ai_exclusion is not None:
        if not isinstance(ai_exclusion, str):
            raise TypeError(f"ai_exclusion必须为字符串: {name}")
        if ai_exclusion not in _AI_EXCLUSION_POLICIES:
            raise ValueError(
                f"未知ai_exclusion policy: {name}={ai_exclusion}"
            )
    classifications = (
        plan_enabled is not _UNSET,
        bool(debug_only),
        bool(ai_exclusion),
    )
    if sum(classifications) != 1:
        raise ValueError(
            f"AI capability必须显式且唯一分类: {name}"
        )
    if plan_enabled is not _UNSET and not isinstance(plan_enabled, bool):
        raise TypeError(f"plan_enabled必须为bool: {name}")
    return AICapability(
        name=name,
        api_name=name,
        category=category,
        plan_enabled=(
            plan_enabled if plan_enabled is not _UNSET else False
        ),
        debug_only=debug_only,
        ai_exclusion=str(ai_exclusion) if ai_exclusion else None,
        value_argument=value_argument,
        table_value_argument=table_value_argument,
        requires_value_action=requires_value_action,
        recorded_action_types=frozenset(recorded_action_types),
        plan_validation_profile=plan_validation_profile,
        ast_match_profile=ast_match_profile,
        ambiguity_parameters_exact=ambiguity_parameters_exact,
        purpose=str(purpose or ""),
        use_when=tuple(use_when),
        avoid_when=tuple(avoid_when),
        alternatives=tuple(alternatives),
        required_control_types=frozenset(required_control_types),
    )


AI_CAPABILITIES = (
    _capability(
        "click",
        "interaction",
        plan_enabled=True,
        recorded_action_types=("click",),
        plan_validation_profile="frozen_click_offset",
        ast_match_profile="frozen_click_offset",
        purpose="Invoke a direct target without a higher-level final-state intent.",
        use_when=("The business intent is a direct activation or button press.",),
        avoid_when=("A final-state input, selection, toggle, expansion, or range API expresses the intent.",),
        alternatives=("input_text", "select_dropdown_option", "set_checked"),
    ),
    _capability(
        "double_click",
        "interaction",
        plan_enabled=True,
        recorded_action_types=("double_click",),
        purpose="Activate a target with a recorded double-click gesture.",
        use_when=("Double-click itself opens or activates the business target.",),
        avoid_when=("A single activation or final-state API expresses the result.",),
        alternatives=("click",),
    ),
    _capability(
        "right_click",
        "interaction",
        plan_enabled=True,
        recorded_action_types=("right_click",),
        purpose="Open the target's context interaction with a right click.",
        use_when=("The business flow requires a context menu or right-click behavior.",),
        avoid_when=("The intent is ordinary activation.",),
        alternatives=("click",),
    ),
    _capability(
        "focus",
        "interaction",
        plan_enabled=True,
        purpose="Move keyboard focus to a target without asserting a business state change.",
        use_when=("A later keyboard or accessibility operation requires focus.",),
        avoid_when=("Activation, input, or selection already performs the intended action.",),
        alternatives=("click", "input_text", "send_text_keys"),
    ),
    _capability(
        "input_text",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "data_or_name"),
        table_value_argument=(1, "data_or_name"),
        requires_value_action=True,
        recorded_action_types=("input_text", "keyboard"),
        purpose="Set the target's final text value.",
        use_when=("The business intent is free-form or parameterized text input.",),
        avoid_when=("The value must be chosen from a finite option list or the intent is a shortcut key sequence.",),
        alternatives=("send_text_keys", "select_dropdown_option"),
    ),
    _capability(
        "send_text_keys",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "data_or_name"),
        table_value_argument=(1, "data_or_name"),
        requires_value_action=True,
        recorded_action_types=("input_text", "keyboard"),
        purpose="Send a key sequence after focusing the target.",
        use_when=("The business intent is a shortcut, navigation key, or incremental key sequence.",),
        avoid_when=("The business result is a final text value that input_text can set directly.",),
        alternatives=("input_text",),
    ),
    _capability(
        "remove_text",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "data_or_name"),
        table_value_argument=(1, "data_or_name"),
        requires_value_action=True,
        recorded_action_types=("keyboard",),
        purpose="Remove one uniquely occurring business substring and verify the final value.",
        use_when=("The business intent names text to delete from an editable value.",),
        avoid_when=("The text is absent, occurs more than once, or caret movement itself is the business behavior.",),
        alternatives=("input_text", "send_text_keys"),
        required_control_types=("Edit", "Document"),
    ),
    _capability("clear_text", "interaction", plan_enabled=False),
    _capability(
        "expand_dropdown",
        "interaction",
        plan_enabled=True,
        purpose="Set a ComboBox to its expanded state without selecting an option.",
        use_when=("Expanded state itself is the intended result.",),
        avoid_when=("The business intent is to choose an option.",),
        alternatives=("select_dropdown_option",),
        required_control_types=("ComboBox",),
    ),
    _capability(
        "select_dropdown_option",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "option_or_name"),
        requires_value_action=True,
        plan_validation_profile="dropdown_selection",
        purpose="Choose one value from a ComboBox option set and verify it.",
        use_when=("The business intent selects an existing finite option.",),
        avoid_when=("The target is free-form text input or expansion alone is intended.",),
        alternatives=("input_text", "expand_dropdown"),
        required_control_types=("ComboBox",),
    ),
    _capability(
        "set_checked",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "checked"),
        table_value_argument=(1, "checked"),
        requires_value_action=True,
        plan_validation_profile="semantic_control_value",
        ast_match_profile="semantic_control_value",
        purpose="Set a CheckBox to an explicit checked or unchecked final state.",
        use_when=("The business result is a boolean CheckBox state.",),
        avoid_when=("The control is tri-state without a known final state.",),
        alternatives=("click",),
        required_control_types=("CheckBox",),
    ),
    _capability(
        "select_radio",
        "interaction",
        plan_enabled=True,
        purpose="Select the recorded RadioButton and verify selection.",
        required_control_types=("RadioButton",),
    ),
    _capability(
        "select_tab",
        "interaction",
        plan_enabled=True,
        purpose="Select the recorded TabItem and verify selection.",
        required_control_types=("TabItem",),
    ),
    _capability(
        "select_list_item",
        "interaction",
        plan_enabled=True,
        purpose="Select the recorded ListItem or DataItem and verify selection.",
        required_control_types=("ListItem", "DataItem"),
    ),
    _capability(
        "select_tree_item",
        "interaction",
        plan_enabled=True,
        purpose="Select the recorded TreeItem and verify selection.",
        alternatives=("set_tree_expanded",),
        required_control_types=("TreeItem",),
    ),
    _capability(
        "set_tree_expanded",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "expanded"),
        table_value_argument=(1, "expanded"),
        requires_value_action=True,
        plan_validation_profile="semantic_control_value",
        ast_match_profile="semantic_control_value",
        purpose="Set the recorded TreeItem to an expanded or collapsed final state.",
        alternatives=("select_tree_item",),
        required_control_types=("TreeItem",),
    ),
    _capability(
        "set_slider_value",
        "interaction",
        plan_enabled=True,
        value_argument=(1, "value_or_name"),
        table_value_argument=(1, "value_or_name"),
        requires_value_action=True,
        plan_validation_profile="semantic_control_value",
        ast_match_profile="semantic_control_value",
        purpose="Set a Slider RangeValue and verify its frozen range and final value.",
        use_when=("The target exposes a bounded numeric RangeValue.",),
        avoid_when=("The interaction is an unbounded geometric drag rather than a numeric final value.",),
        alternatives=("drag_by_offset",),
        required_control_types=("Slider",),
    ),
    _capability(
        "scroll_to",
        "interaction",
        plan_enabled=True,
        recorded_action_types=("scroll",),
        plan_validation_profile="frozen_scroll",
        ast_match_profile="frozen_scroll",
        purpose="Replay a frozen vertical or horizontal scroll direction and step count.",
    ),
    _capability(
        "drag_by_offset",
        "interaction",
        plan_enabled=True,
        recorded_action_types=("drag",),
        plan_validation_profile="frozen_drag",
        ast_match_profile="frozen_drag",
        purpose="Replay a frozen geometric drag offset.",
        alternatives=("set_slider_value",),
    ),
    _capability("wait_exists", "wait", plan_enabled=True),
    _capability("wait_not_exists", "wait", plan_enabled=True),
    _capability("wait_visible", "wait", plan_enabled=True),
    _capability("wait_enabled", "wait", plan_enabled=True),
    _capability("wait_exposed", "wait", plan_enabled=True),
    _capability("wait_ready", "wait", plan_enabled=True),
    _capability("get_text", "read", plan_enabled=False),
    _capability("get_attr", "read", plan_enabled=False),
    _capability("get_collection_items", "read", plan_enabled=False),
    _capability("get_ocr_text", "read", plan_enabled=False),
    _capability("extract_ocr_regex", "read", plan_enabled=False),
    _capability("set_variable", "scenario_state", plan_enabled=False),
    _capability("get_variable", "scenario_state", plan_enabled=False),
    _capability(
        "save_text",
        "scenario_state",
        plan_enabled=True,
        recorded_action_types=("observe",),
        plan_validation_profile="runtime_value_producer",
        ast_match_profile="runtime_value_producer",
        purpose="Capture observed text in a Scenario binding.",
        use_when=(
            "A later operation needs this runtime text.",
        ),
        avoid_when=(
            "Feature, Examples, Table, or data declares the value.",
        ),
        alternatives=("save_attr",),
    ),
    _capability(
        "save_attr",
        "scenario_state",
        plan_enabled=True,
        recorded_action_types=("observe",),
        plan_validation_profile="runtime_value_producer",
        ast_match_profile="runtime_value_producer",
        purpose="Capture an observed property in a Scenario binding.",
        use_when=(
            "A later operation needs this runtime property.",
        ),
        avoid_when=(
            "Frozen evidence does not support the property.",
        ),
        alternatives=("save_text",),
    ),
    _capability("assert_exists", "assertion", plan_enabled=True),
    _capability("assert_not_exists", "assertion", plan_enabled=True),
    _capability("assert_visible", "assertion", plan_enabled=True),
    _capability("assert_not_visible", "assertion", plan_enabled=True),
    _capability("assert_enabled", "assertion", plan_enabled=True),
    _capability("assert_disabled", "assertion", plan_enabled=True),
    _capability(
        "assert_text_equal",
        "assertion",
        plan_enabled=True,
        value_argument=(1, "expected"),
        table_value_argument=(1, "expected"),
        requires_value_action=True,
    ),
    _capability(
        "assert_text_contains",
        "assertion",
        plan_enabled=True,
        value_argument=(1, "expected"),
        table_value_argument=(1, "expected"),
        requires_value_action=True,
    ),
    _capability(
        "assert_text_not_contains",
        "assertion",
        plan_enabled=True,
        value_argument=(1, "expected"),
        table_value_argument=(1, "expected"),
        requires_value_action=True,
    ),
    _capability(
        "assert_text_empty",
        "assertion",
        plan_enabled=True,
        requires_value_action=True,
    ),
    _capability(
        "assert_attr_equal",
        "assertion",
        plan_enabled=True,
        value_argument=(2, "expected"),
        table_value_argument=(2, "expected"),
        requires_value_action=True,
        ast_match_profile="attribute_assertion",
    ),
    _capability(
        "assert_attr_contains",
        "assertion",
        plan_enabled=True,
        value_argument=(2, "expected"),
        table_value_argument=(2, "expected"),
        requires_value_action=True,
        ast_match_profile="attribute_assertion",
    ),
    _capability(
        "assert_collection_equal",
        "assertion",
        plan_enabled=True,
        value_argument=(1, "expected"),
        table_value_argument=(1, "expected"),
        requires_value_action=True,
        plan_validation_profile="collection_assertion",
        ast_match_profile="collection_assertion",
        ambiguity_parameters_exact=True,
    ),
    _capability(
        "assert_ocr_contains",
        "assertion",
        plan_enabled=True,
        value_argument=(0, "text"),
        table_value_argument=(0, "text"),
        requires_value_action=True,
        plan_validation_profile="ocr_assertion",
        ast_match_profile="ocr_assertion",
        ambiguity_parameters_exact=True,
    ),
    _capability(
        "assert_ocr_not_contains",
        "assertion",
        plan_enabled=True,
        value_argument=(0, "text"),
        table_value_argument=(0, "text"),
        requires_value_action=True,
        plan_validation_profile="ocr_assertion",
        ast_match_profile="ocr_assertion",
        ambiguity_parameters_exact=True,
    ),
    _capability(
        "wait_ocr_text_present",
        "visual_ocr",
        plan_enabled=False,
    ),
    _capability(
        "wait_ocr_text_absent",
        "visual_ocr",
        plan_enabled=False,
    ),
    _capability(
        "get_ocr_relative_position",
        "visual_ocr",
        plan_enabled=False,
    ),
    _capability(
        "click_ocr_relative",
        "visual_ocr",
        plan_enabled=False,
    ),
    _capability("save_ocr_debug_image", "debug", debug_only=True),
    _capability(
        "wait_pic_present",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "wait_pic_absent",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "assert_pic_exists",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "assert_pic_not_exists",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "get_pic_region",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "get_pic_relative_position",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "click_pic_relative",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability(
        "save_pic_debug_image",
        "visual_pic",
        ai_exclusion="direct_pic",
    ),
    _capability("bring_to_front", "window", plan_enabled=False),
    _capability("minimize_window", "window", plan_enabled=False),
    _capability("set_window_topmost", "window", plan_enabled=False),
    _capability("unset_window_topmost", "window", plan_enabled=False),
    _capability("send_to_back", "window", plan_enabled=False),
    _capability(
        "load_resources",
        "framework_support",
        ai_exclusion="framework_support",
    ),
    _capability(
        "get_locator",
        "framework_support",
        ai_exclusion="framework_support",
    ),
    _capability(
        "get_visual_value",
        "framework_support",
        ai_exclusion="framework_support",
    ),
    _capability(
        "get_data",
        "framework_support",
        ai_exclusion="framework_support",
    ),
    _capability("set_root", "debug", debug_only=True),
)


def plan_operation_names():
    return frozenset(
        capability.name
        for capability in AI_CAPABILITIES
        if capability.plan_enabled
    )


def contract_api_groups():
    categories = {}
    for capability in AI_CAPABILITIES:
        if capability.debug_only or capability.ai_exclusion:
            continue
        categories.setdefault(capability.category, []).append(
            capability.api_name
        )
    return {
        category: tuple(names)
        for category, names in categories.items()
    }


def debug_api_names():
    return tuple(
        capability.api_name
        for capability in AI_CAPABILITIES
        if capability.debug_only
    )


def excluded_api_names(policy=None):
    return tuple(
        capability.api_name
        for capability in AI_CAPABILITIES
        if capability.ai_exclusion
        and (
            policy is None
            or capability.ai_exclusion == str(policy)
        )
    )


def base_page_public_api_names(base_page_cls):
    return frozenset(
        name
        for name in dir(base_page_cls)
        if not name.startswith("_")
        and callable(getattr(base_page_cls, name, None))
    )


def validate_base_page_action_classification(base_page_cls):
    action_names = base_page_public_api_names(base_page_cls)
    registered_names = {
        capability.api_name
        for capability in AI_CAPABILITIES
    }
    missing = sorted(action_names - registered_names)
    stale = sorted(registered_names - action_names)
    if missing or stale:
        raise RuntimeError(
            "BasePage动作必须在AI_CAPABILITIES中显式分类: "
            f"missing={missing}, stale={stale}"
        )


def capability_by_name(name):
    return next(
        (
            capability
            for capability in AI_CAPABILITIES
            if capability.name == str(name)
        ),
        None,
    )


def operations_for_recorded_action(action_type):
    action_type = str(action_type or "")
    return frozenset(
        capability.name
        for capability in AI_CAPABILITIES
        if capability.plan_enabled
        and action_type in capability.recorded_action_types
    )


_NAMES = [capability.name for capability in AI_CAPABILITIES]
_API_NAMES = [capability.api_name for capability in AI_CAPABILITIES]
if len(_NAMES) != len(set(_NAMES)):
    raise RuntimeError("AI capability name 重复")
if len(_API_NAMES) != len(set(_API_NAMES)):
    raise RuntimeError("AI capability API name 重复")