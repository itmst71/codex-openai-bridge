from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import codex_openai_bridge.app as app_module
from codex_openai_bridge.admission import AdmissionController, AdmissionShuttingDown
from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings

TOKEN = "a" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _NeverCredentialProvider:
    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        raise AssertionError("credentials must not be resolved")


class _HandlerAbort(BaseException):
    pass


def _settings(tmp_path: Path, **changes: Any) -> Settings:
    token_file = tmp_path / "client-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    return replace(Settings.from_env(), client_token_file=token_file, **changes)


async def _event_loop_checkpoint() -> None:
    checkpoint = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
    await checkpoint


@pytest.mark.asyncio
async def test_controller_rejects_invalid_state_and_lease_release_is_idempotent() -> None:
    with pytest.raises(ValueError):
        AdmissionController(max_in_flight=0, queue_wait_seconds=1.0)
    with pytest.raises(ValueError):
        AdmissionController(max_in_flight=1, queue_wait_seconds=float("nan"))

    controller = AdmissionController(max_in_flight=1, queue_wait_seconds=1.0)
    lease = await controller.acquire()
    with pytest.raises(RuntimeError):
        await controller.acquire()
    lease.release()
    lease.release()
    assert controller.active_count == 0
    assert controller.waiting_count == 0
    with pytest.raises(AttributeError):
        controller.active_count = 1  # type: ignore[misc]
    with pytest.raises(ValueError):
        await controller.shutdown(grace_seconds=float("inf"))


