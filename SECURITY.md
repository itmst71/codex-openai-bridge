# Security Policy

## Supported versions

Security fixes are applied to the latest revision on the `main` branch. This project is
currently pre-1.0; older revisions are not maintained separately.

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
