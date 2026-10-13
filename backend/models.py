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
from dataclasses import dataclass, field
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

from backend.config import DEFAULT_PRICES_PER_TOKEN, Settings, thinking_key
from backend.schemas import ModelCall

log = logging.getLogger("leaf.models")

T = TypeVar("T", bound=BaseModel)

# NVIDIA's model cards recommend temperature 1.0 / top_p 0.95 for Nemotron 3.x.
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 0.95


class ModelError(RuntimeError):
    """A model call failed in a way the caller should handle (fallback or 502)."""


@dataclass
class ToolCall:
    """One tool call requested by a model. `arguments` is the raw JSON string it wrote."""

    id: str
    name: str
    arguments: str


@dataclass
class CallResult:
    text: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    def to_trace(self, step: str, note: str = "") -> ModelCall:
        return ModelCall(
            step=step,
            model=self.model,
            latency_ms=self.latency_ms,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=self.cost_usd,
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

    async def complete_tools(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
        thinking: bool | None = True,
        max_tokens: int = 4096,
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
        # Dollars per token, (input, output). Refreshed from /models at startup.
        self.prices: dict[str, tuple[float, float]] = dict(DEFAULT_PRICES_PER_TOKEN)

    def _client(self, base_url: str) -> AsyncOpenAI:
        if base_url not in self._clients:
            self._clients[base_url] = AsyncOpenAI(
                base_url=base_url,
                api_key=self.settings.nebius_api_key,
                timeout=self.settings.request_timeout_s,
                max_retries=2,  # SDK retries 429/5xx/connection errors with backoff
            )
        return self._clients[base_url]

    async def load_prices(self) -> None:
        """Use Token Factory's own per-token prices (GET /models?verbose=true) when listed."""
        try:
            models = await self.list_models()
        except Exception as exc:  # noqa: BLE001 - prices are best-effort; defaults remain
            log.warning("could not load prices: %s", exc)
            return
        for m in models:
            p = m.get("pricing") or {}
            try:
                self.prices[m["id"]] = (float(p["prompt"]), float(p["completion"]))
            except (KeyError, TypeError, ValueError):
                continue

    def cost(self, model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float:
        price_in, price_out = self.prices.get(model, (0.0, 0.0))
        return round((prompt_tokens or 0) * price_in + (completion_tokens or 0) * price_out, 6)

    async def _send(
        self, model: str, kwargs: dict[str, Any], thinking: bool | None, json_mode: bool
    ):
        """One request with the provider-quirk handling shared by every call type."""
        client = self._client(self.settings.base_url_for(model))
        for _attempt in range(3):
            body = dict(kwargs, model=model)
            extra_body: dict[str, Any] = {}
            use_template = thinking is not None and model not in self._no_template_kwargs
            if use_template:
                # Nemotron templates read enable_thinking, Kimi reads thinking (config.py).
                extra_body["chat_template_kwargs"] = {thinking_key(model): thinking}
            use_json = json_mode and model not in self._no_json_mode
            if use_json:
                body["response_format"] = {"type": "json_object"}
            start = time.perf_counter()
            try:
                resp = await client.chat.completions.create(**body, extra_body=extra_body or None)
            except BadRequestError as exc:
                msg = str(exc).lower()
                # Only learn from errors that name the parameter, so an unrelated 400
                # cannot switch a feature off for the life of the process.
                if use_template and any(k in msg for k in ("chat_template", "enable_thinking")):
                    log.info("%s rejects chat_template_kwargs; not sending it again", model)
                    self._no_template_kwargs.add(model)
                    continue
                if use_json and any(
                    k in msg for k in ("response_format", "json_object", "json mode", "guided_json")
                ):
                    log.info("%s rejects response_format; not sending it again", model)
                    self._no_json_mode.add(model)
                    continue
                raise ModelError(f"{model}: bad request: {exc}") from exc
            except NotFoundError as exc:
                raise ModelError(f"{model}: not found at this base URL") from exc
            except (APITimeoutError, APIConnectionError) as exc:
                raise ModelError(f"{model}: network error: {exc}") from exc
            except APIStatusError as exc:
                raise ModelError(f"{model}: HTTP {exc.status_code}") from exc
            return resp, int((time.perf_counter() - start) * 1000)
        raise ModelError(f"{model}: gave up after parameter retries")

    def _result(self, model: str, resp, latency: int, text: str, **extra) -> CallResult:
        usage = resp.usage
        p_in = getattr(usage, "prompt_tokens", None)
        p_out = getattr(usage, "completion_tokens", None)
        result = CallResult(
            text=text,
            model=model,
            latency_ms=latency,
            prompt_tokens=p_in,
            completion_tokens=p_out,
            cost_usd=self.cost(model, p_in, p_out),
            **extra,
        )
        log.info(
            "model_call model=%s latency_ms=%d in=%s out=%s cost=%.5f tools=%d",
            model,
            latency,
            p_in,
            p_out,
            result.cost_usd or 0.0,
            len(result.tool_calls),
        )
        return result

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
        tokens = max_tokens
        for doubled in (False, True):
            kwargs = {
                "messages": messages,
                "max_tokens": tokens,
                "temperature": temperature,
                "top_p": top_p,
            }
            resp, latency = await self._send(model, kwargs, thinking, json_mode)
            choice = resp.choices[0] if resp.choices else None
            text = strip_think((choice.message.content or "") if choice else "")
            # JSON cut off by the token limit is as useless as an empty reply: reasoning models
            # can spend most of max_tokens thinking. (Truncated prose is still readable.)
            truncated = json_mode and getattr(choice, "finish_reason", None) == "length"
            if text.strip() and not truncated:
                return self._result(model, resp, latency, text)
            # Reasoning tokens count toward max_tokens; give it more room once.
            if doubled or tokens >= 16000:
                why = "truncated at max_tokens" if text.strip() else "empty content"
                raise ModelError(f"{model}: {why} (reasoning may have used max_tokens)")
            tokens *= 2
        raise ModelError(f"{model}: no usable reply")  # pragma: no cover - loop always returns

    async def complete_tools(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] = "auto",
        thinking: bool | None = True,
        max_tokens: int = 4096,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
    ) -> CallResult:
        """A chat turn that may request tool calls (never combined with JSON mode: with both,
        Nemotron stops calling tools and writes JSON text instead; probed Oct 2026)."""
        tokens = max_tokens
        for doubled in (False, True):
            kwargs = {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "max_tokens": tokens,
                "temperature": temperature,
                "top_p": top_p,
            }
            resp, latency = await self._send(model, kwargs, thinking, json_mode=False)
            choice = resp.choices[0] if resp.choices else None
            msg = choice.message if choice else None
            calls = [
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
                for tc in ((msg.tool_calls if msg else None) or [])
                if tc.function and tc.function.name
            ]
            text = strip_think((msg.content or "") if msg else "")
            finish = getattr(choice, "finish_reason", None)
            # Tool arguments cut off at max_tokens are unusable JSON; so is a silent turn.
            if (calls or text.strip()) and finish != "length":
                extras = msg.model_dump() if msg is not None and hasattr(msg, "model_dump") else {}
                reasoning = str(extras.get("reasoning_content") or extras.get("reasoning") or "")
                return self._result(
                    model,
                    resp,
                    latency,
                    text,
                    reasoning=reasoning[:4000],
                    tool_calls=calls,
                    finish_reason=finish,
                )
            if doubled or tokens >= 16000:
                raise ModelError(
                    f"{model}: no tool call or text (reasoning may have used max_tokens)"
                )
            tokens *= 2
        raise ModelError(f"{model}: no usable reply")  # pragma: no cover - loop always returns

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
    decoder = json.JSONDecoder()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    starts = [m.start() for m in re.finditer(r"\{", text)]
    if not starts:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    for n, start in enumerate(starts):
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # The first object may be followed by chatter. A later one must be the final thing
        # in the reply: otherwise it is a fragment of a broken outer object, and returning
        # it would pass off a partial answer as a whole one.
        if n == 0 or not text[end:].strip().strip("`").strip():
            return obj
    raise json.JSONDecodeError("no JSON object found", text, 0)


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
            log.warning("%s call failed: %s", step, exc)  # detail stays server-side
            trace.append(ModelCall(step=step, model=model, latency_ms=0, note="failed"))
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
    brace = bad.find("{")  # skip reasoning prose so the cut never drops the JSON itself
    body = bad[brace:] if brace != -1 else bad
    prompt = (
        "Rewrite the text below as ONE valid JSON object matching this JSON Schema. "
        "Output only the JSON.\n\nSchema:\n"
        f"{json.dumps(schema.model_json_schema())}\n\nText:\n{body[:6000]}"
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
