"""Minimal native-Ollama client for paired thinking/non-thinking runs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests


class OllamaError(RuntimeError):
    """Raised when the Ollama API cannot complete a request."""


@dataclass
class ChatResult:
    model: str
    think: bool
    content: str
    thinking: str
    done: bool
    done_reason: str | None
    eval_count: int
    prompt_eval_count: int
    total_duration_ns: int
    load_duration_ns: int
    prompt_eval_duration_ns: int
    eval_duration_ns: int
    wall_start: float
    wall_end: float
    first_token_at: float | None = None
    last_token_at: float | None = None
    chunk_count: int = 0
    fallback_prompt_suffix: bool = False
    raw_final: dict[str, Any] = field(default_factory=dict)

    @property
    def wall_duration_s(self) -> float:
        return max(0.0, self.wall_end - self.wall_start)

    @property
    def time_to_first_token_s(self) -> float | None:
        if self.first_token_at is None:
            return None
        return max(0.0, self.first_token_at - self.wall_start)

    @property
    def tokens_per_second(self) -> float | None:
        if self.eval_duration_ns <= 0:
            return None
        return self.eval_count / (self.eval_duration_ns / 1e9)


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:1.7b",
        timeout_s: float = 1800.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def tags(self) -> dict[str, Any]:
        try:
            response = requests.get(self._url("/api/tags"), timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise OllamaError(f"cannot reach Ollama at {self.base_url}: {exc}") from exc

    def model_info(self, model: str | None = None) -> dict[str, Any] | None:
        model = model or self.model
        payload = self.tags()
        for item in payload.get("models", []):
            if item.get("name") == model or item.get("model") == model:
                return item
        return None

    def check_model(self, expected_digest: str | None = None) -> dict[str, Any]:
        info = self.model_info()
        if info is None:
            raise OllamaError(
                f"model '{self.model}' is not installed. Run 'ollama pull {self.model}'."
            )
        digest = str(info.get("digest", ""))
        if expected_digest and digest and not digest.startswith(expected_digest):
            raise OllamaError(
                f"model digest mismatch: expected {expected_digest}, found {digest}. "
                "Re-run 'make install' or update configs/host.local.yaml."
            )
        return info

    @staticmethod
    def _options(config: Any) -> dict[str, Any]:
        values = {
            "temperature": config.temperature,
            "seed": config.seed,
            "top_p": config.top_p,
            "top_k": config.top_k,
            "num_predict": config.num_predict,
            "num_ctx": config.num_ctx,
            "num_gpu": config.num_gpu,
        }
        return {key: value for key, value in values.items() if value is not None}

    def _payload(
        self,
        question: str,
        think: bool,
        config: Any,
        prompt_suffix: bool = False,
    ) -> dict[str, Any]:
        user_content = question
        if prompt_suffix:
            user_content = f"{question} {'/think' if think else '/no_think'}"
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": config.system_prompt},
                {"role": "user", "content": user_content},
            ],
            "think": think,
            "stream": True,
            "options": self._options(config),
        }

    @staticmethod
    def _consume_stream(
        lines: Iterable[str],
        *,
        model: str,
        think: bool,
        wall_start: float,
        fallback_prompt_suffix: bool = False,
    ) -> ChatResult:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        final: dict[str, Any] = {}
        first_token_at: float | None = None
        last_token_at: float | None = None
        chunks = 0

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunks += 1
            message = event.get("message") or {}
            piece = message.get("content") or ""
            thought = message.get("thinking") or ""
            if piece:
                content_parts.append(piece)
            if thought:
                thinking_parts.append(thought)
            if piece or thought:
                now = time.monotonic()
                if first_token_at is None:
                    first_token_at = now
                last_token_at = now
            if event.get("done"):
                final = event
            if event.get("error"):
                raise OllamaError(str(event["error"]))

        content = "".join(content_parts)
        thinking = "".join(thinking_parts)
        done = bool(final.get("done", False))
        if not done:
            raise OllamaError("Ollama stream ended without a final done=true event")

        return ChatResult(
            model=model,
            think=think,
            content=content,
            thinking=thinking,
            done=done,
            done_reason=final.get("done_reason"),
            eval_count=int(final.get("eval_count") or 0),
            prompt_eval_count=int(final.get("prompt_eval_count") or 0),
            total_duration_ns=int(final.get("total_duration") or 0),
            load_duration_ns=int(final.get("load_duration") or 0),
            prompt_eval_duration_ns=int(final.get("prompt_eval_duration") or 0),
            eval_duration_ns=int(final.get("eval_duration") or 0),
            wall_start=wall_start,
            wall_end=time.monotonic(),
            first_token_at=first_token_at,
            last_token_at=last_token_at,
            chunk_count=chunks,
            fallback_prompt_suffix=fallback_prompt_suffix,
            raw_final={
                key: final.get(key)
                for key in (
                    "model",
                    "created_at",
                    "done",
                    "done_reason",
                    "total_duration",
                    "load_duration",
                    "prompt_eval_count",
                    "prompt_eval_duration",
                    "eval_count",
                    "eval_duration",
                )
                if key in final
            },
        )

    def _chat_once(
        self,
        question: str,
        think: bool,
        config: Any,
        prompt_suffix: bool = False,
    ) -> ChatResult:
        payload = self._payload(question, think, config, prompt_suffix=prompt_suffix)
        wall_start = time.monotonic()
        try:
            response = requests.post(
                self._url("/api/chat"),
                json=payload,
                stream=True,
                timeout=(10, self.timeout_s),
            )
        except requests.RequestException as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc

        if response.status_code >= 400:
            body = response.text[:1000]
            response.close()
            raise OllamaError(f"Ollama HTTP {response.status_code}: {body}")

        return self._consume_stream(
            response.iter_lines(decode_unicode=True),
            model=self.model,
            think=think,
            wall_start=wall_start,
            fallback_prompt_suffix=prompt_suffix,
        )

    def chat(self, question: str, think: bool, config: Any) -> ChatResult:
        """Run one chat request, with a prompt-suffix fallback for old servers."""

        try:
            return self._chat_once(question, think, config)
        except OllamaError as exc:
            message = str(exc).lower()
            # Older Ollama builds reject the `think` field. Fall back to the
            # /think and /no_think prompt suffix used by Qwen3 templates.
            if "think" not in message or "400" not in message:
                raise
            return self._chat_once(question, think, config, prompt_suffix=True)
