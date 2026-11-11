"""Deterministic, bounded discovery and expiring atomic catalog generations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pytest

from codex_openai_bridge.config import ConfigError, Settings


def test_catalog_mode_is_explicit_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_MODEL_DISCOVERY", "codex")
    try:
        settings = Settings.from_env()
    except ConfigError:
        pytest.fail("official model discovery mode must be supported")
    assert settings.model_discovery == "codex"
    assert settings.model_refresh_seconds < settings.model_max_age_seconds
    monkeypatch.setenv("CODEX_BRIDGE_MODEL_MAX_AGE_SECONDS", "1")
    with pytest.raises(ConfigError):
        Settings.from_env()


def _rows(default: str = "gpt-6.1-sol") -> list[Any]:
    from codex_openai_bridge.model_catalog import CatalogModel

    return [
        CatalogModel("gpt-6.1-sol", default == "gpt-6.1-sol"),
        CatalogModel("gpt-5.6-terra", default == "gpt-5.6-terra"),
    ]


def test_catalog_aliases_default_overrides_and_immutability() -> None:
    from codex_openai_bridge.model_catalog import build_catalog

    snapshot = build_catalog(_rows(), overrides={"codex": "gpt-5.6-terra"}, refreshed_at=10.0)
    assert dict(snapshot.routes) == {
        "default": "gpt-6.1-sol",
        "codex": "gpt-5.6-terra",
        "codex-5-6-terra": "gpt-5.6-terra",
        "codex-6-1-sol": "gpt-6.1-sol",
        "standard": "gpt-6.1-sol",
    }
    assert snapshot.models == frozenset({"gpt-6.1-sol", "gpt-5.6-terra"})
    assert isinstance(snapshot.routes, MappingProxyType)


@pytest.mark.parametrize(
    "case",
    [
        "no_default",
        "two_defaults",
        "duplicate",
        "collision",
        "hidden_default",
        "invalid",
        "overflow",
        "override_collision",
        "reserved_default",
    ],
)
def test_invalid_or_ambiguous_catalogs_never_authorize_routes(case: str) -> None:
    from codex_openai_bridge.model_catalog import CatalogModel, CatalogUnavailable, build_catalog

    rows = _rows()
    overrides = {"codex": "gpt-5.6-terra"}
    if case == "no_default":
        rows = [CatalogModel("gpt-a", False)]
    elif case == "two_defaults":
        rows = [CatalogModel("gpt-a", True), CatalogModel("gpt-b", True)]
    elif case == "duplicate":
        rows += rows[:1]
    elif case == "collision":
        rows = [CatalogModel("gpt-6.1-sol", True), CatalogModel("gpt-6-1-sol", False)]
    elif case == "hidden_default":
        rows = [CatalogModel("gpt-hidden", True, hidden=True), CatalogModel("gpt-visible", False)]
    elif case == "invalid":
        rows = [CatalogModel("../model", True)]
    elif case == "overflow":
        rows = [CatalogModel(f"gpt-{i}", i == 0) for i in range(129)]
    elif case == "override_collision":
        overrides["codex-6-1-sol"] = "gpt-5.6-terra"
    elif case == "reserved_default":
        overrides["default"] = "gpt-5.6-terra"
    with pytest.raises(CatalogUnavailable, match=r"^model catalog is unavailable$"):
        build_catalog(rows, overrides=overrides, refreshed_at=10.0)


def test_hidden_and_withdrawn_models_are_not_advertised_or_remapped() -> None:
    from codex_openai_bridge.model_catalog import CatalogModel, build_catalog

    snapshot = build_catalog(
        [CatalogModel("gpt-6.1-sol", True), CatalogModel("gpt-secret", False, hidden=True)],
        overrides={"codex": "gpt-removed", "private": "gpt-secret"},
        refreshed_at=10.0,
    )
    assert dict(snapshot.routes) == {
        "default": "gpt-6.1-sol",
        "codex-6-1-sol": "gpt-6.1-sol",
        "standard": "gpt-6.1-sol",
    }


@pytest.mark.asyncio
async def test_refresh_is_atomic_and_last_good_expires_without_sliding_on_errors() -> None:
    from codex_openai_bridge.model_catalog import CatalogRegistry, CatalogUnavailable

    now = [10.0]
    rows = _rows()
    failure = [False]
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def load() -> list[Any]:
        if failure[0]:
            raise RuntimeError("synthetic private provider diagnostic")
        if blocked.is_set():
            await release.wait()
        return rows

    registry = CatalogRegistry(
        load,
        overrides={"codex": "gpt-5.6-terra"},
        refresh_seconds=5.0,
        max_age_seconds=20.0,
        clock=lambda: now[0],
    )
    with pytest.raises(CatalogUnavailable):
        registry.current()
    assert await registry.refresh()
    original = registry.current()
    rows = _rows("gpt-5.6-terra")
    blocked.set()
    pending = asyncio.create_task(registry.refresh())
    await asyncio.sleep(0)
    assert registry.current() is original
    release.set()
    assert await pending
    assert registry.current().routes["default"] == "gpt-5.6-terra"
    assert original.routes["default"] == "gpt-6.1-sol"
    failure[0] = True
    now[0] = 29.0
    assert not await registry.refresh()
    assert registry.current().routes["default"] == "gpt-5.6-terra"
    now[0] = 30.0
    with pytest.raises(CatalogUnavailable):
        registry.current()
    failure[0] = False
    now[0] = 31.0
    assert await registry.refresh()
    assert registry.current().refreshed_at == 31.0
    await registry.aclose()


@pytest.mark.asyncio
async def test_polling_and_shutdown_own_the_loader() -> None:
    from codex_openai_bridge.model_catalog import CatalogRegistry

    calls = [0]
    stopped = asyncio.Event()

    async def load() -> list[Any]:
        calls[0] += 1
        if calls[0] > 1:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return _rows()

    registry = CatalogRegistry(load, overrides={}, refresh_seconds=0.01, max_age_seconds=1.0)
    await registry.start()
    async with asyncio.timeout(1):
        while calls[0] < 2:
            await asyncio.sleep(0.005)
    await registry.aclose()
    assert stopped.is_set()


def test_static_mode_is_unchanged() -> None:
    settings = Settings.from_env()
    assert settings.model_discovery == "static"
    assert replace(settings, model_discovery="codex").model_map_active


@pytest.mark.asyncio
async def test_concurrent_start_has_one_refresh_and_one_owned_poller() -> None:
    from codex_openai_bridge.model_catalog import CatalogRegistry

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = [0]

    async def load() -> list[Any]:
        calls[0] += 1
        entered.set()
        await release.wait()
        return _rows()

    registry = CatalogRegistry(load, overrides={}, refresh_seconds=10.0, max_age_seconds=20.0)
    tasks = [asyncio.create_task(registry.start()) for _ in range(8)]
    await entered.wait()
    release.set()
    await asyncio.gather(*tasks)
    pollers = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") == "CatalogRegistry._poll"
    ]
    try:
        assert calls[0] == 1 and len(pollers) == 1
    finally:
        await registry.aclose()
        for task in pollers:
            task.cancel()
        await asyncio.gather(*pollers, return_exceptions=True)
