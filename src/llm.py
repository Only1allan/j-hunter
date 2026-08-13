"""LLM provider layer. SiliconFlow (OpenAI-compatible) via httpx.

Two things here are load-bearing:

**Structured output.** GLM-5.2 doesn't support response_format: json_object, so
structured output is achieved by embedding the Pydantic JSON schema in the system
prompt and parsing the model's JSON response. The model wraps JSON in markdown
code fences — _extract_json strips them before json.loads + Pydantic validation.

**Token accounting.** Usage tracks input/output tokens and cache stats from
SiliconFlow's usage response, so cost is observable per run.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from . import config

T = TypeVar("T", bound=BaseModel)


class RefusalError(RuntimeError):
    """The model declined. Content is empty or partial; do not read it."""


@dataclass
class Usage:
    """Token accounting, so cost and cache effectiveness are observable."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def add_raw(self, *, input_tokens: int = 0, output_tokens: int = 0,
                      cache_creation: int = 0, cache_read: int = 0) -> None:
        async with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cache_creation += cache_creation
            self.cache_read += cache_read

    def report(self) -> str:
        cached_total = self.cache_creation + self.cache_read
        hit_rate = (self.cache_read / cached_total * 100) if cached_total else 0.0
        return (
            f"{self.calls} calls | in {self.input_tokens:,} "
            f"out {self.output_tokens:,} | "
            f"cache write {self.cache_creation:,} read {self.cache_read:,} "
            f"({hit_rate:.0f}% read)"
        )

    @property
    def cache_working(self) -> bool:
        return self.cache_read > 0


class LLMClient(Protocol):
    """The interface the pipeline depends on. SiliconFlow is the sole provider."""

    async def generate(self, *, stable_system: str | list[str], user: str,
                       max_tokens: int = ...) -> str: ...

    async def extract(
        self, *, stable_system: str | list[str], user: str, schema: type[T],
        max_tokens: int = ...
    ) -> T: ...


class SiliconFlowClient:
    """SiliconFlow API client. OpenAI-compatible, uses httpx (no SDK dependency).

    JSON mode (response_format: json_object) is not supported for GLM-5.2 on
    SiliconFlow, so structured output is achieved by prompting the model to
    return JSON and parsing the response. The model wraps JSON in markdown code
    fences, which are stripped before parsing.

    Prompt caching: SiliconFlow exposes prompt_tokens_details.cached_tokens in
    the usage response. We track the value for observability.
    """

    name = "siliconflow"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = api_key or config.SILICONFLOW_API_KEY
        if not key:
            raise RuntimeError(
                "SILICONFLOW_API_KEY is not set. Add it to .env."
            )
        self.model = model or config.SILICONFLOW_MODEL
        self._base = config.SILICONFLOW_BASE
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        self.usage = Usage()
        self._sem = asyncio.Semaphore(config.MAX_CONCURRENCY)

    async def _chat(
        self, *, stable_system: str | list[str], user: str,
        max_tokens: int, json_schema: dict | None = None,
    ) -> dict:
        """POST /chat/completions. Returns the parsed JSON response."""
        if isinstance(stable_system, list):
            system = "\n\n".join(s for s in stable_system if s)
        else:
            system = stable_system or ""

        if json_schema:
            system += (
                "\n\n# OUTPUT FORMAT\n"
                "You MUST respond with ONLY a JSON object. No markdown, no code "
                "fences, no explanation, no text before or after. The JSON must "
                "conform to this schema:\n"
                f"{json.dumps(json_schema, indent=2, ensure_ascii=False)}"
            )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        async with self._sem:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers,
                    json=body,
                )

        if resp.status_code != 200:
            raise RuntimeError(
                f"SiliconFlow API error {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()

        # Track usage
        u = data.get("usage", {})
        prompt_details = u.get("prompt_tokens_details", {})
        await self.usage.add_raw(
            input_tokens=u.get("prompt_tokens", 0) or 0,
            output_tokens=u.get("completion_tokens", 0) or 0,
            cache_creation=u.get("prompt_cache_miss_tokens", 0) or 0,
            cache_read=prompt_details.get("cached_tokens", 0) or 0,
        )

        # Check finish reason
        choice = data.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason", "")
        if finish_reason == "length":
            raise RuntimeError(
                f"SiliconFlow response truncated (finish_reason=length). "
                f"Raise max_tokens (currently {max_tokens})."
            )
        if finish_reason == "content_filter":
            raise RefusalError("SiliconFlow content filter triggered")

        return data

    @staticmethod
    def _extract_json(content: str) -> dict:
        """Parse JSON from a model response that may wrap it in markdown fences."""
        clean = content.strip()
        # Strip markdown code fences
        if clean.startswith("```"):
            lines = clean.split("\n")
            # Drop the opening fence line (```json or ```)
            clean = "\n".join(lines[1:])
            # Drop trailing fence
            if clean.rstrip().endswith("```"):
                clean = clean.rsplit("```", 1)[0]
            clean = clean.strip()
        # If there's prose around the JSON, extract the first {...} block
        if not clean.startswith("{"):
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if match:
                clean = match.group(0)
        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse JSON from model response: {e}. "
                f"Content (first 300 chars): {content[:300]}"
            )

    async def generate(
        self, *, stable_system: str | list[str], user: str, max_tokens: int | None = None
    ) -> str:
        """Free-text generation."""
        data = await self._chat(
            stable_system=stable_system, user=user,
            max_tokens=max_tokens or config.MAX_TOKENS,
        )
        content = data["choices"][0]["message"]["content"]
        return content.strip()

    async def extract(
        self,
        *,
        stable_system: str | list[str],
        user: str,
        schema: type[T],
        max_tokens: int | None = None,
    ) -> T:
        """Structured extraction via prompt-based JSON + Pydantic validation."""
        json_schema = schema.model_json_schema()
        data = await self._chat(
            stable_system=stable_system, user=user,
            max_tokens=max_tokens or config.MAX_TOKENS,
            json_schema=json_schema,
        )
        content = data["choices"][0]["message"]["content"]
        parsed = self._extract_json(content)
        return schema.model_validate(parsed)


def get_client() -> LLMClient:
    """Return the SiliconFlow client. The sole LLM provider."""
    return SiliconFlowClient()
