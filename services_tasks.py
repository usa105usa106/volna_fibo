from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def _drain_owned(future: asyncio.Future[Any]) -> Any:
    # A second cancellation must not cancel cleanup or the to_thread wrapper.
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()


async def gather_owned(*coroutines: Awaitable[Any]) -> list[Any]:
    """Cancel and drain siblings on failure, preserving the original exception."""
    tasks = [asyncio.ensure_future(coro) for coro in coroutines]
    group = asyncio.gather(*tasks)
    try:
        return await asyncio.shield(group)
    except BaseException:
        for task in tasks:
            task.cancel()
        await _drain_owned(asyncio.gather(*tasks, return_exceptions=True))
        if group.done() and not group.cancelled():
            group.exception()
        raise


async def run_cpu(call: Callable[..., T], *args: Any) -> T:
    """Keep the event loop responsive; cancellation drains the owned worker.

    Python cannot stop a running thread. Wait for this bounded detector call before
    releasing its slot or allowing Reset to replace the detector/network objects.
    """
    task = asyncio.create_task(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_owned(asyncio.gather(task, return_exceptions=True))
        raise