@pytest.mark.asyncio
async def test_two_active_requests_hold_slots_and_third_waits_until_release() -> None:
    controller = AdmissionController(max_in_flight=2, queue_wait_seconds=1.0)
    entered = [asyncio.Event() for _ in range(3)]
    releases = [asyncio.Event() for _ in range(3)]

    async def request(index: int) -> None:
        lease = await controller.acquire()
        try:
            entered[index].set()
            await releases[index].wait()
        finally:
            lease.release()

    tasks = [asyncio.create_task(request(index)) for index in range(3)]
    await entered[0].wait()
    await entered[1].wait()
    await _event_loop_checkpoint()

    assert controller.active_count == 2
    assert controller.waiting_count == 1
    assert not entered[2].is_set()

    releases[0].set()
    await entered[2].wait()
    assert controller.active_count == 2
    assert controller.waiting_count == 0

    releases[1].set()
    releases[2].set()
    await asyncio.gather(*tasks)
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_queue_deadline_returns_sanitized_429_without_entering_handler(
    tmp_path: Path,
) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=2, queue_wait_seconds=0.01),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    release = asyncio.Event()
    entered = [asyncio.Event(), asyncio.Event()]

    async def holder(index: int) -> None:
        lease = await controller.acquire()
        entered[index].set()
        try:
            await release.wait()
        finally:
            lease.release()

    holders = [asyncio.create_task(holder(index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in entered))
    handler_called = False

    async def handler(_request: web.Request) -> web.StreamResponse:
        nonlocal handler_called
        handler_called = True
        raise AssertionError("queued request handler must not run")

    request = make_mocked_request("POST", "/v1/chat/completions", headers=AUTH, app=app)
    response = await app_module._admission_middleware(request, handler)
    assert isinstance(response, web.Response)
    assert isinstance(response.body, (bytes, bytearray))
    assert response.status == 429
    assert json.loads(response.body)["error"] == {
        "message": "Too many requests",
        "type": "rate_limit_error",
        "param": None,
        "code": "bridge_queue_timeout",
    }
    assert "Retry-After" not in response.headers
    assert not handler_called
    assert controller.active_count == 2
    assert controller.waiting_count == 0

    release.set()
    await asyncio.gather(*holders)


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_and_cannot_consume_or_leak_a_slot() -> None:
    controller = AdmissionController(max_in_flight=1, queue_wait_seconds=1.0)
    holder_release = asyncio.Event()
    holder_entered = asyncio.Event()

    async def holder() -> None:
        lease = await controller.acquire()
        holder_entered.set()
        try:
            await holder_release.wait()
        finally:
            lease.release()

    holder_task = asyncio.create_task(holder())
    await holder_entered.wait()
    cancelled_waiter = asyncio.create_task(controller.acquire())
    await _event_loop_checkpoint()
    assert controller.waiting_count == 1

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert controller.active_count == 1
    assert controller.waiting_count == 0

    replacement_entered = asyncio.Event()

    async def replacement() -> None:
        lease = await controller.acquire()
        replacement_entered.set()
        lease.release()

    replacement_task = asyncio.create_task(replacement())
    await _event_loop_checkpoint()
    holder_release.set()
    await replacement_entered.wait()
    await asyncio.gather(holder_task, replacement_task)
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_shutdown_rejects_new_work_and_wakes_queued_middleware_with_503(
    tmp_path: Path,
) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1, queue_wait_seconds=1.0),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    active_release = asyncio.Event()
    active_entered = asyncio.Event()

    async def active() -> None:
        lease = await controller.acquire()
        active_entered.set()
        try:
            await active_release.wait()
        finally:
            lease.release()

    active_task = asyncio.create_task(active())
    await active_entered.wait()
    handler_called = False

    async def handler(_request: web.Request) -> web.StreamResponse:
        nonlocal handler_called
        handler_called = True
        raise AssertionError("shutdown-woken handler must not run")

    request = make_mocked_request("POST", "/v1/responses", headers=AUTH, app=app)
    queued = asyncio.create_task(app_module._admission_middleware(request, handler))
    await _event_loop_checkpoint()
    assert controller.waiting_count == 1

    shutdown = asyncio.create_task(controller.shutdown(grace_seconds=1.0))
    response = await queued
    assert isinstance(response, web.Response)
    assert isinstance(response.body, (bytes, bytearray))
    assert response.status == 503
    assert json.loads(response.body)["error"]["code"] == "bridge_shutting_down"
    assert not handler_called
    assert controller.waiting_count == 0
    with pytest.raises(AdmissionShuttingDown):
        await controller.acquire()

    active_release.set()
    await asyncio.gather(active_task, shutdown)
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_shutdown_grace_cancels_and_reaps_overdue_active_handler() -> None:
    controller = AdmissionController(max_in_flight=1, queue_wait_seconds=1.0)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def handler() -> None:
        lease = await controller.acquire()
        entered.set()
        try:
            await never.wait()
        finally:
            lease.release()

    handler_task = asyncio.create_task(handler())
    await entered.wait()
    await controller.shutdown(grace_seconds=0.01)

    assert handler_task.done()
    assert handler_task.cancelled()
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_shutdown_reaps_handlers_and_preserves_external_cancellation() -> None:
    controller = AdmissionController(max_in_flight=1, queue_wait_seconds=1.0)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def handler() -> None:
        lease = await controller.acquire()
        entered.set()
        try:
            await never.wait()
        finally:
            lease.release()

    handler_task = asyncio.create_task(handler())
    await entered.wait()
    shutdown_task = asyncio.create_task(controller.shutdown(grace_seconds=1.0))
    await _event_loop_checkpoint()
    shutdown_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown_task

    assert handler_task.done()
    assert handler_task.cancelled()
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_overlapping_shutdown_cancels_each_owner_once_and_shares_reap() -> None:
    controller = AdmissionController(max_in_flight=1, queue_wait_seconds=1.0)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finish = asyncio.Event()
    duplicate_cancel = asyncio.Event()

    async def handler() -> None:
        lease = await controller.acquire()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            try:
                await cleanup_finish.wait()
            except asyncio.CancelledError:
                duplicate_cancel.set()
                raise
            raise
        finally:
            lease.release()

    handler_task = asyncio.create_task(handler())
    await entered.wait()
    first = asyncio.create_task(controller.shutdown(grace_seconds=0))
    await cleanup_started.wait()
    second = asyncio.create_task(controller.shutdown(grace_seconds=0))
    for _ in range(4):
        await _event_loop_checkpoint()

    assert not duplicate_cancel.is_set()
    assert handler_task.cancelling() == 1
    assert not first.done()
    assert not second.done()

    cleanup_finish.set()
    await asyncio.gather(first, second)
    assert handler_task.cancelled()
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_handler_base_exception_and_cancellation_each_release_once(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    request = make_mocked_request("POST", "/v1/chat/completions", headers=AUTH, app=app)

    async def abort(_request: web.Request) -> web.StreamResponse:
        raise _HandlerAbort

    with pytest.raises(_HandlerAbort):
        await app_module._admission_middleware(request, abort)
    assert controller.active_count == 0

    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked(_request: web.Request) -> web.StreamResponse:
        entered.set()
        await never.wait()
        raise AssertionError("unreachable")

    cancelled_request = make_mocked_request("POST", "/v1/chat/completions", headers=AUTH, app=app)
    task = asyncio.create_task(app_module._admission_middleware(cancelled_request, blocked))
    await entered.wait()
    assert controller.active_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_client_auth_rejects_before_queue_admission(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    active_release = asyncio.Event()
    active_entered = asyncio.Event()

    async def active() -> None:
        lease = await controller.acquire()
        active_entered.set()
        try:
            await active_release.wait()
        finally:
            lease.release()

    active_task = asyncio.create_task(active())
    await active_entered.wait()
    handler_called = False

    async def admitted(request: web.Request) -> web.StreamResponse:
        nonlocal handler_called
        handler_called = True

        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.Response()

        return await app_module._admission_middleware(request, handler)

    request = make_mocked_request("POST", "/v1/chat/completions", app=app)
    response = await app_module._client_auth_middleware(request, admitted)
    assert isinstance(response, web.Response)
    assert response.status == 401
    assert response.headers.getall("WWW-Authenticate") == ["Bearer"]
    assert not handler_called
    assert controller.active_count == 1
    assert controller.waiting_count == 0

    active_release.set()
    await active_task


@pytest.mark.asyncio
async def test_expect_admits_before_continue_and_middleware_reuses_one_slot(
    tmp_path: Path,
) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    writer = SimpleNamespace(write=AsyncMock(), output_size=99)
    request = make_mocked_request(
        "POST",
        "/v1/chat/completions",
        headers={
            **AUTH,
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Expect": "100-continue",
        },
        app=app,
        writer=writer,
    )

    assert await app_module._protected_expect_handler(request) is None
    assert controller.active_count == 1
    writer.write.assert_awaited_once_with(b"HTTP/1.1 100 Continue\r\n\r\n")

    async def handler(_request: web.Request) -> web.StreamResponse:
        assert controller.active_count == 1
        assert controller.waiting_count == 0
        return web.Response(status=200)

    response = await app_module._admission_middleware(request, handler)
    assert response.status == 200
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_queued_expect_times_out_without_continue_or_body_handler(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1, queue_wait_seconds=0.01),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    active_release = asyncio.Event()
    active_entered = asyncio.Event()

    async def active() -> None:
        lease = await controller.acquire()
        active_entered.set()
        try:
            await active_release.wait()
        finally:
            lease.release()

    active_task = asyncio.create_task(active())
    await active_entered.wait()
    writer = SimpleNamespace(write=AsyncMock(), output_size=0)
    request = make_mocked_request(
        "POST",
        "/v1/responses",
        headers={
            **AUTH,
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Expect": "100-continue",
        },
        app=app,
        writer=writer,
    )

    response = await app_module._protected_expect_handler(request)
    assert isinstance(response, web.Response)
    assert isinstance(response.body, (bytes, bytearray))
    assert response.status == 429
    assert json.loads(response.body)["error"]["code"] == "bridge_queue_timeout"
    writer.write.assert_not_awaited()
    assert controller.active_count == 1
    assert controller.waiting_count == 0

    active_release.set()
    await active_task


@pytest.mark.asyncio
async def test_expect_owner_ending_before_middleware_reclaims_lease(tmp_path: Path) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]

    async def expect_then_disconnect() -> None:
        writer = SimpleNamespace(write=AsyncMock(), output_size=0)
        request = make_mocked_request(
            "POST",
            "/v1/chat/completions",
            headers={
                **AUTH,
                "Content-Type": "application/json",
                "Content-Length": "2",
                "Expect": "100-continue",
            },
            app=app,
            writer=writer,
        )
        assert await app_module._protected_expect_handler(request) is None
        assert controller.active_count == 1

    owner = asyncio.create_task(expect_then_disconnect())
    await owner
    await _event_loop_checkpoint()
    assert controller.active_count == 0
    assert controller.waiting_count == 0


@pytest.mark.asyncio
async def test_waiters_are_admitted_in_fifo_order() -> None:
    controller = AdmissionController(max_in_flight=1, queue_wait_seconds=1.0)
    releases = [asyncio.Event() for _ in range(3)]
    entered = [asyncio.Event() for _ in range(3)]
    order: list[int] = []

    async def request(index: int) -> None:
        lease = await controller.acquire()
        order.append(index)
        entered[index].set()
        try:
            await releases[index].wait()
        finally:
            lease.release()

    first = asyncio.create_task(request(0))
    await entered[0].wait()
    second = asyncio.create_task(request(1))
    await _event_loop_checkpoint()
    third = asyncio.create_task(request(2))
    await _event_loop_checkpoint()
    assert controller.waiting_count == 2

    releases[0].set()
    await entered[1].wait()
    assert order == [0, 1]
    releases[1].set()
    await entered[2].wait()
    assert order == [0, 1, 2]
    releases[2].set()
    await asyncio.gather(first, second, third)
    assert controller.active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/healthz"), ("GET", "/readyz"), ("POST", "/v1/not-generation")],
)
async def test_non_generation_routes_stay_outside_admission(
    tmp_path: Path,
    method: str,
    path: str,
) -> None:
    app = create_app(
        _settings(tmp_path, max_in_flight=1),
        _NeverCredentialProvider(),
        upstream=object(),  # type: ignore[arg-type]
    )
    controller = app[app_module._ADMISSION_KEY]
    lease = await controller.acquire()
    handler_called = False

    async def handler(_request: web.Request) -> web.StreamResponse:
        nonlocal handler_called
        handler_called = True
        return web.Response(status=204)

    request = make_mocked_request(method, path, app=app)
    response = await app_module._admission_middleware(request, handler)
    assert response.status == 204
    assert handler_called
    assert controller.active_count == 1
    assert controller.waiting_count == 0
    lease.release()


