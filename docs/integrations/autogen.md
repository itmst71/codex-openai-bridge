# AutoGen

**English (canonical)** | [日本語](autogen.ja.md)

English is canonical if translations differ. [README](../../README.md)

Save the Python example below as `example.py`. Start the bridge separately; load extra packages in a consumer-only environment. These examples use the compatibility alias `codex`: confirm it appears in `/v1/models`, or change the model to `default` to match the discovery quickstart. Static mode uses `codex`.

This snippet configures a client/model; it does not itself run a generation. The consuming application owns its execution loop.

```bash
uv run --isolated --no-project --with 'autogen-ext[openai]==0.7.5' --with autogen-core==0.7.5 --with openai==3.2.0 python example.py
```

AutoGen's OpenAI-compatible model client needs explicit capabilities for the unknown public alias
and must omit sampling/name fields:

```python
from pathlib import Path

bridge_token = (
    Path.home()
    .joinpath(".config/codex-openai-bridge/client-token")
    .read_text(encoding="ascii")
    .strip()
)

from autogen_core.models import ModelFamily
from autogen_ext.models.openai import OpenAIChatCompletionClient

client = OpenAIChatCompletionClient(
    model="codex",
    base_url="http://127.0.0.1:8646/v1",
    api_key=bridge_token,
    model_info={
        "vision": False,
        "function_calling": True,
        "json_output": True,
        "family": ModelFamily.UNKNOWN,
        "structured_output": True,
    },
    max_retries=0,
    timeout=240,
    include_name_in_message=False,
)
```

For an `AssistantAgent` reflection loop, setting `parallel_tool_calls=False` on the constructor is
not sufficient: AutoGen also sends it on the later reflection request after removing `tools`, which
the strict bridge correctly rejects. The verified
`BridgeOpenAIChatCompletionClient` adapter in
`tests/consumer_contract/test_autogen.py` adds that field only when a request actually contains
tools. It preserves AutoGen's normal direct client, local tool execution, tool-result replay, and
reflected final answer without weakening the gateway contract.

The permanent matrix verifies direct non-stream text, token streaming with a final `CreateResult`,
Pydantic JSON Schema output, and one fresh `AssistantAgent` local function-tool/reflection run. It
does not claim teams, handoffs, code execution, web/file surfers, MCP/workbenches, model cache,
memory, image input, hosted assistants, or multi-agent state. Configure embeddings separately and
always close the model client.
