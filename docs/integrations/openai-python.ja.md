# OpenAI Python SDK

[English (canonical)](openai-python.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

以下のPython例を`example.py`として保存します。bridgeは別terminalで起動し、追加packageはconsumer用の隔離環境で読み込みます。例は互換alias `codex`を使います。`/v1/models`に掲載されていることを確認し、discoveryのクイックスタートと同じ設定にするなら`selected_model`を`default`へ変更してください。static modeでは`codex`を使います。

```bash
uv run --isolated --no-project --with openai==1.109.1 python example.py
```

```python
from pathlib import Path
from openai import OpenAI

selected_model = "codex"

client = OpenAI(
    base_url="http://127.0.0.1:8646/v1",
    api_key=Path.home()
    .joinpath(".config/codex-openai-bridge/client-token")
    .read_text(encoding="ascii")
    .strip(),
    max_retries=0,
    timeout=240.0,
)

models = client.models.list()
assert any(model.id == selected_model for model in models.data)

completion = client.chat.completions.create(
    model=selected_model,
    messages=[{"role": "user", "content": "Reply briefly."}],
)
print(completion.choices[0].message.content)
```

`max_retries=0` を維持してください。曖昧な automatic generation retries により、output や tool calls が重複する可能性があります。
