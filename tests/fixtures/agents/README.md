# Adapter output fixtures

Design §10.4 / §18: every adapter needs versioned, sanitised output fixtures that
cover at least five failure classes.

## Status: synthetic, not real captures

Every file here declares:

```json
"source": "synthetic-from-documented-protocol"
```

That means the *shapes* were written from the documented output protocol, not
recorded from a real run. No real end-to-end run has happened for any adapter in
this build (`docs/capabilities.md` → `real-e2e: NOT_RUN`), so claiming otherwise
would be the exact failure this project refuses.

Promotion rules:

1. M1 / M3 / M3x record a real, sanitised capture at a pinned CLI version and
   replace the file, setting `source` to `"real-e2e"` and filling `cli_version`
   with the exact version.
2. The same run must produce a record in `docs/compatibility/` and a matching row
   in `docs/capabilities.md`; `tests/test_contract_artifacts.py` fails if the
   three disagree.
3. A `facts_status=FACT_PENDING` adapter (currently `codex`) must have **no**
   fixture file: an unverified interface has no output to be compatible with.

## Shape

```json
{
  "adapter_id": "codebuddy",
  "adapter_backend": "codebuddy-cli",
  "cli_version": "unverified-interface-sketch",
  "source": "synthetic-from-documented-protocol",
  "note": "why this file exists and what must replace it",
  "cases": [
    {"name": "normal", "raw": "...", "expect": {"outcome": "completed"}},
    {"name": "auth_failure", "raw": "...", "expect": {"outcome": "auth_error"}},
    {"name": "truncated", "raw": "...", "truncated": true,
     "expect": {"error": "OUTPUT_LIMIT_EXCEEDED"}}
  ]
}
```

`expect.outcome` is compared with `AgentResult.outcome`; `expect.error` is
compared with the raised `Error.code`. A fixture can never assert success for a
stream that lacks a terminal marker.
