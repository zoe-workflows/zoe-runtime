# Compatibility between `zoe-workflows` and `zoe-runtime`

Two repositories, one contract file. The split only stays safe if each side knows
which question it is answering.

## Three layers, three responsibilities

| Layer | Checks | When | Owner |
|---|---|---|---|
| Runtime gate | version window, `api_version`, `protocol_revision` | every skill call, inside the wrapper | skill |
| CI contract | commands, parameters, response shapes (the subset the skill uses) | every PR, both repositories | both + cross-repo job |
| Release combination | the declared Skill × runtime matrix | release | runtime |

The runtime gate deliberately does **not** know the command list. A machine-readable
command export would be a second source of truth to keep in sync, and the gate runs
before every call — it must stay a tuple compare.

## The runtime gate

`runtime-requirements.json` uses structured fields, not a PEP 440 range string:
the runtime has no `packaging` dependency, and hand-written specifier parsing is
hidden complexity.

```json
{
  "package": "zoe-runtime",
  "command": "zoe",
  "min_version": "1.0.0",
  "max_version_exclusive": "2.0.0",
  "api_version": 2,
  "protocol_revision": 1
}
```

- The **runtime** parses it: `zoe capabilities --check-requirements FILE`. The
  skill must not shell out to `jq` or `python -c` — those are undeclared
  dependencies, and a future non-Python runtime has no reason to have them.
- `min_version` is inclusive, `max_version_exclusive` is exclusive.
- A prerelease sorts **below** its release: with `min_version: 1.0.0`, `1.0.0rc1`
  is rejected. Test prereleases are verified separately and never loosen a published
  skill's window.
- Failure codes: `0` compatible, `5` `RUNTIME_INCOMPATIBLE`, `2` `INPUT_INCOMPLETE`
  (unusable file), `126` wrapper could not resolve a runtime at all.

## The wrapper enforces it

```text
① resolve one absolute runtime: ZOE_BIN → ZOE_SRC → zoe on PATH
② run capabilities --check-requirements <skill>/runtime-requirements.json with it
③ reject → exit non-zero, {"error":{"code":"RUNTIME_INCOMPATIBLE"}}
④ accept → run the request with the same absolute runtime
```

A gate that lives in a document is optional. This one is inside the call path, and
the same resolved path is used for both steps so the gate can never check one
runtime while executing another. `--version` and `--which` skip the gate because
they are how a user diagnoses a rejected runtime.

## CI: three triggers

| Change | Verified before merge | Combination |
|---|---|---|
| runtime PR | build of the PR × **fixed skill tag** | new runtime × released skill |
| skill PR | skill content × **released compatible runtime** | new skill × released runtime |
| release | the declared matrix | see `releasing.md` |
| prerelease | a separate test combination, `runtime-requirements.json` untouched | the prerelease is the object under test |

Test code lives in this repository (`tests/contract/`, `tests/test_doc_contracts.py`)
and reads the skill checkout through `ZOE_SKILL_DIR`, so there is one implementation
of the gate, not two.

## What must be red, what must stay green

| Scenario | Expected |
|---|---|
| the skill claims a command or parameter the runtime rejects | red |
| the runtime removes an interface the skill depends on | red |
| the runtime adds a command the skill never uses | green |
| the skill stops documenting a command it no longer uses | green |

The asymmetry is intentional: only the skill's documented calls are asserted, so
adding a runtime command cannot force a skill PR, and dropping an unused line
cannot fail one.

## Response shapes

`capabilities` carries `api_version`, `version`, `protocol_revision`,
`capabilities`, `builtin`, `extended`, `note`, `adapters`. It does **not** carry a
command list. Schemas for CLI output live in `schemas/`; the runtime does not read
them at run time, so their presence in the wheel is a packaging convenience, not a
runtime dependency.
