## Description

<!-- Explain the problem and the bounded change in 2–4 sentences. -->
<!-- Descriptions and comments may be written in English or Japanese. -->

## Linked issue

<!-- Non-trivial changes require an open issue with the `scope-approved` label. -->

Fixes #

## Public contract and failure boundary

<!-- What exact behavior is accepted? What malformed, ambiguous, unsupported, or over-limit cases fail closed? -->

## Evidence

### RED failure

```text
<exact command and expected failure observed before implementation>
```

### GREEN command and result

```text
<exact focused and full commands actually run, with sanitized result summaries>
```

### Live or operational evidence

<!-- If claimed: exact consumer/version, bridge revision, endpoint/mode, bounded structural result, and unverified boundaries. Otherwise write "Not claimed." -->

## Checklist

- [ ] The linked issue is open and has `scope-approved`, or this is only a typo, broken link, or obviously incorrect documentation example.
- [ ] I ran every check claimed above and did not report checks I did not run.
- [ ] New behavior has a focused RED → GREEN test, including malformed or unsupported cases where applicable.
- [ ] The baseline local gates pass, or every unrun/failed gate is stated explicitly.
- [ ] I ran every applicable version-isolated CI consumer matrix lane locally, or listed the lanes left to GitHub CI.
- [ ] English canonical documentation and Japanese translation are updated when public behavior or support wording changes.
- [ ] Support wording is no stronger than the supplied contract, live, or operational evidence.
- [ ] No credential, token, account ID, prompt, tool argument, reasoning data, raw upstream response, or private production data is included.
- [ ] I did not add API-key authentication, provider fallback, multi-account, or hosted-service behavior.
- [ ] I reviewed any AI-assisted output and disclosed substantial AI assistance below.

## AI assistance

<!-- State the tools used and which parts were materially AI-assisted, or write "None." -->
