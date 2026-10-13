"""The investigator: Nemotron 3 Ultra calling read-only tools, with every limit enforced in code.

    brief -> [model turn -> tool calls (run concurrently) -> results]* -> finish | ask_user

Limits (Settings.deep_*): tool calls that count toward the budget, a dollar budget, and a wall
clock. When any limit is reached the loop tells the model so and offers only `finish`. A failed
tool becomes a "skipped" result the model can see; a failed model turn falls back from Ultra to
Super; if the loop still cannot finish, AgentError tells the caller to fall back to Quick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from backend.case import Case, StepRecord
from backend.config import Settings
from backend.models import LLM, CallResult, ModelError, ToolCall
from backend.prompts import AGENT_SYSTEM, sanitize
from backend.schemas import ModelCall
from backend.search import Searcher
from backend.tools import TOOLS, Emit, ToolContext, ToolError, ToolOutcome, tool_schemas

log = logging.getLogger("leaf.agent")

TERMINAL = ("finish", "ask_user")
FORCE_FINISH = {"type": "function", "function": {"name": "finish"}}
MIN_SECONDS_TO_FINISH = 20  # leave room for the finishing turn and the care plan
BUDGET_SHARE_FOR_LOOP = 0.8  # the rest of the dollar budget pays for the plan and checks


class AgentError(Exception):
    """The investigator could not finish; the caller falls back to Quick."""


@dataclass
class AgentResult:
    kind: Literal["finish", "ask"]
    questions: list[str] = field(default_factory=list)
    limit: str = ""  # which limit forced the finish, if any


@dataclass
class _Prepared:
    call: ToolCall
    record: StepRecord
    args: Any = None  # validated arguments, or None when the call was refused
    data: dict[str, Any] = field(default_factory=dict)  # the tool's result, for the model


def case_brief(case: Case, settings: Settings) -> str:
    """The first user message: history, photos and what the first look saw, all as data."""
    photos = [
        {
            "photo_id": p.id,
            "label": p.label,
            "first_look": p.observation.model_dump() if p.observation else None,
        }
        for p in case.photos
    ]
    place = case.region or ("browser location" if case.lat is not None else "unknown")
    return (
        f"<gardener_notes>{sanitize(case.notes) or 'none'}</gardener_notes>\n"
        f"<region>{sanitize(place, 100)}</region>\n"
        f"<crop>{sanitize(case.crop or 'unknown', 60)}</crop>\n"
        f"<photos>{json.dumps(photos)}</photos>\n"
        f"Budget: {settings.deep_max_calls} tool calls (update_differential, ask_user and "
        f"finish are free), about {settings.deep_wall_s} seconds."
    )


def _assistant_message(res: CallResult) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": res.text or ""}
    if res.tool_calls:
        msg["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in res.tool_calls
        ]
    return msg


def _tool_message(call: ToolCall, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False)
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": f'<tool_result name="{call.name}">{body}</tool_result>',
    }


def _short_errors(exc: ValidationError) -> str:
    parts = [f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()]
    return "; ".join(parts[:4])


def _public_args(args: Any) -> dict[str, Any]:
    """Arguments safe to show the gardener: every string sanitised and trimmed, lists capped
    (values are cleaned one by one; cutting serialised JSON would break it)."""

    def clean(v: Any) -> Any:
        if isinstance(v, str):
            return sanitize(v, 200)
        if isinstance(v, dict):
            return {k: clean(x) for k, x in list(v.items())[:12]}
        if isinstance(v, list):
            return [clean(x) for x in v[:8]]
        return v

    return clean(args.model_dump(exclude={"reason"}))


class Investigator:
    def __init__(
        self,
        llm: LLM,
        searcher: Searcher,
        settings: Settings,
        vision_model: str | None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.llm, self.searcher, self.settings = llm, searcher, settings
        self.vision_model = vision_model
        self.clock = clock

    # ------------------------------------------------------------------ public

    async def run(self, case: Case, emit: Emit) -> AgentResult:
        s = self.settings
        start = self.clock()
        ctx = ToolContext(self.llm, self.searcher, s, case, self.vision_model, emit)
        if not case.messages:
            case.messages = [
                {"role": "system", "content": AGENT_SYSTEM},
                {"role": "user", "content": case_brief(case, s)},
            ]
        limit, nudged, refused_turns, last_text = "", False, 0, ""
        for _turn in range(s.deep_max_calls + 8):
            remaining = s.deep_wall_s - (self.clock() - start)
            if not limit:
                limit = self._limit_reached(case, remaining)
                if limit:
                    await emit({"type": "limit", "limit": limit})
                    case.messages.append(
                        {
                            "role": "user",
                            "content": f"The {limit} is spent. Call finish now with your best "
                            "differential and the evidence ids you have.",
                        }
                    )
            res = await self._turn(case, forced=bool(limit), remaining=remaining, emit=emit)
            case.messages.append(_assistant_message(res))
            if res.text.strip():
                last_text = res.text.strip()
            if not res.tool_calls:
                if limit:
                    break
                if nudged:  # prose twice: it has probably concluded, so make it call finish
                    limit = "no tool call"
                    await emit({"type": "limit", "limit": limit})
                    case.messages.append(
                        {"role": "user", "content": "Record that conclusion now by calling finish."}
                    )
                    continue
                nudged = True
                case.messages.append(
                    {"role": "user", "content": "Use a tool, or call finish if you are done."}
                )
                continue
            steps_before = len(case.steps)
            outcome = await self._run_calls(res.tool_calls, ctx, forced=bool(limit), start=start)
            if outcome is not None:
                outcome.limit = limit
                return outcome
            # Two turns in a row where every call was refused: stop wasting turns.
            new_steps = case.steps[steps_before:]
            refused_turns = (
                refused_turns + 1 if new_steps and not any(st.ok for st in new_steps) else 0
            )
            if refused_turns >= 2 and not limit:
                limit = "no progress"
                await emit({"type": "limit", "limit": limit})
                case.messages.append(
                    {"role": "user", "content": "Those calls were refused. Call finish now."}
                )
        if case.differential:  # ran out of turns: finish with the last recorded differential
            log.warning("investigator stopped without finish; using last differential")
            if not case.summary and last_text:
                case.summary = sanitize(last_text, 800)
            await emit({"type": "limit", "limit": "turn limit"})
            return AgentResult("finish", limit=limit or "turn limit")
        raise AgentError("the investigator did not reach a diagnosis")

    async def resume(self, case: Case, answers: list[str], emit: Emit) -> AgentResult:
        """Continue a case that stopped at ask_user, feeding the gardener's answers back."""
        if case.status != "needs_answers" or not case.pending_call_id:
            raise AgentError("this case is not waiting for answers")
        pairs = [
            {"question": q, "answer": sanitize(a, 400).strip() or "(no answer)"}
            for q, a in zip(case.questions, answers + [""] * len(case.questions), strict=False)
        ]
        call = ToolCall(id=case.pending_call_id, name="ask_user", arguments="{}")
        case.messages.append(_tool_message(call, {"answers": pairs}))
        case.status, case.pending_call_id = "running", None
        await emit({"type": "answers", "answers": pairs})
        return await self.run(case, emit)

    # ------------------------------------------------------------------ limits and turns

    def _limit_reached(self, case: Case, remaining: float) -> str:
        s = self.settings
        if case.calls_used >= s.deep_max_calls:
            return "tool-call budget"
        if case.cost_usd >= s.deep_budget_usd * BUDGET_SHARE_FOR_LOOP:
            return "cost budget"
        if remaining < MIN_SECONDS_TO_FINISH:
            return "time budget"
        return ""

    async def _turn(self, case: Case, forced: bool, remaining: float, emit: Emit) -> CallResult:
        s = self.settings
        names = ["finish"] if forced else [n for n in TOOLS if not (n == "ask_user" and case.asked)]
        tools = tool_schemas(names)
        # A named tool_choice is the strongest way to force finish; if a server rejects it,
        # offering finish as the only tool with "auto" is the fallback.
        choices: list[Any] = [FORCE_FINISH, "auto"] if forced else ["auto"]
        errors = []
        for i, model in enumerate([s.reason_model, s.fallback_model]):
            for choice in choices:
                budget = max(5.0, remaining - 3) if remaining > 0 else 10.0
                try:
                    res = await asyncio.wait_for(
                        self.llm.complete_tools(
                            model=model,
                            messages=case.messages,
                            tools=tools,
                            tool_choice=choice,
                            thinking=True,
                            max_tokens=4096,
                        ),
                        timeout=budget,
                    )
                except (ModelError, TimeoutError) as exc:
                    log.warning("investigator turn failed on %s: %s", model, exc)
                    case.add_trace(
                        ModelCall(step="investigate", model=model, latency_ms=0, note="failed")
                    )
                    errors.append(str(exc))
                    continue
                case.add_trace(res.to_trace("investigate", "fallback" if i else ""))
                await emit(
                    {
                        "type": "model",
                        "model": model,
                        "ms": res.latency_ms,
                        "cost_usd": res.cost_usd,
                        "thinking": sanitize(res.reasoning, 600),
                    }
                )
                return res
        raise AgentError(f"investigator turn failed: {' | '.join(errors)[:300]}")

    # ------------------------------------------------------------------ tool calls

    async def _run_calls(
        self, calls: list[ToolCall], ctx: ToolContext, forced: bool, start: float
    ) -> AgentResult | None:
        case = ctx.case
        terminal = [c for c in calls if c.name in TERMINAL]
        others = [c for c in calls if c.name not in TERMINAL]
        seen: set[tuple[str, str]] = set()
        prepared: list[_Prepared] = []
        for call in others:
            p = self._prepare(call, ctx, forced)
            key = (call.name, call.arguments.strip())
            if p.args is not None and key in seen:  # identical call twice in one turn
                if TOOLS[call.name].counts:
                    case.calls_used -= 1  # refund: it never runs
                p.args, p.record.ok, p.record.skipped = None, False, "duplicate call in this turn"
            seen.add(key)
            prepared.append(p)
        for p in prepared:
            if p.args is not None:
                await ctx.emit(
                    {"type": "step", **p.record.model_dump(include={"n", "tool", "reason", "args"})}
                )
        await asyncio.gather(
            *(self._execute(p, ctx, start) for p in prepared if p.args is not None)
        )
        for p in prepared:
            case.steps.append(p.record)
            payload = (
                {"evidence_id": p.record.evidence_id, "result": p.data}
                if p.record.ok
                else {"skipped": p.record.skipped}
            )
            case.messages.append(_tool_message(p.call, payload))
            await ctx.emit({"type": "step_done", **p.record.model_dump(exclude={"args"})})

        if not terminal:
            return None
        chosen = next((c for c in terminal if c.name == "finish"), terminal[0])
        for extra in terminal:
            if extra is not chosen:
                case.messages.append(
                    _tool_message(extra, {"skipped": "only one of finish/ask_user"})
                )
        if forced and chosen.name != "finish":
            case.messages.append(_tool_message(chosen, {"skipped": "budget spent: call finish"}))
            return None
        p = self._prepare(chosen, ctx, forced=False)
        if p.args is None:
            case.steps.append(p.record)
            case.messages.append(_tool_message(chosen, {"skipped": p.record.skipped}))
            return None
        await self._execute(p, ctx, start)
        case.steps.append(p.record)
        if not p.record.ok:
            case.messages.append(_tool_message(chosen, {"skipped": p.record.skipped}))
            return None
        if chosen.name == "ask_user":
            case.asked += 1
            case.status, case.pending_call_id = "needs_answers", chosen.id
            case.questions = p.data["questions"]
            await ctx.emit({"type": "questions", "questions": case.questions})
            return AgentResult("ask", questions=case.questions)
        case.messages.append(_tool_message(chosen, {"result": "finished"}))
        await ctx.emit(
            {
                "type": "differential",
                "final": True,
                "hypotheses": [h.model_dump() for h in case.differential],
            }
        )
        return AgentResult("finish")

    def _prepare(self, call: ToolCall, ctx: ToolContext, forced: bool) -> _Prepared:
        """Parse, validate and budget-check one call before anything runs."""
        case = ctx.case
        case.step_seq += 1  # unique even when several calls are prepared in one turn
        record = StepRecord(n=case.step_seq, tool=call.name)
        prepared = _Prepared(call=call, record=record)
        spec = TOOLS.get(call.name)
        if spec is None:
            usable = ", ".join(n for n in TOOLS if not (n == "ask_user" and case.asked))
            record.ok = False
            record.skipped = f"unknown tool {sanitize(call.name, 40)!r}; use one of: {usable}"
            return prepared
        try:
            raw = json.loads(call.arguments or "{}")
            args = spec.args.model_validate(raw if isinstance(raw, dict) else {})
        except json.JSONDecodeError:
            record.ok, record.skipped = False, "arguments were not valid JSON"
            return prepared
        except ValidationError as exc:
            record.ok, record.skipped = False, f"invalid arguments: {_short_errors(exc)}"
            return prepared
        record.reason = sanitize(args.reason, 300)
        record.args = _public_args(args)
        if forced and call.name != "finish":
            record.ok, record.skipped = False, "budget spent: call finish"
            return prepared
        if spec.counts:
            if case.calls_used >= self.settings.deep_max_calls:
                record.ok, record.skipped = False, "tool-call budget spent"
                return prepared
            case.calls_used += 1
        prepared.args = args
        return prepared

    async def _execute(self, p: _Prepared, ctx: ToolContext, start: float) -> None:
        spec = TOOLS[p.call.name]
        remaining = self.settings.deep_wall_s - (self.clock() - start)
        t0 = time.perf_counter()
        try:
            out: ToolOutcome = await asyncio.wait_for(
                spec.handler(p.args, ctx), timeout=max(3.0, min(spec.timeout_s, remaining))
            )
        except ToolError as exc:
            p.record.ok, p.record.skipped = False, sanitize(str(exc), 300)
        except TimeoutError:
            p.record.ok, p.record.skipped = False, "the tool took too long"
        except Exception as exc:  # noqa: BLE001 - any tool failure degrades, never crashes
            log.warning("tool %s failed: %s", p.call.name, exc)
            p.record.ok, p.record.skipped = False, "the tool failed"
        else:
            p.record.summary = sanitize(out.summary, 300)
            p.data = out.data
            if out.evidence:
                ev = ctx.case.add_evidence(
                    p.call.name,
                    p.record.summary,
                    data=out.data,
                    source_ids=out.source_ids,
                    photo_id=out.photo_id,
                )
                p.record.evidence_id = ev.id
        p.record.ms = int((time.perf_counter() - t0) * 1000)
