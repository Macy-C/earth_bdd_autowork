from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Literal, Protocol, Sequence

from autowork_core.integrations.llm import ChatMessage, LlmClient, LlmResponse


class ModelClient(Protocol):
    def complete(self, messages: Sequence[ChatMessage], *, max_output_tokens: int | None = None) -> LlmResponse: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class AnalysisRequest:
    messages: tuple[ChatMessage, ...]
    output_format: Literal["text", "json"] = "text"
    validator: Callable[[Any], bool | None] | None = field(
        default=None, repr=False, compare=False,
    )
    max_output_tokens: int | None = None

    def __post_init__(self):
        messages = tuple(self.messages)
        if not messages or any(not isinstance(item, ChatMessage) for item in messages):
            raise ValueError("messages must contain ChatMessage instances")
        if self.output_format not in {"text", "json"}:
            raise ValueError("output_format must be text or json")
        if self.validator is not None and not callable(self.validator):
            raise ValueError("validator must be callable")
        if self.max_output_tokens is not None and (type(self.max_output_tokens) is not int or self.max_output_tokens < 1):
            raise ValueError("max_output_tokens must be a positive integer")
        object.__setattr__(self, "messages", tuple(
            ChatMessage(item.role, item.content, tuple(item.images))
            for item in messages
        ))


@dataclass(frozen=True)
class AnalysisResult:
    response: LlmResponse
    data: Any


class AnalysisError(RuntimeError):
    def __init__(self, code: str, message: str, *, response: LlmResponse):
        super().__init__(message)
        self.code = code
        self.response = response


def _reject_json_constant(value: str):
    raise ValueError("Non-finite JSON number")


class Analyzer:
    def __init__(
        self,
        client: ModelClient | None = None,
        *,
        client_factory: Callable[[], ModelClient] | None = None,
        supports_images: bool = False,
    ):
        if (client is None) == (client_factory is None):
            raise ValueError("Provide exactly one of client or client_factory")
        self._client = client
        self._client_factory = client_factory
        self._owns_client = client is None
        self._supports_images = supports_images
        self._closed = False

    @classmethod
    def from_settings(cls, settings, *, supports_images: bool = False) -> Analyzer:
        snapshot = SimpleNamespace(**{
            name: getattr(settings, name)
            for name in (
                "llm_base_url", "llm_api_key", "llm_model",
                "llm_connect_timeout_seconds", "llm_read_timeout_seconds",
                "llm_allow_insecure_http",
            )
        })
        return cls(
            client_factory=lambda: LlmClient.from_settings(snapshot),
            supports_images=supports_images,
        )

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        if self._closed:
            raise RuntimeError("Analyzer is closed")
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be AnalysisRequest")
        if not self._supports_images and any(item.images for item in request.messages):
            raise ValueError("Image capability must be explicitly enabled")
        if self._client is None:
            if self._client_factory is None:
                raise RuntimeError("No model client factory")
            self._client = self._client_factory()

        options = {} if request.max_output_tokens is None else {"max_output_tokens": request.max_output_tokens}
        response = self._client.complete(request.messages, **options)
        if response.finish_reason not in {None, "stop"}:
            raise AnalysisError(
                "incomplete_response", "Model did not finish a text answer",
                response=response,
            )
        if not isinstance(response.content, str) or not response.content.strip():
            raise AnalysisError(
                "empty_response", "Model returned no text", response=response,
            )
        data = response.content
        if request.output_format == "json":
            try:
                data = json.loads(response.content, parse_constant=_reject_json_constant)
            except (ValueError, RecursionError):
                raise AnalysisError(
                    "invalid_json", "Model output is not valid JSON", response=response,
                ) from None
        if request.validator is not None:
            try:
                valid = request.validator(data)
                if valid is not None and valid is not True:
                    raise ValueError("Output rejected")
            except Exception:
                raise AnalysisError(
                    "validation_failed", "Model output failed validation",
                    response=response,
                ) from None
        return AnalysisResult(response=response, data=data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_client and self._client is not None:
                self._client.close()

    def __enter__(self) -> Analyzer:
        if self._closed:
            raise RuntimeError("Analyzer is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise