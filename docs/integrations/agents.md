# OpenAI Agents SDK

**English (canonical)** | [日本語](agents.ja.md)

English is canonical if translations differ. [README](../../README.md)

Save the Python example below as `example.py`. Start the bridge separately; load extra packages in a consumer-only environment. These examples use the compatibility alias `codex`: confirm it appears in `/v1/models`, or change the model to `default` to match the discovery quickstart. Static mode uses `codex`.

This snippet configures a client/model; it does not itself run a generation. The consuming application owns its execution loop.

```bash
uv run --isolated --no-project --with openai-agents==0.24.0 --with openai==3.51.0 python example.py
```

```python
from pathlib import Path

bridge_token = (
    Path.home()
    .joinpath(".config/codex-openai-bridge/client-token")
    .read_text(encoding="ascii")
    .strip()
)

from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI

set_tracing_disabled(True)
client = AsyncOpenAI(
    base_url="http://127.0.0.1:8646/v1",
    api_key=bridge_token,
    max_retries=0,
    timeout=240,
)
model = OpenAIChatCompletionsModel(model="codex", openai_client=client)
agent = Agent(name="bounded-agent", instructions="Answer briefly.", model=model)
```

Tracing is disabled in the verified bridge-only configuration because tracing is a separate external
API surface. Local `function_tool` calls with nonempty docstrings are contract verified; the latest
live slice covers the basic Chat model and streaming. `OpenAIResponsesModel` is not qualified by
this lane. The older 0.21.1 probe emitted fields outside the strict Responses subset; that is not a
new test of 0.24.0 Responses support. Use the verified Chat model without relaxing the bridge parser.
