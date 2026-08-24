from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2.1"
SUPPORTED_SCHEMA_VERSIONS = ("2.0", "2.1")


@dataclass(frozen=True)
class StepPlan:
    id: str
    key: str
    ordinal: int
    keyword: str
    text: str
    line: int
    semantic_type: str = ""
    is_background: bool = False
    selected: bool = True
    text_block: str | None = None
    table: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScenarioPlan:
    id: str
    key: str
    logical_template_id: str
    name: str
    line: int
    kind: str
    example_id: str | None
    example_values: dict[str, str]
    tags: tuple[str, ...]
    steps: tuple[StepPlan, ...]
    specification: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self):
        if self.example_id:
            return f"{self.name} [Examples {self.example_id}]"
        return self.name


@dataclass(frozen=True)
class FeaturePlan:
    id: str
    key: str
    source_path: Path
    source_relpath: str
    source_hash: str
    name: str
    line: int
    tags: tuple[str, ...]
    scenarios: tuple[ScenarioPlan, ...]
    description: tuple[str, ...] = ()


@dataclass
class RecordingEvent:
    id: str
    index: int
    event_type: str
    monotonic_ms: int
    wall_time: str
    point: list[int] | None = None
    button: str | None = None
    wheel_delta: int | None = None
    key: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    screenshot: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


@dataclass
class StepTake:
    id: str
    step_id: str
    take_number: int
    started_at: str
    started_monotonic: float
    directory: Path
    events: list[RecordingEvent] = field(default_factory=list)
    ended_at: str | None = None
    ended_monotonic: float | None = None
    video_path: Path | None = None
    error: str | None = None


def public_dict(value):
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: public_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_dict(item) for item in value]
    return value