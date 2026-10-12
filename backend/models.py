"""Nebius Token Factory client: the ONLY place Leaf talks to a model.

- OpenAI-compatible API, so we use the official `openai` SDK with a custom base URL.
- Every call returns a CallResult with model, latency and tokens for the trace.
- Defensive about provider quirks: drops unsupported params and retries once,
  strips inline <think> blocks, and treats empty content as an error to fall back on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    NotFoundError,
)
from pydantic import BaseModel, ValidationError

from backend.config import Settings
from backend.schemas import ModelCall

log = logging.getLogger("leaf.models")

T = TypeVar("T", bound=BaseModel)

# NVIDIA's model cards recommend temperature 1.0 / top_p 0.95 for Nemotron 3.x.
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 0.95


class ModelError(RuntimeError):
    """A model call failed in a way the caller should handle (fallback or 502)."""


@dataclass
class CallResult:
    text: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_trace(self, step: str, note: str = "") -> ModelCall:
        return ModelCall(
            step=step,
            model=self.model,
            latency_ms=self.latency_ms,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            note=note,
        )


class LLM(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool = False,
        thinking: bool | None = None,
        max_tokens: int = 2048,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> CallResult: ...

    async def list_models(self) -> list[dict[str, Any]]: ...


class TokenFactory:
    """Async client for Nebius Token Factory with per-model base URLs."""

    def __init__(self, settings: Settings):
        if not settings.nebius_api_key:
            raise ModelError("NEBIUS_API_KEY is not set")
        self.settings = settings
        self._clients: dict[str, AsyncOpenAI] = {}
        # Learned at runtime: params a model rejected, so we stop sending them.
        self._no_template_kwargs: set[str] = set()
        self._no_json_mode: set[str] = set()

    def _client(self, base_url: str) -> AsyncOpenAI:
        if base_url not in self._clients:
            self._clients[base_url] = AsyncOpenAI(
                base_url=base_url,
                api_key=self.settings.nebius_api_key,
                timeout=self.settings.request_timeout_s,
                max_retries=2,  # SDK retries 429/5xx/connection errors with backoff
            )
        return self._clients[base_url]

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        json_mode: bool = False,
        thinking: bool | None = None,
        max_tokens: int = 2048,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> CallResult:
        client = self._client(self.settings.base_url_for(model))
        tokens, doubled = max_tokens, False
        for _attempt in range(4):
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": tokens,
                "temperature": temperature,
                "top_p": top_p,
            }
            extra_body: dict[str, Any] = {}
            use_template = thinking is not None and model not in self._no_template_kwargs
            if use_template:
                # Nemotron chat templates read enable_thinking (see NVIDIA model cards).
                extra_body["chat_template_kwargs"] = {"enable_thinking": thinking}
            use_json = json_mode and model not in self._no_json_mode
            if use_json:
                kwargs["response_format"] = {"type": "json_object"}

            start = time.perf_counter()
            try:
                resp = await client.chat.completions.create(**kwargs, extra_body=extra_body or None)
            except BadRequestError as exc:
                msg = str(exc).lower()
                if use_template and ("chat_template" in msg or "extra" in msg):
                    self._no_template_kwargs.add(model)
                    continue
                if use_json and ("response_format" in msg or "json" in msg):
                    self._no_json_mode.add(model)
                    continue
                raise ModelError(f"{model}: bad request: {exc}") from exc
            except NotFoundError as exc:
                raise ModelError(f"{model}: not found at this base URL") from exc
            except (APITimeoutError, APIConnectionError) as exc:
                raise ModelError(f"{model}: network error: {exc}") from exc
            except APIStatusError as exc:
                raise ModelError(f"{model}: HTTP {exc.status_code}") from exc

            latency = int((time.perf_counter() - start) * 1000)
            choice = resp.choices[0] if resp.choices else None
            text = strip_think((choice.message.content or "") if choice else "")
            if not text.strip():
                # Reasoning tokens count toward max_tokens; give it more room once.
                if not doubled and tokens < 16000:
                    tokens, doubled = tokens * 2, True
                    continue
                raise ModelError(f"{model}: empty content (reasoning may have used max_tokens)")
            usage = resp.usage
            result = CallResult(
                text=text,
                model=model,
                latency_ms=latency,
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
            )
            log.info(
                "model_call model=%s latency_ms=%d in=%s out=%s",
                model,
                latency,
                result.prompt_tokens,
                result.completion_tokens,
            )
            return result
        raise ModelError(f"{model}: gave up after parameter retries")

    async def list_models(self) -> list[dict[str, Any]]:
        """Raw model list with modality info (GET /v1/models?verbose=true)."""
        url = self.settings.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {self.settings.nebius_api_key}"}
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(url, params={"verbose": "true"}, headers=headers)
            r.raise_for_status()
            return r.json().get("data", [])


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove inline reasoning blocks some models put in `content`."""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # opening tag was cut off
        text = text.split("</think>", 1)[1]
    return text.strip()


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a model reply (tolerates code fences and chatter)."""
    text = strip_think(text)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    return json.loads(text[start : end + 1])


async def complete_json(
    llm: LLM,
    *,
    step: str,
    models: list[str],
    messages: list[dict[str, Any]],
    schema: type[T],
    trace: list[ModelCall],
    thinking: bool | None = None,
    max_tokens: int = 2048,
    temperature: float = DEFAULT_TEMPERATURE,
    repair_model: str | None = None,
) -> T:
    """Call models in order until one returns JSON that validates against `schema`.

    If a reply is malformed, one cheap repair call (repair_model) tries to fix it before
    moving on to the next model. Every attempt is recorded in `trace`.
    """
    errors: list[str] = []
    for i, model in enumerate(models):
        note = "fallback" if i else ""
        try:
            res = await llm.complete(
                model=model,
                messages=messages,
                json_mode=True,
                thinking=thinking,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except ModelError as exc:
            trace.append(ModelCall(step=step, model=model, latency_ms=0, note=f"failed: {exc}"))
            errors.append(str(exc))
            continue
        try:
            parsed = schema.model_validate(extract_json(res.text))
            trace.append(res.to_trace(step, note))
            return parsed
        except (json.JSONDecodeError, ValidationError) as exc:
            trace.append(res.to_trace(step, f"{note} invalid JSON".strip()))
            errors.append(f"{model}: invalid JSON: {exc}")
            if repair_model:
                fixed = await _repair(llm, repair_model, res.text, schema, step, trace)
                if fixed is not None:
                    return fixed
    raise ModelError(f"{step}: all models failed: {' | '.join(errors)[:500]}")


async def _repair(
    llm: LLM, model: str, bad: str, schema: type[T], step: str, trace: list[ModelCall]
) -> T | None:
    prompt = (
        "Rewrite the text below as ONE valid JSON object matching this JSON Schema. "
        "Output only the JSON.\n\nSchema:\n"
        f"{json.dumps(schema.model_json_schema())}\n\nText:\n{bad[:6000]}"
    )
    try:
        res = await llm.complete(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            json_mode=True,
            thinking=False,
            max_tokens=2048,
            temperature=0.2,
        )
        parsed = schema.model_validate(extract_json(res.text))
        trace.append(res.to_trace(step, "json repair"))
        return parsed
    except (ModelError, json.JSONDecodeError, ValidationError):
        return None


async def gather_limited(coros: list, limit: int = 4) -> list:
    """Run coroutines concurrently with a cap (keeps us under Token Factory/Tavily limits)."""
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
