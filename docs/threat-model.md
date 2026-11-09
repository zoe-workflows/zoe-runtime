# Threat model

This tool exists to contain the risk of granting an AI agent write access to a
repository. The threat model below is the honest summary: what is mitigated,
and — just as importantly — what is **not**.

## Assets

- Source code in the target repository.
- Publishing credentials (GitHub token via `gh`, model credentials).
- Remote branch state and PR history.
- The task state database (`state.db`) and project configuration.

## Trust boundaries

| Component | Trust level |
|---|---|
| User / host (the operator) | trusted |
| `project.json` configuration | trusted (editable by the operator) |
| Zoe Runtime controller (host process) | trusted |
| Adapter module (in-tree, code-reviewed) | trusted but bounded by `LaunchSpec`/`AgentResult` |
| **Agent worker (process tree, default backend)** | **untrusted output, but runs with the operator's own OS privileges** |
| Remote (GitHub) | untrusted as a source of truth; reconciled, not trusted |

The worker's *output* is treated as a claim: only a bounded parse of a
fixed-version protocol may produce `completed`, and the controller re-verifies the
workspace itself. With the default `local-process` backend the worker is **not**
treated as a system-level adversary, because nothing stops it from acting as the
operator — see "Positioning" below.

## Positioning (v0.4)

> **Controlled execution in a trusted personal development environment.** Zoe
> controls *who may act, within what scope, and what evidence is left behind* — not
> "the agent cannot do harm".

| Mechanism | Guarantees | Does **not** guarantee |
|---|---|---|
| git worktree + branch | file organisation, task isolation, auditable diff scope | that the agent cannot read/write outside the workspace |
| `allowed_paths` + post-hoc diff | **accountability** (out-of-scope writes block delivery) | **prevention** (the write already happened) |
| environment allowlist | only declared credential *names* are injected | that `~/.ssh`, git credential helpers or other processes' secrets are unreadable |
| controller-owned publication | an auditable publish record | that the agent cannot push with the operator's own credentials |

## Mitigations (implemented)

- **One writer per workspace** — `maintenance.lock` (shared) + `.writer.lock`
  (exclusive), held by the supervisor for the whole run and released only after the
  managed process tree is confirmed gone.
- **Process-tree containment** — the agent runs in its own session/pgroup (POSIX) or
  a named Job Object with `KILL_ON_JOB_CLOSE` (Windows), so stopping a run takes the
  whole subtree, and a controller crash cannot leave an orphan writer.
- **Observation never signals** — liveness probing uses `OpenProcess` +
  creation-time comparison on Windows; `os.kill(pid, 0)` is never used there
  because it would terminate the target.
- **No container boundary to rely on** — v0.5.0 removed the container backend.
  There is no `--cap-drop=ALL`/`--read-only`/`--network none` guarantee for a
  worker anymore, and the documentation must not imply one: the worker runs with
  the operator's own privileges inside a git worktree. `allowed_paths` and the
  post-hoc diff are accountability, not prevention (see the table above).
- **Windows platform layer is code, not evidence** — the Job Object, lock and
  identity code paths exist and are unit-tested only through doubles; no real
  Windows hardware run exists. `docs/capabilities.md` records this as
  `implemented, unverified`, and `doctor` never claims Windows support. Treat a
  Windows host as a scenario to qualify first, not as a supported target.
- **Credential allowlist** — only environment variables whose names match the
  selected adapter's credential prefixes (`CODEBUDDY_`, `ANTHROPIC_`,
  `OPENCODE_`, `OPENAI_`, ...) are passed into the worker, and only those the
  profile names. One adapter cannot borrow another vendor's credentials.
- **Publisher owns Git writes** — commit/push/PR are controller-only; the worker
  is not given publication credentials.
- **Drift is judged before publishing** — the target branch and the task branch
  are compared with the frozen baseline first; a moved, rewritten or diverged
  remote blocks publication with evidence instead of force-pushing over it. An
  unobservable remote is `REMOTE_UNKNOWN`, never "clean".
- **The reported binary is the launched binary** — `argv[0]` is pinned to the
  path resolved by `doctor`/`agent probe`, so a version claim cannot describe a
  different executable than the one that runs.
- **No user string reaches a shell** — CLI names are validated against a table
  before the only `$SHELL -i -c` invocation, which is bounded (3 s) and killed by
  process group.
- **Independent verification** — checks run in the controller's environment, not
  the worker's; fingerprints are compared before and after.
- **Single writer** — `execution.lock` + process identity (PID + boot id + start
  time) prevent a second writer while one is alive or unknown.
- **Host-path protection** — the controller refuses to run when the repository
  path matches the controller's own install/state/skill directories.
- **Adapter containment** — an adapter cannot add host mounts: `LaunchSpec`
  only carries container-side writable directories (absolute, no `..`, never
  `/work`), and the workspace root stays read-only. Prompt text is delivered on
  stdin from a read-only mount, never as argv, with a 64 KiB ceiling.
- **No runtime plugin loading** — the registry is a code change. Nothing is
  imported from a repository, profile or request at runtime, so a project file
  cannot add an executable adapter.
- **Capability gating** — an adapter that is not `VERIFIED` for its exact
  `(adapter, backend, version)` cannot start; evidence for one backend never
  unlocks another. A `--version` probe never promotes a backend.
- **Bounded agent output** — capture is byte-limited; truncation is an error
  (`OUTPUT_LIMIT_EXCEEDED`), never a successful result.

## Not mitigated (open risks)

- **The agent is not sandboxed by default.** With `local-process` it runs as the
  operator: it can read any file the operator can read (including `~/.ssh`), write
  outside the worktree, or push with the operator's git credentials. `allowed_paths`
  and the fingerprint checks detect and block *delivery*, not the action itself.
- **Sub-process escape from the tree.** An agent that calls `setsid` (POSIX) or
  double-forks can leave the managed process group; such a run is reported as
  `PROCESS_UNKNOWN` rather than silently treated as exited, but the surviving
  process is not killed.
- **Configuration tampering.** A local user who can edit `project.json` can set
  `compatibility_verified: true` and expand `authorized_actions`. This is a
  single-user trust boundary, not a multi-tenant one.
- **Image supply chain.** The image is pinned by digest, but its provenance is
  the operator's responsibility; the controller does not verify image contents.
- **Model egress.** When networking is enabled for a model, the controller does
  not itself enforce domain-level egress policy; `network_policy_confirmed` is
  an operator attestation, not a technical control.
- **Compatibility evidence is an operator claim.** `compatibility_evidence`
  and the legacy `compatibility_verified` flag are administrator declarations
  labelled by `source`; they are not third-party attestation.
- **`authorization_ref` is not authentication.** It is a self-declared string
  used to bind a request to a configured scope. It must not be treated as proof
  of a human's identity.

## Reporting

See [SECURITY.md](../SECURITY.md).
