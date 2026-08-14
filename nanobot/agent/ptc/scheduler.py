"""Ordered, bounded scheduler for tool calls originating in PTC programs."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Entry:
    safe: bool
    execute: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]


class PtcToolScheduler:
    """Run safe calls concurrently while preserving exclusive barriers."""

    def __init__(self, max_parallel: int) -> None:
        self.max_parallel = max_parallel
        self._queue: deque[_Entry] = deque()
        self._active: set[asyncio.Task[None]] = set()
        self._wakeup = asyncio.Event()
        self._closed = False
        self._abort_error: BaseException | None = None
        self._driver = asyncio.create_task(self._drive())
        self.peak_parallel = 0

    async def submit(self, *, safe: bool, execute: Callable[[], Awaitable[Any]]) -> Any:
        if self._closed:
            if self._abort_error is not None:
                raise self._abort_error
            raise RuntimeError("PTC scheduler is closed")
        future = asyncio.get_running_loop().create_future()
        self._queue.append(_Entry(safe=safe, execute=execute, future=future))
        self._wakeup.set()
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            self._wakeup.set()
            raise

    async def _run_entry(self, entry: _Entry) -> None:
        if entry.future.cancelled():
            return
        try:
            result = await entry.execute()
        except BaseException as exc:
            if not entry.future.done():
                entry.future.set_exception(exc)
        else:
            if not entry.future.done():
                entry.future.set_result(result)

    def _start_safe(self, entry: _Entry) -> None:
        task = asyncio.create_task(self._run_entry(entry))
        self._active.add(task)
        self.peak_parallel = max(self.peak_parallel, len(self._active))
        task.add_done_callback(lambda done: (self._active.discard(done), self._wakeup.set()))

    async def _drive(self) -> None:
        try:
            while not self._closed or self._queue or self._active:
                progressed = False
                while self._queue and self._queue[0].future.cancelled():
                    self._queue.popleft()
                    progressed = True
                while (
                    self._queue
                    and self._queue[0].safe
                    and len(self._active) < self.max_parallel
                ):
                    self._start_safe(self._queue.popleft())
                    progressed = True
                if self._queue and not self._queue[0].safe and not self._active:
                    entry = self._queue.popleft()
                    if not entry.future.cancelled():
                        await self._run_entry(entry)
                    progressed = True
                    continue
                if progressed:
                    continue
                self._wakeup.clear()
                if self._closed and not self._queue and not self._active:
                    break
                await self._wakeup.wait()
        except asyncio.CancelledError:
            raise
        finally:
            while self._queue:
                entry = self._queue.popleft()
                if not entry.future.done():
                    entry.future.set_exception(
                        self._abort_error or RuntimeError("PTC scheduler closed")
                    )

    def abort(self, error: BaseException) -> None:
        self._abort_error = error
        self._closed = True
        while self._queue:
            entry = self._queue.popleft()
            if not entry.future.done():
                entry.future.set_exception(error)
        self._wakeup.set()

    async def close(self, *, cancel_active: bool = False) -> None:
        self._closed = True
        if cancel_active:
            for task in self._active:
                task.cancel()
            self._driver.cancel()
        self._wakeup.set()
        await asyncio.gather(self._driver, *self._active, return_exceptions=True)
