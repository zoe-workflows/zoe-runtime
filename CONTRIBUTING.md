# Contributing to Zoe Runtime

Thanks for your interest in contributing. Zoe Runtime is a local execution
controller for AI coding tasks — it grants write access to an agent while
deliberately refusing to trust the agent's own claims. That stance shapes
how we review contributions.

## Developer Certificate of Origin (DCO)

By contributing, you certify that you have the right to submit the work
under the project's license (Apache-2.0). Each commit must carry a
`Signed-off-by` trailer:

```text
Signed-off-by: Name <email>
```

Use `git commit -s` to add it automatically. This mirrors the
[Developer Certificate of Origin](https://developercertificate.org).

## Getting started

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Requirements: Python 3.11+ on a POSIX system. Runtime has no third-party
dependencies; `dev` extras add `pytest` and `ruff`.

## Expectations for a change

1. **Preserve existing test scenarios.** The 40 runtime scenarios encode
   the behavior contract (single-writer, independent verification,
   publish idempotency, honest acceptance). Do not weaken their
   assertions. Changing import/mock paths during a refactor is fine;
   changing what they prove is not.
2. **Every behavior change needs a test.** New capabilities, changed
   defaults, or relaxed security checks must ship with a regression test.
3. **Capability claims need evidence.** If a capability moves from
   `not-run` toward `verified`, the evidence must be cited in
   `docs/capabilities.md`. Simulated tests never count as real
   end-to-end verification.
4. **Security-sensitive code must stay auditable.** This tool handles
   credentials and remote branches. Prefer small, single-purpose
   functions over clever one-liners when it affects permissions or
   publication.

## Areas of care

- **The single-writer invariant** (`execution.lock` + process identity).
- **Independent verification** (fingerprint before/after, no trusting
  agent self-reports).
- **Publication ownership** (only the controller commits/pushes/creates
  PRs).
- **Acceptance** (`authorization_ref` is a self-declared string, not
  identity — never treat it as authentication).

## Commit style

- Short imperative subject, e.g. `fix: resolve worker entry after install`.
- Reference an issue when one exists.
- Keep functional changes separate from formatting-only changes.

## Reporting security issues

Do not open a public issue for suspected vulnerabilities. See
[SECURITY.md](SECURITY.md) for the reporting channel and support scope.
