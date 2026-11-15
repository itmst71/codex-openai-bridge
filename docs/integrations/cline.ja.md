# Cline CLI

[English (canonical)](cline.md) | **日本語**

英語版を正本（canonical）とします。 [README](../../README.ja.md)

検証済みの Cline role は、意図的に Cline の default product surface より狭く設定されています。実際の 3.0.74 headless CLI を使用し、`read_files`、`editor`、`submit_and_exit` だけを有効にして、local で exact-replacement edit を 1 回実行します。Cline state と hooks は target repository の外に置きます。

private temporary state root を用意し、hosted feature flags、logging、updates、compaction、shell commands、web tools、skills、subagents、teams を無効にします。

```bash
control_dir="$(mktemp -d)"
workspace="$(pwd -P)"
trap 'rm -rf -- "$control_dir"' EXIT
install -d -m 700 "$control_dir/data/settings" "$control_dir/hooks"
cat > "$control_dir/data/settings/global-settings.json" <<'JSON'
{
  "autoUpdateEnabled": false,
  "compactionEnabled": false,
  "disabledTools": [
    "ask_question", "fetch_web_content", "run_commands", "search_codebase",
    "skills", "spawn_agent", "teams", "web_search"
  ],
  "telemetryOptOut": true,
  "toolAutoApprove": true
}
JSON

IFS= read -r bridge_token \
  < "$HOME/.config/codex-openai-bridge/client-token"
E2E_TEST=true CLINE_DATA_DIR="$control_dir/data" CLINE_LOG_ENABLED=0 \
  cline auth --provider openai --apikey "$bridge_token" --modelid codex \
  --baseurl http://127.0.0.1:8646/v1 --data-dir "$control_dir/data"
unset bridge_token

export E2E_TEST=true CLINE_DATA_DIR="$control_dir/data" CLINE_LOG_ENABLED=0
export CLINE_SESSION_BACKEND_MODE=local CLINE_SANDBOX=1
export CLINE_COMMAND_PERMISSIONS='{"allow":[],"deny":["*"],"allowRedirects":false}'

cline --yolo --json --compaction off --retries 0 --timeout 240 \
  --provider openai-compatible --model codex \
  --cwd "$workspace" --data-dir "$control_dir/data" --hooks-dir "$control_dir/hooks" \
  --system "You are a bounded coding agent. The workspace root is $workspace. Use absolute paths under that root. Use read_files, editor, and submit_and_exit only. Never run shell commands." \
  "Read $workspace/target.txt. Use editor for the requested exact replacement, then submit and exit."
```

これらの controls は、任意の compatibility polish ではありません。

- telemetry opt-out単独ではなく、上記controls全体が検証済み設定です。3.0.74のnative guard試験ではnon-loopback接続試行は0件でした。
  旧3.0.55では`data.cline.bot`を使わず、`E2E_TEST=true`のNoOp feature-flag branchを選択しました。
- 短いoverrideには正確なabsolute workspace rootも含めます。3.0.74のdefault promptは未検証です。
  旧3.0.55の3,421-character default promptで観測したupstream502を、新版の失敗実績とは扱いません。
- `--thinking none`も含めて`--thinking`を省略してください。3.0.74では指定すると未対応のChat field `reasoning_effort`を送信します。
  省略した設定でstrict contractと実Codexの編集が成功しました。upstream modelの既定reasoningを無効化する保証ではありません。
- `--yolo`とdisabled-tool listで検証済み3種類のlocal toolを選択します。他roleの検証ではありません。
- `--retries 0` は Cline の agent mistake loop を制限するもので、HTTP transport retries を制限するものではありません。transient failure によって同じ model request が繰り返される可能性があるため、non-idempotent external tool roles は未検証です。

恒常的な contract は、コンシューマー専用 lock から Cline の integrity-pinned Linux x64 binary package だけを、lifecycle scripts を無効にしてインストールします。Cline の別個の Node SDK dependency graph はインストールしません。binary は native loopback-only connect guard の背後で動作し、production-like ブリッジを介して厳格な streaming function calls を 3 回受信し、`VALUE=1` だけを `VALUE=2` に変更します。Git HEAD は変更されず、repository artifact も作成されません。default prompt、feature-flag service、interactive TUI、VS Code/JetBrains/ACP、shell/web/browser tools、MCP、plugins、skills、hooks、subagents、teams、hub、schedules、connectors、Kanban、resumed sessions、images、compaction は対応を保証しません。

別途行った最新版のlive試験でもHEADを変えず、対象fileだけを編集しました。実際の順序は`read_files`、`editor`、
もう一度`read_files`、`submit_and_exit`の4 tool callsです。決定的contractの3回とは区別してください。
[確認日付き検証表と証跡](../integrations.ja.md)を参照してください。

この matrix の versions は、再現可能な動作確認済みの baseline であり、runtime allowlist ではありません。ブリッジは package versions を受信しません。より新しいコンシューマーでも、送信する HTTP payload が同じ厳格な contract を満たす限り使用できます。
