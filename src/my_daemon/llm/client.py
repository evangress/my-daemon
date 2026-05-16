"""Thin Anthropic client wrapper for synthesis (sync + streaming)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from my_daemon.config import LLMConfig
from my_daemon.llm.prompts import SYSTEM_PROMPT, build_user_message
from my_daemon.models import RetrievedChunk

if TYPE_CHECKING:
    from anthropic import Anthropic


class LLMClient:
    def __init__(self, config: LLMConfig, api_key: str | None) -> None:
        self.config = config
        self.api_key = api_key
        self._client: Anthropic | None = None

    def _client_(self) -> Anthropic:
        if self._client is None:
            from anthropic import Anthropic
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Add it to .env or your shell environment."
                )
            self._client = Anthropic(api_key=self.api_key)
        return self._client

    def _build_kwargs(self, query: str, chunks: list[RetrievedChunk]) -> dict:
        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_message(query, chunks)}],
        }
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        return kwargs

    def synthesize(self, query: str, chunks: list[RetrievedChunk]) -> str:
        client = self._client_()
        message = client.messages.create(**self._build_kwargs(query, chunks))
        out: list[str] = []
        for block in message.content:
            if getattr(block, "type", None) == "text":
                out.append(block.text)
        return "\n".join(out).strip()

    def synthesize_stream(self, query: str, chunks: list[RetrievedChunk]) -> Iterator[str]:
        """Yield text deltas as they arrive. Use for live-updating UIs."""
        client = self._client_()
        with client.messages.stream(**self._build_kwargs(query, chunks)) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
