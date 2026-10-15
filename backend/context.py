"""Per-request context: the trace id, the task each model call serves, and the actor.

Context variables follow async code into every coroutine a request starts, so the meter can
attribute a model call without threading ids through every function signature.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RequestContext:
    """Mutable on purpose: an endpoint sets `actor` and the middleware reads it afterwards
    (FastAPI runs the endpoint in a copy of the middleware's context)."""

    trace_id: str = field(default_factory=lambda: secrets.token_hex(8))
    actor: str = ""  # "g:<garden id>" when the request opened a garden
    visitor: str = ""  # salted hash of the caller's address, for the active-user count
    model_calls: int = 0


REQUEST: ContextVar[RequestContext | None] = ContextVar("leaf_request", default=None)
TASK: ContextVar[str] = ContextVar("leaf_task", default="unlabeled")


def current_trace_id() -> str:
    ctx = REQUEST.get()
    return ctx.trace_id if ctx else ""


@contextmanager
def task_scope(task: str) -> Iterator[None]:
    """Model calls inside this block are metered against `task` (a key of config.ROUTES)."""
    token = TASK.set(task)
    try:
        yield
    finally:
        TASK.reset(token)


@contextmanager
def request_scope(ctx: RequestContext | None = None) -> Iterator[RequestContext]:
    """A trace for work outside an HTTP request (jobs, scripts)."""
    ctx = ctx or RequestContext()
    token = REQUEST.set(ctx)
    try:
        yield ctx
    finally:
        REQUEST.reset(token)
