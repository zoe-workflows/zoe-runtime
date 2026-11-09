# Releasing

One entry point, two stages, no manual uploads.

```text
tag → CI (tests + contract) → build → install-artifact verification
    → TestPyPI upload + install from TestPyPI
    → PyPI upload of the same artifacts
    → GitHub Release
```

## Ownership and naming

- Distribution name: `zoe-runtime` on PyPI. The **CLI command stays `zoe`**; so do
  task ids and the state directory.
- If the name is unavailable, change only the distribution name (for example
  `zoe-runtime-cli`) and update `runtime-requirements.json`'s `package` field. Never
  rename the command to work around a name collision.
- Credentials: PyPI Trusted Publishing (OIDC). No long-lived token in the
  repository, no token in a contributor's environment.

## Version rules

- The single source of truth is `src/__init__.py`; hatch reads it.
- `tag == package metadata == zoe --version`.
- A prerelease (`0.5.1rc1`) and the final release (`0.5.1`) are **different
  versions**: built separately, verified separately, uploaded as different
  filenames. Renaming a file to look like the final release is forbidden.
- A prerelease never relaxes a published skill's window; it is verified as an
  object, not adopted as the supported runtime.

## Build

```bash
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*          # metadata only — not a test result
```

`twine check` proves the metadata parses; it says nothing about whether the code
runs. That is what the install-artifact checks are for.

## Install-artifact verification (all of it, every release)

| # | Check | How |
|---|---|---|
| 1 | wheel installs in a clean environment | fresh venv, `pip install dist/*.whl` |
| 2 | the venv is outside the source tree and `PYTHONPATH` is cleared | `cd /tmp`, env scrubbed |
| 3 | `zoe --version` and `capabilities --json` | version equals the tag; `api_version`/`protocol_revision` present |
| 4 | key features that need no agent account | `zoe demo --json` guardrails, `capabilities --check-requirements` on both accept and reject |
| 5 | wheel rebuilt **from the sdist** behaves the same | `tar -xzf dist/*.tar.gz` → `python -m build --wheel` → repeat 1–4 |
| 6 | macOS and Windows each verified | Windows job failing means no dual-platform claim |

Steps 3–5 are encoded in `tests/test_distribution.py` and driven by CI
(`ZOE_DIST_PYTHON`). A skipped run is not evidence.

## Stage 1 — TestPyPI

```bash
python -m twine upload --repository testpypi dist/*
python3 -m venv /tmp/zoe-testpypi
/tmp/zoe-testpypi/bin/pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ "zoe-runtime==<version>"
cd /tmp && /tmp/zoe-testpypi/bin/zoe --version
```

TestPyPI availability is not delivery. It answers "does the upload install", not
"can a user install it".

## Stage 2 — PyPI

Upload the **same** `dist/` files (never rebuild between stages), then verify the
real user path from a machine or venv with no repository nearby:

```bash
pipx install "zoe-runtime==<version>"
cd /tmp && zoe --version && zoe capabilities --json
zoe capabilities --check-requirements /abs/path/to/zoe-workflows/runtime-requirements.json
```

Only after this does the GitHub Release get published.

## Six boundaries

1. **Staged acceptance** — TestPyPI green ≠ PyPI delivered; the production install
   path is verified separately.
2. **`rc` is not the release** — different version, different build, different
   verification.
3. **Name unavailable → change the distribution name only** — not `zoe`, not task
   ids, not the state directory.
4. **A failing Windows job forbids the dual-platform claim** — ship a Preview with
   an explicit platform scope instead.
5. **Runtime resources need a functional test, not a grep** — `grep "schemas"` is a
   lead; only an out-of-source run is proof. Not shipping `schemas/` today is a
   current fact, not a permanent rule.
6. **Two different paths** — `doctor.resolution.path` is the coding agent's;
   `ZOE_BIN` must point at Zoe. `scripts/zoe.sh --which` reports the wrapper's own
   resolution.

## Failure handling

- Failed upload: fix, bump the version, rebuild. Never overwrite or re-upload a
  released file.
- Bad release already installed: publish a fixed version, document the rollback in
  `docs/rollback.md`, and keep the previous skill copy available for a migration
  window.
