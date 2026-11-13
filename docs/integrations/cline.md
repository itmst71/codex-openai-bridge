# Cline CLI

**English (canonical)** | [日本語](cline.ja.md)

English is canonical if translations differ. [README](../../README.md)

The verified Cline role is deliberately narrower than Cline's default product surface. It uses the
real 3.0.55 headless CLI for one local, exact-replacement edit with only `read_files`, `editor`, and
`submit_and_exit` enabled. Cline state and hooks stay outside the target repository.

Prepare a private temporary state root and disable hosted feature flags, logging, updates, compaction,
shell commands, web tools, skills, subagents, and teams:

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

cline --yolo --json --thinking none --compaction off --retries 0 --timeout 240 \
  --provider openai-compatible --model codex \
  --cwd "$workspace" --data-dir "$control_dir/data" --hooks-dir "$control_dir/hooks" \
  --system "You are a bounded coding agent. The workspace root is $workspace. Use absolute paths under that root. Use read_files, editor, and submit_and_exit only. Never run shell commands." \
  "Read $workspace/target.txt. Use editor for the requested exact replacement, then submit and exit."
```

These controls are not optional compatibility polish:

- normal Cline startup contacts `data.cline.bot` for feature flags even after telemetry opt-out;
  version 3.0.55's own `E2E_TEST=true` branch selects its NoOp feature-flag provider;
- Cline's default 3,421-character system prompt reaches the bridge but consistently produced a Codex
  upstream 502 in bounded probes; the short override must also restore the exact absolute workspace
  root;
- `--yolo` is documented by Cline but hidden from this version's `--help`; it disables subagents and
  teams and leaves the three verified local tools;
- `--retries 0` limits Cline's agent mistake loop, not its HTTP transport retries. A transient failure
  can repeat the same model request, so non-idempotent external tool roles are unverified.

The permanent contract installs only Cline's integrity-pinned Linux x64 binary package from a
consumer-only lock with lifecycle scripts disabled. It does not install Cline's separate Node SDK
dependency graph. The binary runs behind a native loopback-only connect guard, receives three strict
streaming function calls through the production-like bridge, changes only `VALUE=1` to `VALUE=2`,
leaves Git HEAD unchanged, and creates no repository artifact. The default prompt, feature-flag
service, interactive TUI, VS Code/JetBrains/ACP, shell/web/browser tools, MCP, plugins, skills, hooks,
subagents, teams, hub, schedules, connectors, Kanban, resumed sessions, images, and compaction remain
unclaimed.

The versions in this matrix are reproducible known-good baselines, not a runtime allowlist. The
bridge never receives package versions: newer consumers remain usable when their emitted HTTP
payload still satisfies the same strict contract.
