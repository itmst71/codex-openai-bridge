from __future__ import annotations

import pytest

from codex_openai_bridge.continuation import (
    ContinuationError,
    decode_continuation_id,
    decode_continuation_state,
    encode_continuation_id,
    encode_continuation_state,
)


def test_continuation_id_round_trip_is_alias_kind_and_state_bound() -> None:
    encoded = encode_continuation_id(
        raw_id="call_raw_1",
        public_model="codex-sol",
        kind="call",
        binding_key="k" * 43,
        state="digest-1",
    )

    assert encoded.startswith("cobr_c1_")
    assert (
        decode_continuation_id(
            encoded,
            public_model="codex-sol",
            kind="call",
            binding_key="k" * 43,
            state="digest-1",
        )
        == "call_raw_1"
    )
    for model, kind, state in (
        ("codex", "call", "digest-1"),
        ("codex-sol", "reasoning", "digest-1"),
        ("codex-sol", "call", "digest-2"),
    ):
        with pytest.raises(ContinuationError, match="invalid continuation"):
            decode_continuation_id(
                encoded,
                public_model=model,
                kind=kind,
                binding_key="k" * 43,
                state=state,
            )


def test_continuation_id_rejects_tampering_and_noncanonical_tokens() -> None:
    encoded = encode_continuation_id(
        raw_id="call_raw_1",
        public_model="codex-sol",
        kind="call",
        binding_key="k" * 43,
    )
    for invalid in (encoded + "A", encoded[:-1] + ("A" if encoded[-1] != "A" else "B"), "raw"):
        with pytest.raises(ContinuationError, match="invalid continuation"):
            decode_continuation_id(
                invalid,
                public_model="codex-sol",
                kind="call",
                binding_key="k" * 43,
            )

    assert (
        decode_continuation_id(
            "legacy_raw",
            public_model="codex",
            kind="call",
            binding_key="k" * 43,
            allow_legacy=True,
        )
        == "legacy_raw"
    )


def test_continuation_state_round_trip_is_alias_kind_and_item_bound() -> None:
    encoded = encode_continuation_state(
        "YQ==",
        public_model="codex-sol",
        kind="responses_reasoning",
        binding_key="k" * 43,
        max_value_bytes=4096,
        state="reasoning-item-a",
    )

    assert encoded.startswith("cobr_s1_")
    assert (
        decode_continuation_state(
            encoded,
            public_model="codex-sol",
            kind="responses_reasoning",
            binding_key="k" * 43,
            max_value_bytes=4096,
            state="reasoning-item-a",
        )
        == "YQ=="
    )
    for model, state in (
        ("codex", "reasoning-item-a"),
        ("codex-sol", "reasoning-item-b"),
    ):
        with pytest.raises(ContinuationError, match="invalid continuation"):
            decode_continuation_state(
                encoded,
                public_model=model,
                kind="responses_reasoning",
                binding_key="k" * 43,
                max_value_bytes=4096,
                state=state,
            )


def test_continuation_state_public_envelope_fits_exact_string_budget() -> None:
    limit = 256
    accepted: tuple[int, str] | None = None
    for raw_length in range(limit + 1):
        try:
            token = encode_continuation_state(
                "x" * raw_length,
                public_model="codex-sol",
                kind="responses_reasoning",
                binding_key="k" * 43,
                max_value_bytes=limit,
                state="reasoning-item-a",
            )
        except ContinuationError:
            break
        accepted = (raw_length, token)

    assert accepted is not None
    raw_length, token = accepted
    assert len(token.encode("utf-8")) <= limit
    assert (
        decode_continuation_state(
            token,
            public_model="codex-sol",
            kind="responses_reasoning",
            binding_key="k" * 43,
            max_value_bytes=limit,
            state="reasoning-item-a",
        )
        == "x" * raw_length
    )
    with pytest.raises(ContinuationError, match="invalid continuation"):
        encode_continuation_state(
            "x" * (raw_length + 1),
            public_model="codex-sol",
            kind="responses_reasoning",
            binding_key="k" * 43,
            max_value_bytes=limit,
            state="reasoning-item-a",
        )
    with pytest.raises(ContinuationError, match="invalid continuation"):
        decode_continuation_state(
            token + "A",
            public_model="codex-sol",
            kind="responses_reasoning",
            binding_key="k" * 43,
            max_value_bytes=len(token),
            state="reasoning-item-a",
        )
