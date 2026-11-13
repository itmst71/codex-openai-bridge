# LangChain

**English (canonical)** | [日本語](langchain.ja.md)

English is canonical if translations differ. [README](../../README.md)

Save the Python example below as `example.py`. Start the bridge separately; load extra packages in a consumer-only environment. These examples use the compatibility alias `codex`: confirm it appears in `/v1/models`, or change the model to `default` to match the discovery quickstart. Static mode uses `codex`.

```bash
uv run --isolated --no-project --with langchain-openai==1.6.0 --with langchain-core==1.6.0 --with 'openai[aiohttp]==3.3.1' python example.py
```

```python
from pathlib import Path

bridge_token = (
    Path.home()
    .joinpath(".config/codex-openai-bridge/client-token")
    .read_text(encoding="ascii")
    .strip()
)

import asyncio

from langchain_openai import ChatOpenAI
from openai import DefaultAioHttpClient, DefaultHttpxClient


async def main() -> None:
    sync_http_client = DefaultHttpxClient(trust_env=False)
    async_http_client = DefaultAioHttpClient(trust_env=False)
    llm = ChatOpenAI(
        model="codex",
        base_url="http://127.0.0.1:8646/v1",
        api_key=bridge_token,
        temperature=None,
        max_retries=0,
        timeout=240,
        use_responses_api=True,
        http_client=sync_http_client,
        http_async_client=async_http_client,
        http_socket_options=(),
    )
    try:
        response = await llm.ainvoke("Reply briefly.")
        print(response.content)
    finally:
        await llm.root_async_client.close()
        llm.root_client.close()


asyncio.run(main())
```

Set `use_responses_api=False` to select the verified Chat Completions path instead. Pydantic models
and functions supplied through `bind_tools` need a nonempty docstring/description. A missing
description can become `description=""`, which the bridge intentionally rejects rather than
weakening its exact tool contract. Configure embeddings separately.
For async Responses streaming, install `openai[aiohttp]` and explicitly close the model-owned
client. Accept output only after LangChain exposes a final chunk with `object=response`,
`status=completed`, and `chunk_position=last`; partial text before a disconnect is not a success.
The verified loopback configuration sets `trust_env=False` on both clients and
`http_socket_options=()` to prevent proxy inheritance and LangChain transport injection.
