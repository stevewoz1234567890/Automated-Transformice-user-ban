"""Session-wide step tracing (``BOT_DEBUG_TRACE``): monotonic ``#N`` lines like a debugger single-step.

* With trace **off**: ``trace_step`` emits DEBUG only when the root/file handler level allows it (quiet).
* With trace **on**: each ``trace_step`` is INFO with ``[trace #N | phase | slot]`` so ``log.txt`` reads top-to-bottom as an ordered timeline.

Pair with ``BOT_LOG_LEVEL=DEBUG`` or ``BOT_DEBUG_TRACE_CONSOLE=true`` for stderr verbosity.
"""

from __future__ import annotations

import logging
import os
import threading


class _TraceSeq:
    __slots__ = ("_lock", "_n")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._n = 0

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    def reset(self) -> None:
        with self._lock:
            self._n = 0


_SEQ = _TraceSeq()


def _truthy(key: str) -> bool:
    return (os.environ.get(key) or "").strip().lower() in ("1", "true", "yes", "on")


def trace_enabled() -> bool:
    return _truthy("BOT_DEBUG_TRACE")


def reset_trace_session() -> None:
    """Call once per CLI session so trace IDs restart at 1."""
    _SEQ.reset()


def trace_step(
    logger: logging.Logger,
    phase: str,
    msg: str,
    *args: object,
    slot: str | None = None,
) -> None:
    """One chronological step on the global timeline (thread-safe sequence)."""
    slot_s = slot if slot is not None else "-"
    if trace_enabled():
        n = _SEQ.next()
        logger.info("[trace #%s | %s | slot=%s] " + msg, n, phase, slot_s, *args)
    elif logger.isEnabledFor(logging.DEBUG):
        logger.debug("[dbg %s | slot=%s] " + msg, phase, slot_s, *args)


def asyncio_trace_install() -> None:
    """Inside an async entrypoint: enable loop slow-callback warnings when ``BOT_ASYNCIO_DEBUG``."""
    if not _truthy("BOT_ASYNCIO_DEBUG"):
        return
    import asyncio

    loop = asyncio.get_running_loop()
    loop.set_debug(True)
    raw = (os.environ.get("BOT_ASYNCIO_SLOW_CALLBACK_SEC") or "").strip()
    try:
        dur = float(raw) if raw else 0.05
    except ValueError:
        dur = 0.05
    dur = max(0.01, min(dur, 10.0))
    loop.slow_callback_duration = dur
