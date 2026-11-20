from __future__ import annotations

import base64
import math
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import requests


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str
    images: tuple[str | Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LlmResponse:
    content: str
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    request_id: str | None = None
    finish_reason: str | None = None


class LlmRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LlmClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        allow_insecure_http: bool = False,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        session: requests.Session | None = None,
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.read_timeout_seconds = float(read_timeout_seconds)
        self.allow_insecure_http = bool(allow_insecure_http)
        self.max_image_bytes = int(max_image_bytes)
        self._validate_configuration()
        self._owns_session = session is None
        self._session = requests.Session() if session is None else session
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            if self._owns_session:
                self._session.close()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("LLM client is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @classmethod
    def from_settings(cls, settings):
        return cls(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            read_timeout_seconds=settings.llm_read_timeout_seconds,
            allow_insecure_http=settings.llm_allow_insecure_http,
        )

    def complete(self, messages: Sequence[ChatMessage], *, max_output_tokens: int | None = None) -> LlmResponse:
        if self._closed:
            raise RuntimeError("LLM client is closed")
        if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1):
            raise ValueError("max_output_tokens must be a positive integer")
        payload_messages = self._serialize_messages(messages)
        payload = {"model": self.model, "messages": payload_messages, "stream": False}
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        try:
            response = self._session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=(
                    self.connect_timeout_seconds,
                    self.read_timeout_seconds,
                ),
            )
        except requests.RequestException as error:
            raise LlmRequestError(
                f"LLM 请求失败：{type(error).__name__}"
            ) from error

        try:
            if not response.ok:
                raise LlmRequestError(
                    f"LLM 服务返回 HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            try:
                response_payload = response.json()
            except ValueError as error:
                raise LlmRequestError("LLM 服务返回了无效 JSON") from error

            return self._parse_response(response_payload, response.headers)
        finally:
            response.close()

    def _validate_configuration(self):
        parsed_url = urlparse(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("LLM_SETTING.BASE_URL 必须是有效的 HTTP(S) URL")
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("LLM_SETTING.BASE_URL 不能包含 query 或 fragment")
        if parsed_url.scheme == "http" and not self.allow_insecure_http:
            raise ValueError(
                "LLM_SETTING 使用 HTTP；接受明文传输风险后才能启用 "
                "ALLOW_INSECURE_HTTP"
            )
        if not self.api_key:
            raise ValueError("LLM_SETTING.API_KEY 不能为空")
        if not self.model:
            raise ValueError("LLM_SETTING.MODEL 不能为空")
        if any(
            not math.isfinite(timeout) or timeout <= 0
            for timeout in (
                self.connect_timeout_seconds,
                self.read_timeout_seconds,
            )
        ):
            raise ValueError("LLM 请求超时必须大于 0")
        if self.max_image_bytes <= 0:
            raise ValueError("max_image_bytes 必须大于 0")

    def _serialize_messages(self, messages: Sequence[ChatMessage]):
        if not messages:
            raise ValueError("messages 不能为空")
        serialized = []
        for message in messages:
            if message.role not in {"system", "user", "assistant"}:
                raise ValueError(f"不支持的消息角色：{message.role}")
            if message.images and message.role != "user":
                raise ValueError("图片只能附加到 user 消息")
            if not message.content.strip() and not message.images:
                raise ValueError("消息内容不能为空")
            content: str | list[dict[str, Any]] = message.content
            if message.images:
                parts: list[dict[str, Any]] = []
                if message.content:
                    parts.append({"type": "text", "text": message.content})
                parts.extend(self._image_part(path) for path in message.images)
                content = parts
            serialized.append({"role": message.role, "content": content})
        return serialized

    def _image_part(self, image_path: str | Path):
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"图片文件不存在：{path}")
        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type not in SUPPORTED_IMAGE_TYPES:
            raise ValueError("图片格式必须是 JPEG、PNG、GIF 或 WebP")
        with path.open("rb") as image:
            image_bytes = image.read(self.max_image_bytes + 1)
        if len(image_bytes) > self.max_image_bytes:
            raise ValueError(
                f"图片超过大小限制：{self.max_image_bytes} bytes"
            )
        encoded_image = base64.b64encode(image_bytes).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{encoded_image}"
            },
        }

    @staticmethod
    def _parse_response(payload, headers):
        if not isinstance(payload, dict):
            raise LlmRequestError("LLM 服务响应必须是 JSON object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmRequestError("LLM 服务响应中没有 choices")
        first_choice = choices[0]
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text", ""), str)
            )
        if not isinstance(content, str):
            raise LlmRequestError("LLM 服务响应中没有可识别的文本内容")
        usage = payload.get("usage")
        return LlmResponse(
            content=content,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            usage=usage if isinstance(usage, dict) else None,
            request_id=headers.get("x-request-id"),
            finish_reason=(
                first_choice.get("finish_reason")
                if isinstance(first_choice.get("finish_reason"), str)
                else None
            ),
        )