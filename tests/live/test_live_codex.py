from __future__ import annotations

import json
import os

import pytest
from openai import OpenAI
from openai.types.chat.completion_create_params import ResponseFormat

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_CODEX") != "1",
    reason="requires RUN_LIVE_CODEX=1",
)


@pytest.mark.parametrize(
    ("response_format", "expected_field"),
    [
        ({"type": "json_object"}, None),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "live_result",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            "answer",
        ),
    ],
)
def test_live_codex_structured_output(
    response_format: ResponseFormat,
    expected_field: str | None,
) -> None:
    base_url = os.environ.get("CODEX_BRIDGE_LIVE_BASE_URL")
    api_key = os.environ.get("CODEX_BRIDGE_LIVE_API_KEY")
    if not base_url or not api_key:
        pytest.fail(
            "CODEX_BRIDGE_LIVE_BASE_URL and CODEX_BRIDGE_LIVE_API_KEY "
            "are required when RUN_LIVE_CODEX=1"
        )

    with OpenAI(base_url=base_url, api_key=api_key) as client:
        completion = client.chat.completions.create(
            model="codex",
            messages=[
                {
                    "role": "user",
                    "content": "Return a JSON object. For a schema request, set answer to yes.",
                }
            ],
            response_format=response_format,
        )

    content = completion.choices[0].message.content
    assert content is not None
    parsed = json.loads(content)
    assert type(parsed) is dict
    if expected_field is not None:
        assert parsed.get(expected_field) == "yes"
