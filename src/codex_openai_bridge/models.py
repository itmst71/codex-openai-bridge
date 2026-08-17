"""Small immutable models shared by streaming protocol layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsedSseEvent:
    """One unambiguous, bounded SSE data field."""

    event: str | None
    data: dict[str, Any] | None
    done: bool = False


@dataclass(frozen=True, slots=True)
class StreamIdentity:
    """Server-owned public identity used by every emitted chunk."""

    response_id: str
    created: int


@dataclass(frozen=True, slots=True)
class StreamUsage:
    """Validated Responses usage projected to Chat Completions names."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
