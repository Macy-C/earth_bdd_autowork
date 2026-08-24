"""发现 Feature、解析 Step 文件标签，并安全地把每个 Feature 映射到 Step 文件。

Discovers features, parses step-file tags, and safely maps each
feature to its step-definition file.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from behave.model import ScenarioOutline

from autowork_core.runtime.tag_manager import (
    TAG_MANAGER,
    TagOwner,
)

SKIPPED_STEP_FILE_NAMES = {
    "__init__.py",
}


@dataclass(frozen=True)
class StepScopeDeclaration:
    owner: str
    owner_name: str
    owner_line: int
    step_file: str
    explicit: bool


@dataclass(frozen=True)
class ResolvedStepScope:
    files: tuple[str, ...]
    entry_file: str
    origin: str
    declarations: tuple[StepScopeDeclaration, ...]
    file_statuses: tuple[tuple[str, str], ...]
    fingerprint: str

    def runtime_scope(self, *, through_owner=None):
        declarations = list(self.declarations)
        if through_owner is not None:
            owner_order = {
                "Feature": 0,
                "Rule": 1,
                "Scenario": 2,
                "Scenario Outline": 2,
            }
            maximum = owner_order[str(through_owner)]
            declarations = [
                declaration
                for declaration in declarations
                if owner_order[declaration.owner] <= maximum
            ]
        files = list(dict.fromkeys(
            declaration.step_file
            for declaration in declarations
        ))
        origin = (
            declarations[-1].owner
            if declarations[-1].explicit
            else "inferred"
        )
        identity = {
            "files": files,
            "entry_file": declarations[-1].step_file,
            "origin": origin,
            "declarations": [
                {
                    key: value
                    for key, value in asdict(declaration).items()
                    if key != "owner_line"
                }
                for declaration in declarations
            ],
        }
        return {
            "files": files,
            "entry_file": declarations[-1].step_file,
            "origin": origin,
            "declarations": [
                asdict(declaration)
                for declaration in declarations
            ],
            "fingerprint": hashlib.sha256(json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        }

    def public_dict(self):
        return {
            "files": list(self.files),
            "entry_file": self.entry_file,
            "origin": self.origin,
            "declarations": [
                asdict(declaration)
                for declaration in self.declarations
            ],
            "file_statuses": dict(self.file_statuses),
            "fingerprint": self.fingerprint,
        }


def collect_feature_files(feature_path):
    path = Path(feature_path)
    if path.is_file():
        return [path] if path.suffix.lower() == ".feature" else []
    if path.is_dir():
        return sorted(path.rglob("*.feature"))
    return []


def read_feature_tags(feature_file):
    feature_file = Path(feature_file)
    try:
        lines = feature_file.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = feature_file.read_text().splitlines()

    tags = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            tags.extend(part[1:] for part in line.split() if part.startswith("@"))
            continue
        if line.lower().startswith("feature:"):
            return tags
        return []
    return []


def step_scope_for_feature(feature_file, steps_dir):
    decision = TAG_MANAGER.resolve_step_scope(
        read_feature_tags(feature_file),
        TagOwner.FEATURE,
        str(feature_file),
    )
    if decision.step_file:
        files = [_normalize_step_file(decision.step_file, steps_dir)]
    else:
        files = [_infer_step_file_for_feature(feature_file, steps_dir)]

    return {"files": files}


def resolved_step_scope_for_scenario(
        feature,
        scenario,
        steps_dir,
        *,
        require_files=True,
):
    """Resolve the layered Step files visible to one concrete Scenario."""
    steps_dir = Path(steps_dir).resolve()
    template = _scenario_template(scenario)
    rule = _scenario_rule(template)
    _validate_examples_scope_tags(template)

    declarations = []
    feature_decision = TAG_MANAGER.resolve_step_scope(
        feature.tags,
        TagOwner.FEATURE,
        feature.name,
    )
    if feature_decision.step_file:
        feature_file = _normalize_step_file(
            feature_decision.step_file,
            steps_dir,
            require_exists=require_files,
        )
        declarations.append(_declaration(
            feature,
            TagOwner.FEATURE,
            feature_file,
            explicit=True,
        ))
    else:
        feature_file = _infer_step_file_for_feature(
            feature.filename,
            steps_dir,
            require_exists=require_files,
        )
        declarations.append(_declaration(
            feature,
            TagOwner.FEATURE,
            feature_file,
            explicit=False,
        ))

    if rule is not None:
        rule_decision = TAG_MANAGER.resolve_step_scope(
            rule.tags,
            TagOwner.RULE,
            rule.name,
        )
        if rule_decision.step_file:
            declarations.append(_declaration(
                rule,
                TagOwner.RULE,
                _normalize_step_file(
                    rule_decision.step_file,
                    steps_dir,
                    require_exists=require_files,
                ),
                explicit=True,
            ))

    scenario_owner = (
        TagOwner.SCENARIO_OUTLINE
        if isinstance(template, ScenarioOutline)
        else TagOwner.SCENARIO
    )
    scenario_decision = TAG_MANAGER.resolve_step_scope(
        template.tags,
        scenario_owner,
        template.name,
    )
    if scenario_decision.step_file:
        declarations.append(_declaration(
            template,
            scenario_owner,
            _normalize_step_file(
                scenario_decision.step_file,
                steps_dir,
                require_exists=require_files,
            ),
            explicit=True,
        ))

    files = tuple(dict.fromkeys(
        declaration.step_file
        for declaration in declarations
    ))
    file_statuses = tuple(
        (
            file_value,
            "existing"
            if (steps_dir / file_value).is_file()
            else "missing_create_allowed",
        )
        for file_value in files
    )
    origin = declarations[-1].owner if declarations[-1].explicit else "inferred"
    payload = {
        "files": files,
        "entry_file": declarations[-1].step_file,
        "origin": origin,
        "declarations": [
            {
                key: value
                for key, value in asdict(item).items()
                if key != "owner_line"
            }
            for item in declarations
        ],
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return ResolvedStepScope(
        files=files,
        entry_file=declarations[-1].step_file,
        origin=origin,
        declarations=tuple(declarations),
        file_statuses=file_statuses,
        fingerprint=fingerprint,
    )


def scenario_scope_key(feature, scenario):
    template = _scenario_template(scenario)
    row = getattr(scenario, "_row", None)
    example_id = str(getattr(row, "id", "") or "")
    return "|".join((
        str(Path(feature.filename).resolve()),
        str(_line(template)),
        example_id,
    ))


def rule_scope_key(feature, rule):
    return "|".join((
        str(Path(feature.filename).resolve()),
        "rule",
        str(_line(rule)),
    ))


def resolve_scoped_step_path(steps_dir, value):
    path = resolve_under_steps(value, steps_dir, label="Scoped step path")
    if not path.exists():
        raise FileNotFoundError(f"Scoped step path does not exist: {path}")
    return path


def resolve_under_steps(value, steps_dir, *, label="Feature step path"):
    steps_root = Path(steps_dir).resolve()
    normalized_value = str(value).replace("\\", "/")
    path = (steps_root / normalized_value).resolve()
    try:
        path.relative_to(steps_root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under {steps_root}: {value}") from exc
    return path


def iter_step_files(steps_dir):
    for py_file in sorted(Path(steps_dir).resolve().rglob("*.py")):
        if py_file.name in SKIPPED_STEP_FILE_NAMES:
            continue
        yield py_file.resolve()


def relative_step_path(path, steps_dir):
    return Path(path).resolve().relative_to(Path(steps_dir).resolve()).as_posix()


def _normalize_step_file(value, steps_dir, *, require_exists=True):
    value = _normalize_step_value(value)
    if value.startswith("steps."):
        value = value[len("steps."):].replace(".", "/")
    value = _strip_steps_prefix(value)
    if not value.endswith(".py"):
        value = f"{value}.py"
    path = _resolve_step_file(
        value,
        steps_dir,
        require_exists=require_exists,
    )
    return relative_step_path(path, steps_dir)


def _infer_step_file_for_feature(
        feature_file,
        steps_dir,
        *,
        require_exists=True,
):
    feature_key = _step_key(Path(feature_file).stem)
    matches = []
    for py_file in iter_step_files(steps_dir):
        step_key = _step_key(py_file.stem)
        if step_key.endswith("_step"):
            step_key = step_key[:-len("_step")]
        if feature_key == step_key or feature_key.startswith(f"{step_key}_"):
            matches.append(py_file)

    if not matches:
        expected_names = _expected_step_names(feature_key)
        if not require_exists:
            return expected_names[-1]
        raise FileNotFoundError(
            f"No step file found for feature {feature_file}. "
            f"Expected a matching file like {' or '.join(expected_names)} "
            f"under {Path(steps_dir).resolve()}."
        )
    if len(matches) > 1:
        choices = ", ".join(relative_step_path(path, steps_dir) for path in matches)
        raise ValueError(f"Multiple step files match feature {feature_file}: {choices}")
    return relative_step_path(matches[0], steps_dir)


def _resolve_step_file(value, steps_dir, *, require_exists=True):
    direct_path = resolve_under_steps(value, steps_dir)
    if direct_path.is_file():
        return direct_path

    if "/" in value:
        if not require_exists:
            return direct_path
        raise FileNotFoundError(f"Feature step file does not exist: {direct_path}")

    matches = [path for path in iter_step_files(steps_dir) if path.name == value]
    if not matches:
        if not require_exists:
            return direct_path
        raise FileNotFoundError(
            f"Feature step file does not exist under {Path(steps_dir).resolve()}: {value}"
        )
    if len(matches) > 1:
        choices = ", ".join(relative_step_path(path, steps_dir) for path in matches)
        raise ValueError(f"Step file name is ambiguous: {value}. Matches: {choices}")
    return matches[0]


def _normalize_step_value(value):
    return str(value).strip().strip("'\"").replace("\\", "/").strip("/")


def _strip_steps_prefix(value):
    for prefix in ("Bdd/steps/", "steps/"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _step_key(value):
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", str(value)).strip("_").lower()
    return re.sub(r"_+", "_", normalized)


def _expected_step_names(feature_key):
    names = [f"{feature_key}_step.py"]
    trimmed_key = re.sub(r"_\d+$", "", feature_key)
    if trimmed_key != feature_key:
        names.insert(0, f"{trimmed_key}_step.py")
    return names


def _scenario_template(scenario):
    parent = getattr(scenario, "parent", None)
    return parent if isinstance(parent, ScenarioOutline) else scenario


def _scenario_rule(template):
    parent = getattr(template, "parent", None)
    return parent if getattr(parent, "keyword", None) == "Rule" else None


def _validate_examples_scope_tags(template):
    if not isinstance(template, ScenarioOutline):
        return
    for examples in template.examples:
        TAG_MANAGER.resolve_step_scope(
            examples.tags,
            TagOwner.EXAMPLES,
            examples.name,
        )


def _declaration(statement, owner, step_file, *, explicit):
    return StepScopeDeclaration(
        owner=owner.value,
        owner_name=str(getattr(statement, "name", "") or ""),
        owner_line=_line(statement),
        step_file=step_file,
        explicit=bool(explicit),
    )


def _line(statement):
    return int(getattr(getattr(statement, "location", None), "line", 0) or 0)