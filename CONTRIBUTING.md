# Contributing to codex-openai-bridge

**English (canonical)** | [日本語](CONTRIBUTING.ja.md)

Bug reports and narrowly scoped pull requests are welcome. This is a personal, pre-1.0
project maintained on a best-effort basis. No response, review, merge, or release timeline is
guaranteed, and accepting an issue does not promise that a particular implementation will merge.
Issue and pull request descriptions may be written in English or Japanese. English is canonical
for policy interpretation.

## Before writing code

1. Search open and closed issues and pull requests first.
2. Non-trivial external changes require an open issue with the `scope-approved` label. Wait for that label
   and comment on the issue before starting, so contributors and the maintainer do not duplicate
   work. The label approves the problem and scope, not a specific implementation.
3. Typos, broken links, and obviously incorrect documentation examples may be submitted directly
   as small pull requests.
4. Link a non-trivial pull request with `Fixes #N` or `Refs #N`.

The maintainer may implement an approved issue independently, request a different approach, or
close work that no longer fits. Check the issue immediately before starting and before opening a
pull request.

## What fits this project

A bridge change is a candidate only when all of these are true:

1. **A concrete consumer need** is identified with an exact consumer and version.
2. **Live acceptance by the Codex backend** can be demonstrated for the claimed behavior. A
   deterministic fixture may establish a contract, but not live or operational support.
3. **Meaningful OpenAI-compatible behavior** is preserved rather than simulated or silently
   ignored.
4. The bridge remains **bounded, stateless, server-owned**, loopback-only, and fail-closed.
5. **A RED → GREEN fail-closed contract** covers both the accepted behavior and malformed or
   unsupported cases.

Good candidates include strict translations of real Codex capabilities, reproducible consumer
compatibility fixes, validation and resource-bound hardening, secret-negative logging fixes, and
corrections to inaccurate documentation.

The following are out of scope:

- API-key authentication or fallback, OpenAI Developer API routing, or provider switching;
- multi-account operation, account pooling, client-selected credentials, or quota distribution;
- public, hosted, team, commercial, resale, or CI inference services;
- bridge-owned OAuth, generic credential plugins, or arbitrary external credential helpers;
- bridge-side Embeddings routing, vector stores, Files, Batch, Realtime, or other capability
  emulation not provided by the Codex backend;
- shell, browser, MCP, computer-use, or hosted-tool execution inside the bridge;
- relaxing strict validation by silently accepting, coercing, or ignoring unverified fields.

## Evidence and support wording

Use the repository's evidence levels exactly:

- **Contract verified**: an exact package version produces or accepts the documented deterministic
  wire shape. This does not prove provider behavior.
- **Live verified**: an exact package completes a representative operation through a reviewed
  bridge and the real Codex backend.
- **Operationally verified**: a real application is used repeatedly with real data or source, and
  relevant multi-turn and failure-recovery boundaries are observed.

Support wording must name the exact version, scope, configuration, and unverified boundaries. Live
claims are owner-local, opt-in checks and are never run with Codex OAuth credentials in GitHub
Actions. A contributor may supply sanitized live evidence, but the maintainer may require an
independent owner-local run before merging a live or operational claim.

## Sensitive data and security

Do not include credentials, access tokens, account IDs, real prompts, responses, tool arguments,
encrypted reasoning data, raw upstream responses, or bridge client tokens in issues, pull requests,
logs, screenshots, fixtures, commits, or review prompts. Use synthetic values and report only
bounded structure, status, error code, counts, and versions.
The server-only continuation signing key is also prohibited and must never be disclosed to clients.

Security vulnerabilities must be reported privately using the process in [SECURITY.md](SECURITY.md).
Do not open a public issue or pull request with exploit details.

## Development workflow

Use Python 3.12 and the locked environment:

```bash
uv sync --locked --all-groups
```

For every behavior change:

1. add the smallest behavior-level test;
2. run it and record the expected RED failure;
3. make the minimal change;
4. run the focused test to GREEN;
5. run the full offline gates.

```bash
uv run pytest -q
uv run mypy src tests scripts
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q src tests scripts
uv run python scripts/verify_systemd_unit.py
git diff --check
```

These are the **baseline local gates**, not the complete GitHub CI matrix. GitHub CI additionally
runs the version-isolated **Contract test with OpenAI SDK 3.1.0** and consumer contracts for the
versions pinned in `.github/workflows/ci.yml`, including the **Consumer contract with LangChain
1.5.1**, OpenAI Agents SDK, AutoGen, Aider, Cline CLI, and Continue core. The workflow file is the
source of truth for that matrix; the pull request must pass it after submission. Contributors should
run any isolated lane affected by their change and state exactly which lanes they did not run.

Live tests are optional and credential-bearing. Follow the opt-in instructions in the README and
never paste credential values into proofs. Do not add live credentials to CI.

## Pull request requirements

A mergeable pull request should:

- stay focused on one approved issue. Small documentation-only exceptions may omit the approved
  issue when they are limited to a typo, broken link, or obviously incorrect example;
- explain the public contract and failure boundary;
- include the RED failure and the exact GREEN command and result;
- include malformed and unsupported cases for parser, filesystem, process, or protocol changes;
- update English canonical documentation and the Japanese translation when behavior or support
  wording changes;
- preserve compatibility claims at or below the evidence supplied;
- contain no generated caches, local paths, credentials, or unrelated refactors;
- pass the baseline local gates and the applicable version-isolated consumer lanes; the submitted
  pull request must also pass the complete GitHub CI matrix.

Reviews may request changes or decline the proposal. Squash merge is the intended external
contribution history unless preserving multiple commits is specifically useful.

## AI-assisted contributions

AI-assisted contributions are allowed. The human contributor remains responsible for the complete
diff, security boundary, licensing, and test evidence. Review generated code before submission.
Only report checks that you actually ran, disclose substantial AI assistance in the pull request,
and do not let an agent read or post credentials or private production data.

## Conduct and license

Be respectful, technical, and concise. Harassment, personal attacks, and disclosure of private data
are not accepted. Contributions are licensed under the repository's [MIT License](LICENSE).
