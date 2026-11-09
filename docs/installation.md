# Installation

The runtime (`zoe-runtime`) and the host skill (`zoe-workflows`) are installed
separately. Installing the skill does not install a runtime; installing the runtime
does not install a skill.

- Runtime requirements: Python ≥ 3.11, no third-party dependencies, macOS or Windows.
- Skill requirement: this runtime inside the window declared in the skill's
  `runtime-requirements.json`.

## 1. pipx (recommended)

A CLI application belongs in an isolated environment with one entry point on
`PATH`:

```bash
pipx install zoe-runtime
pipx ensurepath          # new shells only; re-open the terminal
zoe --version
zoe capabilities --json
```

Production or reproducible installs pin the exact version. A range is a *skill*
decision, not an operator one:

```bash
pipx install "zoe-runtime==1.0.0"
```

## 2. venv + pip

```bash
python3 -m venv ~/.venvs/zoe
~/.venvs/zoe/bin/pip install "zoe-runtime==1.0.0"
~/.venvs/zoe/bin/zoe --version
```

Add `~/.venvs/zoe/bin` to `PATH`, or point the skill at it explicitly:
`ZOE_BIN=~/.venvs/zoe/bin/zoe`.

## 3. Developer checkout (editable)

```bash
git clone https://github.com/zoe-runtime/zoe-runtime.git
cd zoe-runtime
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Without installing, the runtime also runs from the checkout:

```bash
PYTHONPATH=. python3 -m src --version
```

To let the skill use this checkout, bind it explicitly — never rely on a source
tree that merely sits nearby:

```bash
export ZOE_SRC=/abs/path/to/zoe-runtime      # 仓库根目录（含 src/__main__.py），不是 src/
```

## 4. Offline / air-gapped

Take the wheel from the GitHub Release of the exact version, transfer it, then:

```bash
pipx install /media/zoe_runtime-1.0.0-py3-none-any.whl
```

Do not rebuild the wheel on the target host: the release artifact is the verified
one (§3.3 of the split plan — never overwrite released files to "fix" a version).

## 5. Verify the install

```bash
zoe --version                        # must equal the package metadata and the tag
zoe capabilities --json              # api_version / protocol_revision live here
```

Against a skill checkout:

```bash
zoe capabilities --check-requirements /abs/path/to/zoe-workflows/runtime-requirements.json
```

Exit `0` means the runtime is inside the declared window; exit `5` (`RUNTIME_INCOMPATIBLE`)
means it is not; exit `2` (`INPUT_INCOMPLETE`) means the file itself is unusable.

## GUI hosts cannot see your terminal PATH

`command -v zoe` succeeding in a terminal proves nothing about WorkBuddy or
OpenClaw when the host was launched from the GUI (launch agents often do not read
your shell profile).

```bash
bash /abs/path/to/zoe-workflows/scripts/zoe.sh --which
```

That reports the path the wrapper will actually execute, its resolution source and
the requirements file it will check. Two rules:

1. `zoe doctor`'s `resolution.path` is the **coding agent's** path, not Zoe's.
2. If the wrapper reports `RUNTIME_NOT_FOUND`, set an explicit absolute `ZOE_BIN`
   for the host, restart it, and re-run `--which`. The gate and the forwarded
   request always use that same path.

## Windows

`scripts/zoe.cmd` and `scripts/zoe.ps1` run the same check as `zoe.sh` and report
the same codes; PowerShell forwards to `.cmd` so the logic exists once. Note that
`where zoe` must find the console script — `pipx ensurepath` writes a user-level
`PATH` entry that some hosts only pick up after a restart.
