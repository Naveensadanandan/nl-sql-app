"""Minimal per-node logging for the graph: one line when a node starts, one when it ends.

    nlsql.graph INFO  q_1a2b3c4d  guard            start
    nlsql.graph INFO  q_1a2b3c4d  guard            ok    412ms  -> guard
    nlsql.graph ERROR q_1a2b3c4d  execute_sql      FAIL  80ms   ValueError: ...

Only the node name, question id, timing and the *names* of the state keys it updated are logged,
never values (no question text, SQL, or result rows). Set NLSQL_GRAPH_LOG=off to silence it.
"""

import asyncio
import functools
import logging
import os
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("nlsql.graph")

if not log.handlers:  # own handler, so it shows without touching the root/uvicorn config
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    log.addHandler(_handler)
    log.propagate = False
log.setLevel(
    logging.CRITICAL + 1 if os.getenv("NLSQL_GRAPH_LOG", "").lower() == "off" else logging.INFO
)


def _qid(state: Any) -> str:
    qid = state.get("question_id") if isinstance(state, dict) else None
    return str(qid)[:10] if qid else "-"


def _keys(update: Any) -> str:
    return ",".join(update) if isinstance(update, dict) and update else "(none)"


def logged(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a node so its start, end, duration and failures are logged. Keeps sync/async as is."""

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
            qid, t0 = _qid(state), time.perf_counter()
            log.info("%s  %-20s start", qid, name)
            try:
                update = await fn(state, *args, **kwargs)
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000
                log.error("%s  %-20s FAIL  %.0fms  %s: %s", qid, name, ms, type(exc).__name__, str(exc)[:200])
                raise
            log.info(
                "%s  %-20s ok    %.0fms  -> %s",
                qid,
                name,
                (time.perf_counter() - t0) * 1000,
                _keys(update),
            )
            return update

        return awrapper

    @functools.wraps(fn)
    def wrapper(state: Any, *args: Any, **kwargs: Any) -> Any:
        qid, t0 = _qid(state), time.perf_counter()
        log.info("%s  %-20s start", qid, name)
        try:
            update = fn(state, *args, **kwargs)
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000
            log.error("%s  %-20s FAIL  %.0fms  %s: %s", qid, name, ms, type(exc).__name__, str(exc)[:200])
            raise
        log.info(
            "%s  %-20s ok    %.0fms  -> %s",
            qid,
            name,
            (time.perf_counter() - t0) * 1000,
            _keys(update),
        )
        return update

    return wrapper