@pytest.mark.asyncio
async def test_cleanup_drains_admission_before_closing_owned_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OrderingUpstream:
        def __init__(self) -> None:
            self.app: web.Application | None = None
            self.close_calls = 0
            self.counts_at_close: tuple[int, int] | None = None

        async def aclose(self) -> None:
            assert self.app is not None
            controller = self.app[app_module._ADMISSION_KEY]
            self.counts_at_close = (controller.active_count, controller.waiting_count)
            self.close_calls += 1

    upstream = OrderingUpstream()
    monkeypatch.setattr(
        app_module,
        "HttpxResponsesUpstream",
        lambda _settings, **_kwargs: upstream,
    )
    app = create_app(_settings(tmp_path, max_in_flight=1), _NeverCredentialProvider())
    upstream.app = app
    app.freeze()
    await app.startup()
    controller = app[app_module._ADMISSION_KEY]
    active_release = asyncio.Event()
    active_entered = asyncio.Event()

    async def active() -> None:
        lease = await controller.acquire()
        active_entered.set()
        try:
            await active_release.wait()
        finally:
            lease.release()

    active_task = asyncio.create_task(active())
    await active_entered.wait()
    waiter = asyncio.create_task(controller.acquire())
    await _event_loop_checkpoint()
    cleanup = asyncio.create_task(app.cleanup())
    with pytest.raises(AdmissionShuttingDown):
        await waiter
    assert upstream.close_calls == 0

    active_release.set()
    await asyncio.gather(active_task, cleanup)
    assert upstream.close_calls == 1
    assert upstream.counts_at_close == (0, 0)
