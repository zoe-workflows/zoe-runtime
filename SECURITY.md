# Security policy

## Supported versions

Only the latest release is supported. The project is in **preview**; treat it
as such.

## Scope

Zoe Runtime is a local controller that grants an AI agent write access inside an
isolated git worktree. Security issues that fall in scope include:

- A worker escaping the intended worktree scope.
- A way to obtain publishing credentials or model credentials from within the
  worker.
- A second writer starting while one is alive or unaccounted for.
- A task reaching `REVIEW_READY` / `DONE` on false evidence.
- Unintended remote writes (force-push, PR on the wrong branch).

## Out of scope

- Compromise of a trusted operator account or the host machine.
- Malicious `project.json` authored by an operator (single-user trust
  boundary).
- Image supply-chain issues in a third-party image.

## Reporting

Do **not** open a public issue. Report suspected vulnerabilities privately to
the maintainers via the repository's security advisory mechanism
(`Security` → `Report a vulnerability`).

Include, where possible:

- `zoe --version` and `zoe doctor --json` output.
- The project configuration (with secrets redacted).
- A minimal reproduction.

We will acknowledge within 7 days and aim for a fix or a reasoned
out-of-scope response within 90 days.

## Disclosure

Please coordinate disclosure. We ask that you not publish details until a fix
is available, and that you credit us in any public write-up.
