"""Fire-and-forget work that must not delay the response (L3), with a clean shutdown.

    spawn(coro, "save_rows")   schedule on the running loop; failures are logged by type only
    await drain()              on shutdown: wait (bounded) for pending work

In-process only. A crash loses pending work; the query store marks such answers by leaving
rows_status = 'saving'. Moving this to a real queue (arq / Celery) is a drop-in for `spawn`.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task[Any]] = set()


def _done(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if not task.cancelled() and (e := task.exception()) is not None:
        # Type only: messages can contain SQL or row values.
        log.warning("background task %s failed: %s", task.get_name(), type(e).__name__)


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)  # keep a strong reference until it finishes
    task.add_done_callback(_done)


async def drain(wait_seconds: float = 10.0) -> None:
    if _tasks:
        await asyncio.wait(set(_tasks), timeout=wait_seconds)
