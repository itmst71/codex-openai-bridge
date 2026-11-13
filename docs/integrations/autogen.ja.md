# AutoGen

[English (canonical)](autogen.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

以下のPython例を`example.py`として保存します。bridgeは別terminalで起動し、追加packageはconsumer用の隔離環境で読み込みます。例は互換alias `codex`を使います。`/v1/models`に掲載されていることを確認し、discoveryのクイックスタートと同じ設定にするならmodelを`default`へ変更してください。static modeでは`codex`を使います。

この例はclient/modelの設定例であり、単独では生成を実行しません。実行loopは接続するアプリが所有します。

```bash
uv run --isolated --no-project --with 'autogen-ext[openai]==0.7.5' --with autogen-core==0.7.5 --with openai==3.2.0 python example.py
```

AutoGen の OpenAI-compatible model client では、未知の public alias に対する capabilities を明示し、sampling/name fields を省略する必要があります。

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

`AssistantAgent` reflection loop では、constructor に `parallel_tool_calls=False` を設定するだけでは不十分です。AutoGen は、後続の reflection request で `tools` を削除した後にもこれを送信し、厳格なブリッジは正しく拒否します。`tests/consumer_contract/test_autogen.py` にある検証済みの `BridgeOpenAIChatCompletionClient` adapter は、request に実際に tools が含まれる場合に限ってその field を追加します。gateway contract を弱めることなく、AutoGen の通常の direct client、local tool execution、tool-result replay、reflected final answer を維持します。

恒常的な matrix では、direct non-stream text、final `CreateResult` を伴う token streaming、Pydantic JSON Schema output、新規の `AssistantAgent` による local function-tool/reflection run 1 回を検証します。teams、handoffs、code execution、web/file surfers、MCP/workbenches、model cache、memory、image input、hosted assistants、multi-agent state の対応を示すものではありません。embeddings は別に設定し、model client は必ず close してください。
