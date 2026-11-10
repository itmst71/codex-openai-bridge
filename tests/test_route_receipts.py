"""Floating model selectors preserve authenticated, stateless continuation routes."""

from __future__ import annotations

import asyncio

import pytest

from codex_openai_bridge.continuation import decode_continuation_id, encode_continuation_id
from codex_openai_bridge.wire import create_reasoning_binding_id, reasoning_binding_is_valid

KEY = "synthetic-route-signing-key"


def test_receipts_pin_models_without_disclosing_model_or_route_key() -> None:
    from codex_openai_bridge.route_receipts import RouteReceipt, receipt_scope, select_receipt_model

    with receipt_scope(RouteReceipt(KEY, "default", "gpt-a")):
        token = encode_continuation_id(
            raw_id="call_a", public_model="default", kind="chat_call", binding_key="inner-key"
        )
        assert (
            decode_continuation_id(
                token, public_model="default", kind="chat_call", binding_key="inner-key"
            )
            == "call_a"
        )
    assert "gpt-a" not in token and len(token) <= 768
    document = {"messages": [{"role": "tool", "tool_call_id": token, "content": "ok"}]}
    assert (
        select_receipt_model(
            document,
            surface="chat",
            public_model="default",
            models=frozenset({"gpt-a", "gpt-b"}),
            binding_key=KEY,
        )
        == "gpt-a"
    )


@pytest.mark.parametrize("fault", ["tamper", "removed", "alias", "mixed", "purpose"])
def test_receipt_faults_fail_closed(fault: str) -> None:
    from codex_openai_bridge.route_receipts import (
        RouteReceipt,
        RouteReceiptError,
        receipt_scope,
        select_receipt_model,
    )

    with receipt_scope(RouteReceipt(KEY, "default", "gpt-a")):
        token = encode_continuation_id(
            raw_id="call_a", public_model="default", kind="responses_call", binding_key="a"
        )
    rows = [{"type": "function_call_output", "call_id": token, "output": "ok"}]
    models = frozenset({"gpt-a", "gpt-b"})
    alias = "default"
    if fault == "tamper":
        rows[0]["call_id"] = token[:-1] + ("A" if token[-1] != "A" else "B")
    elif fault == "removed":
        models = frozenset({"gpt-b"})
    elif fault == "alias":
        alias = "codex"
    elif fault == "mixed":
        with receipt_scope(RouteReceipt(KEY, "default", "gpt-b")):
            other = encode_continuation_id(
                raw_id="call_b", public_model="default", kind="responses_call", binding_key="b"
            )
        rows.append({"type": "function_call_output", "call_id": other, "output": "ok"})
    elif fault == "purpose":
        rows = [
            {"type": "message", "id": token.replace("cobr_t1_i.", "cobr_t1_r."), "content": "ok"}
        ]
    with pytest.raises(RouteReceiptError):
        select_receipt_model(
            {"input": rows}, surface="responses", public_model=alias, models=models, binding_key=KEY
        )


def test_reasoning_semantics_still_authenticate_after_route_selection() -> None:
    from codex_openai_bridge.route_receipts import RouteReceipt, receipt_scope, select_receipt_model

    with receipt_scope(RouteReceipt(KEY, "default", "gpt-a")):
        token = create_reasoning_binding_id(
            binding_key="route-key",
            content=None,
            tool_calls=(),
            index=0,
            data="QUJD",
            public_model="default",
        )
        assert reasoning_binding_is_valid(
            binding_key="route-key",
            content=None,
            tool_calls=(),
            index=0,
            data="QUJD",
            public_model="default",
            binding_id=token,
        )
        assert not reasoning_binding_is_valid(
            binding_key="route-key",
            content="",
            tool_calls=(),
            index=0,
            data="QUJD",
            public_model="default",
            binding_id=token,
        )
    document = {"messages": [{"role": "assistant", "reasoning_details": [{"id": token}]}]}
    assert (
        select_receipt_model(
            document,
            surface="chat",
            public_model="default",
            models=frozenset({"gpt-a", "gpt-b"}),
            binding_key=KEY,
        )
        == "gpt-a"
    )


@pytest.mark.asyncio
async def test_receipt_context_is_request_local() -> None:
    from codex_openai_bridge.route_receipts import RouteReceipt, receipt_scope, select_receipt_model

    async def run(model: str) -> str:
        with receipt_scope(RouteReceipt(KEY, "default", model)):
            await asyncio.sleep(0)
            token = encode_continuation_id(
                raw_id="call", public_model="default", kind="chat_call", binding_key=model
            )
            await asyncio.sleep(0)
            selected = select_receipt_model(
                {"messages": [{"role": "tool", "tool_call_id": token}]},
                surface="chat",
                public_model="default",
                models=frozenset({"gpt-a", "gpt-b"}),
                binding_key=KEY,
            )
            assert selected == model
            return token

    first, second = await asyncio.gather(run("gpt-a"), run("gpt-b"))
    assert first != second


def test_receipt_stripping_and_oversize_do_not_authorize_ids() -> None:
    from codex_openai_bridge.continuation import ContinuationError
    from codex_openai_bridge.route_receipts import RouteReceipt, receipt_scope

    inner = encode_continuation_id(
        raw_id="call", public_model="default", kind="chat_call", binding_key="inner"
    )
    with receipt_scope(RouteReceipt(KEY, "default", "gpt-a")):
        for invalid in [inner, "cobr_t1_" + "x" * 769]:
            with pytest.raises(ContinuationError):
                decode_continuation_id(
                    invalid, public_model="default", kind="chat_call", binding_key="inner"
                )
