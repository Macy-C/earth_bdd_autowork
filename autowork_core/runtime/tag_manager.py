"""统一识别框架标签，并按预处理、Step scope 和运行时阶段生成专用决策。

Recognizes framework tags and produces phase-specific decisions for
preprocessing, step scope selection, and runtime hooks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final


class TagConfigurationError(ValueError):
    pass


class TagKind(str, Enum):
    SKIP = "skip"
    API_ONLY = "api_only"
    STEP_SCOPE = "step_scope"
    SINGLE_STEP = "single_step"


class TagOwner(str, Enum):
    FEATURE = "Feature"
    RULE = "Rule"
    SCENARIO = "Scenario"
    SCENARIO_OUTLINE = "Scenario Outline"
    EXAMPLES = "Examples"


@dataclass(frozen=True)
class TagRule:
    kind: TagKind
    owners: tuple[TagOwner, ...]
    names: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    max_count: int | None = None


@dataclass(frozen=True)
class TagMatch:
    kind: TagKind
    text: str
    index: int
    value: str | None = None


@dataclass(frozen=True)
class RuntimeTagDecision:
    raw_tags: tuple[str, ...]
    skip_reason: str | None
    api_only: bool


@dataclass(frozen=True)
class StepScopeTagDecision:
    raw_tags: tuple[str, ...]
    step_file: str | None


@dataclass(frozen=True)
class SingleStepDirective:
    raw_tag: str
    mode: str
    step_index: int
    example_index: int | None = None
    row_index: int | None = None

    @property
    def example_id(self):
        if self.example_index is None:
            return None
        return f"{self.example_index}.{self.row_index}"


@dataclass(frozen=True)
class SingleStepTagDecision:
    raw_tags: tuple[str, ...]
    directive: SingleStepDirective | None
    passthrough_tags: tuple[str, ...]


SCENARIO_OWNERS = (TagOwner.SCENARIO, TagOwner.SCENARIO_OUTLINE)
RUNTIME_OWNERS = (TagOwner.FEATURE, TagOwner.RULE, *SCENARIO_OWNERS, TagOwner.EXAMPLES)
STEP_SCOPE_PREFIXES = ("stepfile:", "step_file:", "steps:", "step:")
SINGLE_STEP_PREFIX = "single:"
SKIP_TAG_PRIORITY = ("maint", "skip", "rep")
SINGLE_STEP_RE = re.compile(
    r"^single:(previous|background|none):([1-9]\d*)"
    r"(?::example=([1-9]\d*)\.([1-9]\d*))?$",
    re.IGNORECASE,
)

TAG_RULES = (
    TagRule(
        kind=TagKind.SKIP,
        names=SKIP_TAG_PRIORITY,
        owners=RUNTIME_OWNERS,
    ),
    TagRule(
        kind=TagKind.API_ONLY,
        names=("api",),
        owners=RUNTIME_OWNERS,
    ),
    TagRule(
        kind=TagKind.STEP_SCOPE,
        prefixes=STEP_SCOPE_PREFIXES,
        owners=(TagOwner.FEATURE, TagOwner.RULE, *SCENARIO_OWNERS),
        max_count=1,
    ),
    TagRule(
        kind=TagKind.SINGLE_STEP,
        prefixes=(SINGLE_STEP_PREFIX,),
        owners=SCENARIO_OWNERS,
        max_count=1,
    ),
)


@dataclass(frozen=True)
class TagManager:
    rules: tuple[TagRule, ...] = TAG_RULES

    def resolve_runtime(self, tags, owner, owner_name=None, effective_tags=None):
        owner = TagOwner(owner)
        raw_tags = _raw_tags(tags)
        self._validate_placement(raw_tags, owner, owner_name)
        effective_raw_tags = _raw_tags(
            tags if effective_tags is None else effective_tags
        )
        grouped = self._collect(
            effective_raw_tags,
            owner,
            owner_name,
            kinds=(
                TagKind.SKIP,
                TagKind.API_ONLY,
            ),
        )
        skip_reason = _first_exact(grouped[TagKind.SKIP], SKIP_TAG_PRIORITY)
        api_only = skip_reason is None and bool(grouped[TagKind.API_ONLY])
        return RuntimeTagDecision(
            raw_tags=effective_raw_tags,
            skip_reason=skip_reason,
            api_only=api_only,
        )

    def resolve_step_scope(
            self,
            tags,
            owner=TagOwner.FEATURE,
            owner_name=None,
    ):
        try:
            owner = TagOwner(owner)
        except ValueError:
            if owner_name is not None:
                raise
            owner_name = str(owner)
            owner = TagOwner.FEATURE
        raw_tags = _raw_tags(tags)
        self._validate_placement(raw_tags, owner, owner_name)
        grouped = self._collect(
            raw_tags,
            owner,
            owner_name,
            kinds=(TagKind.STEP_SCOPE,),
        )
        matches = grouped[TagKind.STEP_SCOPE]
        return StepScopeTagDecision(
            raw_tags=raw_tags,
            step_file=matches[0].value if matches else None,
        )

    def resolve_single_step(self, tags, owner, owner_name=None, effective_tags=None):
        owner = TagOwner(owner)
        raw_tags = _raw_tags(tags)
        self._validate_placement(raw_tags, owner, owner_name)
        grouped = self._collect(
            raw_tags,
            owner,
            owner_name,
            kinds=(TagKind.SINGLE_STEP,),
        )
        matches = grouped[TagKind.SINGLE_STEP]
        directive = self._single_step_directive(matches, owner, owner_name)
        effective_raw_tags = _raw_tags(
            tags if effective_tags is None else effective_tags
        )
        skip_reason = _first_raw_exact(effective_raw_tags, SKIP_TAG_PRIORITY)
        if skip_reason and directive is not None:
            raise TagConfigurationError(
                f"@{skip_reason} cannot be combined with @{directive.raw_tag} "
                f"on {_owner_label(owner, owner_name)}"
            )
        return SingleStepTagDecision(
            raw_tags=raw_tags,
            directive=directive,
            passthrough_tags=self._single_passthrough_tags(effective_raw_tags),
        )

    def _single_passthrough_tags(self, tags):
        excluded_kinds = (TagKind.STEP_SCOPE, TagKind.SINGLE_STEP)
        passthrough = []
        for index, text in enumerate(tags):
            matched = self._match(text, index, excluded_kinds)
            if matched is None:
                passthrough.append(text)
        return tuple(passthrough)

    def _validate_placement(self, raw_tags, owner, owner_name):
        all_kinds = tuple(TagKind)
        for index, text in enumerate(raw_tags):
            matched = self._match(text, index, all_kinds)
            if matched is None:
                continue
            rule, _ = matched
            if owner not in rule.owners:
                raise TagConfigurationError(
                    f"@{text} is not allowed on {_owner_label(owner, owner_name)}"
                )

    def _collect(self, raw_tags, owner, owner_name, kinds):
        grouped_lists = {kind: [] for kind in kinds}
        for index, text in enumerate(raw_tags):
            matched = self._match(text, index, kinds)
            if matched is None:
                continue
            _, match = matched
            grouped_lists[match.kind].append(match)

        grouped = {
            kind: tuple(matches)
            for kind, matches in grouped_lists.items()
        }
        self._validate_counts(grouped, owner, owner_name, kinds)
        return grouped

    def _match(self, text, index, kinds):
        normalized = normalize_tag(text)
        for rule in self.rules:
            if rule.kind not in kinds:
                continue
            if normalized in rule.names:
                return rule, TagMatch(rule.kind, text, index)
            for prefix in rule.prefixes:
                if normalized.startswith(normalize_tag(prefix)):
                    return rule, TagMatch(
                        rule.kind,
                        text,
                        index,
                        value=text[len(prefix):],
                    )
        return None

    def _validate_counts(self, grouped, owner, owner_name, kinds):
        for rule in self.rules:
            if rule.kind not in kinds or rule.max_count is None:
                continue
            matches = grouped[rule.kind]
            if len(matches) <= rule.max_count:
                continue
            tags = ", ".join(f"@{match.text}" for match in matches)
            if rule.kind == TagKind.STEP_SCOPE:
                message = "Only one step-file tag is allowed"
            elif rule.kind == TagKind.SINGLE_STEP:
                message = "Only one single-step tag is allowed"
            else:
                message = f"Only {rule.max_count} {rule.kind.value} tag is allowed"
            raise TagConfigurationError(
                f"{message} on {_owner_label(owner, owner_name)}: {tags}"
            )

    @staticmethod
    def _single_step_directive(matches, owner, owner_name):
        if not matches:
            return None
        raw_tag = matches[0].text
        match = SINGLE_STEP_RE.fullmatch(raw_tag)
        if not match:
            raise TagConfigurationError(
                f"Invalid single-step tag: @{raw_tag}. Expected "
                "@single:<previous|background|none>:<step-index>"
                "[:example=<examples-index>.<row-index>]"
            )

        example_index = int(match.group(3)) if match.group(3) else None
        row_index = int(match.group(4)) if match.group(4) else None
        if owner == TagOwner.SCENARIO_OUTLINE and example_index is None:
            raise TagConfigurationError(
                f"{_owner_label(owner, owner_name)} requires "
                "example=<examples-index>.<row-index>"
            )
        if owner == TagOwner.SCENARIO and example_index is not None:
            raise TagConfigurationError(
                f"{_owner_label(owner, owner_name)} is not an outline and "
                "cannot use example=..."
            )

        return SingleStepDirective(
            raw_tag=raw_tag,
            mode=match.group(1).lower(),
            step_index=int(match.group(2)),
            example_index=example_index,
            row_index=row_index,
        )


def tag_text(tag):
    if tag is None:
        return ""
    text = str(tag).strip()
    return text[1:] if text.startswith("@") else text


def normalize_tag(tag):
    return tag_text(tag).casefold()


def scenario_tag_owner(scenario):
    if getattr(scenario, "_row", None) is not None:
        return TagOwner.SCENARIO_OUTLINE
    if type(scenario).__name__ == "ScenarioOutline":
        return TagOwner.SCENARIO_OUTLINE
    return TagOwner.SCENARIO


def _raw_tags(tags):
    return tuple(
        text
        for tag in tags or ()
        if (text := tag_text(tag))
    )


def _owner_label(owner, owner_name):
    return f"{owner.value} {owner_name!r}" if owner_name else owner.value


def _first_raw_exact(raw_tags, candidates):
    for candidate in candidates:
        normalized = normalize_tag(candidate)
        if any(normalize_tag(tag) == normalized for tag in raw_tags):
            return normalized
    return None


def _first_exact(matches, candidates):
    for candidate in candidates:
        if _has_exact(matches, candidate):
            return normalize_tag(candidate)
    return None


def _has_exact(matches, candidate):
    normalized = normalize_tag(candidate)
    return any(normalize_tag(match.text) == normalized for match in matches)


TAG_MANAGER: Final = TagManager()
