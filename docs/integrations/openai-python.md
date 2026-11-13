# OpenAI Python SDK

**English (canonical)** | [日本語](openai-python.ja.md)

English is canonical if translations differ. [README](../../README.md)

Save the Python example below as `example.py`. Start the bridge separately; load extra packages in a consumer-only environment. These examples use the compatibility alias `codex`: confirm it appears in `/v1/models`, or change `selected_model` to `default` to match the discovery quickstart. Static mode uses `codex`.

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

Keep `max_retries=0`: ambiguous automatic generation retries can duplicate output or tool calls.
