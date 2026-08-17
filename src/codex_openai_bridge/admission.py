"""Bounded FIFO admission for generation request handlers."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

_SHUTDOWN_GRACE_SECONDS = 5.0


class AdmissionQueueTimeout(RuntimeError):
    """Raised when no generation slot becomes available by the queue deadline."""


class AdmissionShuttingDown(RuntimeError):
    """Raised when generation admission has stopped for application shutdown."""


class AdmissionLease:
    """One idempotently releasable generation slot."""

    def __init__(
        self,
        controller: AdmissionController,
        owner: asyncio.Task[object],
    ) -> None:
        self._controller = controller
        self._owner = owner
        self._released = False
        self._owner_done_callback: Callable[[asyncio.Task[object]], None] | None = None

    def _install_owner_done_callback(
        self,
        callback: Callable[[asyncio.Task[object]], None],
    ) -> None:
        if self._owner_done_callback is not None:
            raise RuntimeError("admission owner callback is already installed")
        self._owner_done_callback = callback
        self._owner.add_done_callback(callback)

    def release(self) -> None:
        """Release this slot exactly once."""
        if self._released:
            return
        self._controller._release(self, self._owner)
        if self._owner_done_callback is not None:
            self._owner.remove_done_callback(self._owner_done_callback)
            self._owner_done_callback = None
        self._released = True


@dataclass
class _Waiter:
    owner: asyncio.Task[object]
    result: asyncio.Future[AdmissionLease | None]
    admitted: AdmissionLease | None = None


class AdmissionController:
    """Small fail-closed FIFO controller with explicit slot ownership."""

    def __init__(self, *, max_in_flight: int, queue_wait_seconds: float) -> None:
        if type(max_in_flight) is not int or max_in_flight < 1:
            raise ValueError("max_in_flight must be a positive integer")
        if (
            isinstance(queue_wait_seconds, bool)
            or not isinstance(queue_wait_seconds, (int, float))
            or not math.isfinite(queue_wait_seconds)
            or queue_wait_seconds <= 0
        ):
            raise ValueError("queue_wait_seconds must be positive and finite")
        self._max_in_flight = max_in_flight
        self._queue_wait_seconds = float(queue_wait_seconds)
        self._accepting = True
        self._active: dict[asyncio.Task[object], AdmissionLease] = {}
        self._waiters: deque[_Waiter] = deque()
        self._drained = asyncio.Event()
        self._drained.set()
        self._shutdown_operation: asyncio.Task[None] | None = None

    @property
    def active_count(self) -> int:
        """Return the active lease count for deterministic tests."""
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        """Return the queued waiter count for deterministic tests."""
        return len(self._waiters)

    def _current_task(self) -> asyncio.Task[object]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("admission requires an asyncio task")
        return task

    def _admit(self, owner: asyncio.Task[object]) -> AdmissionLease:
        if owner in self._active or len(self._active) >= self._max_in_flight:
            raise RuntimeError("invalid admission state")
        lease = AdmissionLease(self, owner)
        self._active[owner] = lease
        self._drained.clear()

        def release_when_owner_finishes(_task: asyncio.Task[object]) -> None:
            lease.release()

        lease._install_owner_done_callback(release_when_owner_finishes)
        return lease

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass

    async def acquire(self) -> AdmissionLease:
        """Acquire one slot or fail at the bounded queue deadline."""
        owner = self._current_task()
        if owner in self._active or any(waiter.owner is owner for waiter in self._waiters):
            raise RuntimeError("task already has an admission request")
        if not self._accepting:
            raise AdmissionShuttingDown
        if not self._waiters and len(self._active) < self._max_in_flight:
            return self._admit(owner)

        result: asyncio.Future[AdmissionLease | None] = asyncio.get_running_loop().create_future()
        waiter = _Waiter(owner=owner, result=result)
        self._waiters.append(waiter)
        try:
            async with asyncio.timeout(self._queue_wait_seconds):
                lease = await result
            if lease is None:
                raise AdmissionShuttingDown
            return lease
        except TimeoutError:
            self._remove_waiter(waiter)
            if waiter.admitted is not None:
                waiter.admitted.release()
            raise AdmissionQueueTimeout from None
        except BaseException:
            self._remove_waiter(waiter)
            if waiter.admitted is not None:
                waiter.admitted.release()
            raise

    def _release(self, lease: AdmissionLease, owner: asyncio.Task[object]) -> None:
        if self._active.get(owner) is not lease:
            raise RuntimeError("invalid admission release")
        del self._active[owner]
        while self._accepting and self._waiters:
            waiter = self._waiters.popleft()
            if waiter.result.done():
                continue
            admitted = self._admit(waiter.owner)
            waiter.admitted = admitted
            waiter.result.set_result(admitted)
            break
        if not self._active:
            self._drained.set()

    async def shutdown(self, *, grace_seconds: float = _SHUTDOWN_GRACE_SECONDS) -> None:
        """Join the single operation that stops, cancels, and reaps admission."""
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, (int, float))
            or not math.isfinite(grace_seconds)
            or grace_seconds < 0
        ):
            raise ValueError("grace_seconds must be non-negative and finite")

        operation = self._shutdown_operation
        if operation is None:
            operation = asyncio.create_task(self._shutdown_once(float(grace_seconds)))
            self._shutdown_operation = operation

        external_cancellation: asyncio.CancelledError | None = None
        operation_error: BaseException | None = None
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as error:
                if external_cancellation is None:
                    external_cancellation = error
            except BaseException as error:
                operation_error = error
                break
        if operation_error is None:
            try:
                operation.result()
            except BaseException as error:
                operation_error = error
        if external_cancellation is not None:
            raise external_cancellation
        if operation_error is not None:
            raise operation_error

    async def _shutdown_once(self, grace_seconds: float) -> None:
        """Perform the controller shutdown exactly once."""

        self._accepting = False
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.result.done():
                waiter.result.set_result(None)

        if self._active:
            try:
                async with asyncio.timeout(grace_seconds):
                    await self._drained.wait()
            except TimeoutError:
                pass

        active_tasks = tuple(task for task in self._active if not task.done())
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        for lease in tuple(self._active.values()):
            lease.release()
        if self._active or self._waiters:
            raise RuntimeError("admission failed to drain")
