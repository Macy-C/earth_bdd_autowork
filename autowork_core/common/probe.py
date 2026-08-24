from dataclasses import dataclass
from typing import Literal

from autowork_core.common.root_store import RootResolveResult
from autowork_core.common.target import ResolvedTarget


ProbeBackend = Literal["default", "child", "xpath", "ocr", "pic", "pos"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    backend: ProbeBackend
    target: ResolvedTarget
    root_result: RootResolveResult | None = None
    error: Exception | None = None

    @classmethod
    def from_legacy(
        cls,
        backend: ProbeBackend,
        value,
        *,
        root_result=None,
        error=None,
    ):
        return cls(
            backend=backend,
            target=ResolvedTarget.from_legacy(value),
            root_result=root_result,
            error=error,
        )

    @property
    def legacy_value(self):
        return self.target.value
