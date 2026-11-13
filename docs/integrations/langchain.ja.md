# LangChain

[English (canonical)](langchain.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

以下のPython例を`example.py`として保存します。bridgeは別terminalで起動し、追加packageはconsumer用の隔離環境で読み込みます。例は互換alias `codex`を使います。`/v1/models`に掲載されていることを確認し、discoveryのクイックスタートと同じ設定にするならmodelを`default`へ変更してください。static modeでは`codex`を使います。

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

検証済みの Chat Completions path を選択する場合は、`use_responses_api=False` を設定します。`bind_tools` を介して渡す Pydantic models と functions には、nonempty docstring/description が必要です。description が欠けていると `description=""` になる場合があります。ブリッジは正確な tool contract を弱めるのではなく、これを意図的に拒否します。embeddings は別に設定してください。
async Responses streamingでは`openai[aiohttp]`を導入し、modelが所有するclientを明示的にcloseしてください。
LangChainが`object=response`、`status=completed`、`chunk_position=last`を持つ最終chunkを公開した後だけ
outputを成功として受理します。切断前のpartial textは成功ではありません。
検証済みloopback設定では、proxy継承とLangChain transport注入を防ぐため、両clientへ`trust_env=False`を設定し、
`http_socket_options=()`を指定します。
