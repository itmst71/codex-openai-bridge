# OpenAI Agents SDK

[English (canonical)](agents.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

以下のPython例を`example.py`として保存します。bridgeは別terminalで起動し、追加packageはconsumer用の隔離環境で読み込みます。例は互換alias `codex`を使います。`/v1/models`に掲載されていることを確認し、discoveryのクイックスタートと同じ設定にするならmodelを`default`へ変更してください。static modeでは`codex`を使います。

この例はclient/modelの設定例であり、単独では生成を実行しません。実行loopは接続するアプリが所有します。

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

検証済みのブリッジ専用 configuration では tracing を無効にします。tracing は別の external API surface だからです。nonempty docstrings を持つ local `function_tool` calls は検証済みです。Agents SDK の `OpenAIResponsesModel` は現在、厳格なブリッジ Responses subset の範囲外の fields を送信します。SDK 全体を非対応とみなしたりブリッジ parser を緩和したりせず、検証済みの Chat Completions model を使用してください。
