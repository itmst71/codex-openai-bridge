# Security Policy

## Supported versions

Security fixes are applied to the latest revision on the `main` branch. This project is
currently pre-1.0; older revisions are not maintained separately.

## Scope and non-goals

The supported deployment boundary is personal, single-user, loopback-only use with one
owner-controlled bridge token and one owner-controlled Codex account. The security model does
not include multi-user credential distribution, account pooling, billing or resale, or a public hosted deployment. Do not use the bridge to make subscription-backed access available to other
users.

The bridge translates a bounded protocol subset; it is not a secret manager, tenant gateway,
hosted inference service, or OpenAI Developer API replacement. Shared services and production
automation should use the official OpenAI API with credentials and terms suitable for that use.

The supported credential authority is the official Codex CLI signed in with ChatGPT and configured
for file storage. Codex CLI 0.146.0 is the verified version.
Other Codex CLI versions remain unverified. API-key login, keyring scraping, provider fallback,
account pooling, and bridge-owned OAuth are outside this project's scope. Treat
`$CODEX_HOME/auth.json` as a password; never include it or any of its fields in a report.

The optional owner-controlled model alias map is routing authority, not a client passthrough or a
project support claim. It is read once with strict filesystem and schema checks. Public APIs expose
only configured aliases; real upstream model identifiers remain server-owned and are never returned.
Continuation authenticity uses a separate owner-only server signing key. It must never equal or be
disclosed with the client-facing bearer token.
Tool, reasoning, and compaction state uses signed continuation IDs and opaque-state envelopes bound
to its selected alias and configured real model; cross-alias or remapped-route continuation is
rejected before credential or upstream access.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting form for this repository when it is available.
If private reporting is unavailable, open a minimal issue asking the maintainer for a private
contact channel. Do not include exploit details, credentials, access tokens, account IDs,
prompts, tool arguments, encrypted reasoning data, or raw upstream responses in a public
issue.

A useful private report includes:

- the affected revision;
- the endpoint or trust boundary involved;
- minimal reproduction steps using synthetic credentials and payloads;
- the expected and observed sanitized behavior;
- an assessment of impact.

This bridge is intended for loopback-only use. Public exposure, shared-host deployments, and
protection from arbitrary malicious processes running as the same Unix user are outside its
security model.
