# Compatibility records

A profile is **labelled** `VERIFIED` only when a verification record exists for its
exact `(adapter_id, backend, version)` combination. This directory holds those
records and the evidence behind them.

Since v0.4.1 the label does not gate dispatch: by default an unverified backend
still runs and is reported as `unverified` / `trust_level`. Set
`require_real_e2e: true` on the project to make the label a gate again.

Nothing in this directory is shipped as `VERIFIED`: no real end-to-end run has
been completed for any adapter in this build (see `docs/capabilities.md`).

## Producing a record

The canonical way is the command itself — it drives two real runs (implement +
stop/resume) and writes the bundle this schema validates:

```bash
zoe qualification run --project P --profile <profile> \
  --fixture tests/qualification/basic --repo /tmp/disposable-repo \
  --out docs/compatibility/<adapter>-<backend>.json
```

All six checks must pass for `source: real_e2e`; otherwise the same file is
written with `source: local_admin`, which never lifts a backend above
`IMPLEMENTED`.

## Pre-v0.5.0 records

Any record whose evidence captured `argv[0]` as a **bare name** was taken before
launch pinning (§6.6) and no longer describes what `start` executes: v0.5.0 pins
`argv[0]` to the absolute resolved path. Such a record must either be re-run
after upgrading or be annotated `"argv_form": "bare-name (pre-v0.5.0)"`. It must
never be quoted as evidence for the current command form.

## File shape

One file per profile or backend, validated by
`schemas/compat-evidence.v1.json`:

```json
{
  "records": [
    {
      "adapter_id": "codex",
      "backend": "<verified-backend-id>",
      "version": "0.42.0",
      "source": "local_admin",
      "platform": "linux-x86_64",
      "matrix": "extended",
      "recorded_at": "2026-10-27",
      "evidence_ref": "runs/2026-10-27-codex-openai-01.log"
    }
  ]
}
```

| Field | Rule |
|---|---|
| `adapter_id` | must match the profile's adapter, otherwise `ADAPTER_BACKEND_MISMATCH` |
| `backend` | must match the profile's backend; one backend never unlocks another |
| `version` | the **exact** CLI version; ranges, `latest` and empty strings are rejected |
| `source` | `real_e2e` for a real run; `local_admin` for an administrator declaration |
| `platform` | the platform the record was produced on (records are platform-scoped by convention) |
| `matrix` | `builtin` for the three factory adapters, `extended` for `codex` and later |

## How to record a real result

1. Pin the image by digest and write the exact CLI version into the profile's
   `expected_version`.
2. Run the full loop on the target platform: natural-language spec → `task
   create` → `task start` → real file change → controller verification →
   `local_artifact` `REVIEW_READY` → human acceptance. Include one stop/resume.
3. Keep the sanitised log and the command lines as `evidence_ref`.
4. Write the record with `source: "real_e2e"`, point the profile's
   `compatibility_evidence` at it, and re-run `zoe agent list --project P --json`
   to confirm the state.
5. Update `docs/capabilities.md` with the same evidence — the document and the
   registry must agree, or `tests/test_contract_artifacts.py` fails.

## Codex

`codex` ships as `facts_status=FACT_PENDING`. Before any command form is written
into `src/_agents/codex.py`, the M3x milestone must confirm, for a fixed
version:

1. the non-interactive entry point and its arguments;
2. the output protocol (single final JSON or event stream) and its terminator;
3. backend authentication and credential injection;
4. session-resume semantics and any repository-check switch;
5. distribution differences between releases.

Until then the adapter cannot build a launch, and no record should claim
otherwise.
