"""Bounded immutable generations of the official account-scoped model catalog."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from codex_openai_bridge.config import _MODEL_ID

MAX_CATALOG_MODELS = 128
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)


class CatalogUnavailable(RuntimeError):
    """A catalog cannot currently authorize model routing."""


def _unavailable() -> CatalogUnavailable:
    return CatalogUnavailable("model catalog is unavailable")


@dataclass(frozen=True, slots=True)
class CatalogModel:
    model: str
    is_default: bool
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    routes: Mapping[str, str]
    models: frozenset[str]
    refreshed_at: float


CatalogLoader = Callable[[], Awaitable[Sequence[CatalogModel]]]


def build_catalog(
    models: Sequence[CatalogModel],
    *,
    overrides: Mapping[str, str],
    refreshed_at: float,
) -> CatalogSnapshot:
    """Authorize only complete, unambiguous visible choices and deterministic aliases."""
    if (
        type(models) not in (list, tuple)
        or not 1 <= len(models) <= MAX_CATALOG_MODELS
        or type(refreshed_at) not in (int, float)
        or not math.isfinite(refreshed_at)
        or refreshed_at < 0
        or not isinstance(overrides, Mapping)
        or len(overrides) > 16
    ):
        raise _unavailable()
    visible: dict[str, str] = {}
    seen: set[str] = set()
    defaults: list[str] = []
    for row in models:
        if (
            type(row) is not CatalogModel
            or type(row.model) is not str
            or _MODEL_ID.fullmatch(row.model) is None
            or type(row.is_default) is not bool
            or type(row.hidden) is not bool
            or row.model in seen
        ):
            raise _unavailable()
        seen.add(row.model)
        if row.hidden:
            continue
        stem = row.model[4:] if row.model.startswith("gpt-") else row.model
        alias = "codex-" + stem.replace(".", "-")
        if _ALIAS.fullmatch(alias) is None or alias in visible:
            raise _unavailable()
        visible[alias] = row.model
        if row.is_default:
            defaults.append(row.model)
    if len(defaults) != 1:
        raise _unavailable()
    available = frozenset(visible.values())
    for alias, model in overrides.items():
        if (
            type(alias) is not str
            or type(model) is not str
            or _ALIAS.fullmatch(alias) is None
            or _MODEL_ID.fullmatch(model) is None
            or alias == "default"
            or (alias in visible and visible[alias] != model)
        ):
            raise _unavailable()
        if model in available:
            visible[alias] = model
    ordered = {"default": defaults[0]}
    if "codex" in visible:
        ordered["codex"] = visible.pop("codex")
    ordered.update((alias, visible[alias]) for alias in sorted(visible))
    return CatalogSnapshot(MappingProxyType(ordered), available, float(refreshed_at))


class CatalogRegistry:
    """Single-flight polling with atomic publication and a bounded last-good lifetime."""

    def __init__(
        self,
        loader: CatalogLoader,
        *,
        overrides: Mapping[str, str],
        refresh_seconds: float,
        max_age_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                for value in (refresh_seconds, max_age_seconds)
            )
            or refresh_seconds > max_age_seconds
        ):
            raise _unavailable()
        self._loader = loader
        self._overrides = MappingProxyType(dict(overrides))
        self._refresh_seconds = refresh_seconds
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._snapshot: CatalogSnapshot | None = None
        self._refresh_task: asyncio.Task[bool] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False

    def current(self) -> CatalogSnapshot:
        snapshot = self._snapshot
        if (
            snapshot is None
            or not 0 <= self._clock() - snapshot.refreshed_at < self._max_age_seconds
        ):
            raise _unavailable()
        return snapshot

    async def _refresh_once(self) -> bool:
        try:
            models = await self._loader()
            snapshot = build_catalog(models, overrides=self._overrides, refreshed_at=self._clock())
        except Exception:
            return False
        if self._closed:
            return False
        self._snapshot = snapshot
        return True

    async def refresh(self) -> bool:
        if self._closed:
            return False
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_once())
        return await asyncio.shield(self._refresh_task)

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            await self.refresh()

    async def start(self) -> None:
        if self._closed:
            raise _unavailable()
        if self._poll_task is not None:
            return
        try:
            await self.refresh()
            if not self._closed and self._poll_task is None:
                self._poll_task = asyncio.create_task(self._poll())
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        self._closed = True
        tasks = [task for task in (self._poll_task, self._refresh_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            awaitables: list[Awaitable[object]] = [task for task in tasks]
            await asyncio.gather(*awaitables, return_exceptions=True)
