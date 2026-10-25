"""Units of work: tie every model call made inside a block to one task, ticket, document or job.

    with gm.unit("ticket-4812", kind="support-reply"):
        client.messages.create(...)          # recorded with unit_id="ticket-4812"
    gm.outcome("ticket-4812", accepted=True, score=4.5)

The current unit lives in a context variable: it follows asyncio tasks and `asyncio.to_thread`, and a plain thread
when started with `gm.bind(fn)` (or `contextvars.copy_context().run`). Without a block, $GIUGGIAI_METER_UNIT (and
$GIUGGIAI_METER_UNIT_KIND) give the unit, handy for jobs launched by a scheduler with the task id in the environment.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import os

_CURRENT: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("giuggiai_meter_unit", default=None)


@contextlib.contextmanager
def unit(unit_id, kind: str | None = None):
    """Every call recorded inside the block belongs to `unit_id` (blocks nest: the innermost wins)."""
    token = _CURRENT.set((str(unit_id), kind))
    try:
        yield str(unit_id)
    finally:
        _CURRENT.reset(token)


def current() -> tuple[str | None, str | None]:
    """(unit_id, unit_kind) of the innermost block, else from the environment, else (None, None)."""
    v = _CURRENT.get()
    if v:
        return v
    env = os.environ.get("GIUGGIAI_METER_UNIT")
    return (env, os.environ.get("GIUGGIAI_METER_UNIT_KIND")) if env else (None, None)


def bind(fn):
    """Wrap `fn` so it runs with the caller's current unit, even in another thread (threading.Thread(target=bind(f)))."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def run(*a, **k):
        return ctx.run(fn, *a, **k)

    return run
