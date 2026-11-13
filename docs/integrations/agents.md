# OpenAI Agents SDK

**English (canonical)** | [日本語](agents.ja.md)

English is canonical if translations differ. [README](../../README.md)

Save the Python example below as `example.py`. Start the bridge separately; load extra packages in a consumer-only environment. These examples use the compatibility alias `codex`: confirm it appears in `/v1/models`, or change the model to `default` to match the discovery quickstart. Static mode uses `codex`.

This snippet configures a client/model; it does not itself run a generation. The consuming application owns its execution loop.

```bash
uv run --isolated --no-project --with openai-agents==0.21.1 --with openai==3.2.0 python example.py
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
API surface. Local `function_tool` calls with nonempty docstrings are verified. The Agents SDK's
`OpenAIResponsesModel` currently emits fields outside the strict bridge Responses subset; use the
verified Chat Completions model rather than treating the entire SDK as unsupported or relaxing the
bridge parser.
