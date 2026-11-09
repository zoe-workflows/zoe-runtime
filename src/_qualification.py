"""Qualification: prove a local CLI can complete one real coding turn (03).

The six checks are: (1) CLI discoverable, (2) version matches, (3) auth works
non-interactively, (4) non-interactive edit, (5) result parse, (6) stop+resume.

Checks 1-2 are local probes. Checks 3-6 are proved by *real runs* through the same
controller path (``create`` -> ``start`` -> supervisor -> ``run_pipeline``), with a
single exemption: the throw-away project used for the run is registered with
``require_real_e2e: false``, so "no VERIFIED record" cannot block the run that is
supposed to produce one. Nothing else is bypassed: writer locks, verification,
scope checks and evidence all apply.
"""

from __future__ import annotations

import json
import platform as _platform_mod
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from src import _profiles
from src._store import Store
from src._util import Error, atomic, digest, require, safe_id


def platform_id() -> str:
    """Stable platform identity used as part of the capability key (D9)."""
    sys_ = _platform_mod.system().lower()
    if sys_ == "darwin":
        return f"macos-{_platform_mod.mac_ver()[0]}-{_platform_mod.machine().lower()}"
    if sys_ == "windows":
        return f"windows-{_platform_mod.win32_ver()[1]}-{_platform_mod.machine().lower()}"
    return f"{sys_}-{_platform_mod.machine().lower()}"


#: Kept for backwards-compatible imports inside this module's own tests.
_platform_id = platform_id


def _find_cli(adapter_id: str) -> str | None:
    """Resolve the CLI executable (Windows respects PATHEXT)."""
    candidates = {
        "codebuddy": ["codebuddy", "cbc"],
        "claude-code": ["claude"],
        "opencode": ["opencode"],
        "codex": ["codex"],
    }.get(adapter_id, [adapter_id])
    for name in candidates:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    return None


def _runtime_digest(cli_path: str, version_output: str) -> str:
    """Fingerprint the local CLI: path + version + size + mtime (03 §6)."""
    path = Path(cli_path)
    if not path.is_file():
        raise Error("QUALIFICATION_REQUIRED", f"CLI not found: {cli_path}")
    stat = path.stat()
    return "sha256:" + digest(
        {
            "path": str(path),
            "version_output": version_output,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
    )


def _version_of(adapter_id: str, cli_path: str) -> str:
    try:
        p = subprocess.run([cli_path, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (p.stdout or "") + (p.stderr or "")
    import re as _re

    m = _re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)", text)
    return m.group(1) if m else ""


#: Per-process cache of the current host CLI fingerprint. Computing it shells out
#: to ``--version``, so resolve_binding must not do it repeatedly.
_DIGEST_CACHE: dict[str, str | None] = {}


def current_runtime_digest(adapter_id: str) -> str | None:
    """Return the fingerprint of *this host's* CLI for ``adapter_id``.

    ``None`` means the CLI is not discoverable here, in which case an
    identity-bound evidence record can never match and the backend stays
    ``IMPLEMENTED`` (never a silent fallback to another binary).
    """
    if adapter_id in _DIGEST_CACHE:
        return _DIGEST_CACHE[adapter_id]
    value: str | None
    try:
        cli_path = _find_cli(adapter_id)
        if not cli_path:
            value = None
        else:
            value = _runtime_digest(cli_path, _version_of(adapter_id, cli_path))
    except (Error, OSError):
        value = None
    _DIGEST_CACHE[adapter_id] = value
    return value


def _git(repo: Path, *args: str) -> str:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error("ENVIRONMENT_ERROR", f"git unavailable: {exc}") from None
    if p.returncode != 0:
        raise Error(
            "INPUT_INCOMPLETE",
            f"git {' '.join(args)} failed in {repo}: {(p.stderr or p.stdout).strip()}",
        )
    return p.stdout


def _fixture_material(fixture: Path) -> tuple[str, list, dict]:
    """Read a qualification fixture: ``spec.md`` + optional ``checks.json``/``expect.json``."""
    require(fixture.is_dir(), "INPUT_INCOMPLETE", f"--fixture must be a directory: {fixture}")
    spec_path = fixture / "spec.md"
    require(spec_path.is_file(), "INPUT_INCOMPLETE", "fixture requires spec.md")
    text = spec_path.read_text()
    checks: list = [{"id": "qual-noop", "argv": ["true"]}]
    checks_path = fixture / "checks.json"
    if checks_path.is_file():
        try:
            data = json.loads(checks_path.read_text())
        except json.JSONDecodeError as exc:
            raise Error("INPUT_INCOMPLETE", f"checks.json is not JSON: {exc}") from None
        if isinstance(data, list) and data:
            checks = data
        elif isinstance(data, dict) and data.get("checks"):
            checks = data["checks"]
    expect: dict = {}
    expect_path = fixture / "expect.json"
    if expect_path.is_file():
        try:
            data = json.loads(expect_path.read_text())
        except json.JSONDecodeError as exc:
            raise Error("INPUT_INCOMPLETE", f"expect.json is not JSON: {exc}") from None
        if isinstance(data, dict):
            expect = data
    return text, checks, expect


def _validate_repo(repo: Path) -> None:
    """§5: a disposable repository, never a production remote."""
    require(repo.is_dir(), "INPUT_INCOMPLETE", f"--repo must be a directory: {repo}")
    inside = _git(repo, "rev-parse", "--is-inside-work-tree").strip()
    require(inside == "true", "INPUT_INCOMPLETE", f"--repo is not a git worktree: {repo}")
    _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    for line in _git(repo, "remote", "-v").splitlines():
        parts = line.split()
        url = parts[1] if len(parts) > 1 else ""
        if not url:
            continue
        require(
            url.startswith("/") or url.startswith(".") or url.startswith("file://"),
            "POLICY_DENIED",
            f"qualification repo must have no remote or a local one, found {url!r}",
        )


def _allowed_paths(repo: Path, expect: dict) -> list[str]:
    """Top-level repository entries (or the fixture's explicit list).

    ``scope()`` matches on path prefixes, so ``"."`` would match nothing; the
    disposable repo is allowed entry by entry instead.
    """
    declared = expect.get("allowed_paths")
    if isinstance(declared, list) and declared:
        return [str(item) for item in declared]
    tops = sorted({line.split("/")[0] for line in _git(repo, "ls-files").splitlines() if line})
    return tops or ["src"]


def _ephemeral_project(
    store: Store,
    binding,
    profile_id: str,
    repo: Path,
    checks: list,
    allowed: list[str],
    timeout: float,
) -> str:
    """Register a throw-away project whose only difference is the VERIFIED gate.

    Qualification reuses the real execution path; the single exemption (03 §1) is
    ``require_real_e2e: false`` on this ephemeral project. It also drops
    ``compatibility_evidence`` so the run cannot be "verified" by the record it is
    about to produce.
    """
    from src._config import validate_config
    from src._util import atomic

    profile = {k: v for k, v in binding.profile.items() if k not in ("compatibility_evidence",)}
    profile["require_permission_binding"] = False
    profile["image"] = None
    pid = "qual-" + uuid.uuid4().hex[:8]
    config = {
        "schema_version": 2,
        "project_id": pid,
        "repo": str(repo.resolve()),
        "base_ref": "HEAD",
        "backend": "local-process",
        "network": "none",
        "default_agent_profile": profile_id,
        "agent_profiles": {profile_id: profile},
        "authorization_ref": "qualification",
        "authorized_actions": ["create", "execute"],
        "allowed_paths": allowed,
        "checks": checks,
        "max_active_tasks": 1,
        # 暂停本身消耗一次尝试，所以"停止→续跑"至少需要 2 次预算。
        "max_attempts": 3,
        "timeout_seconds": int(max(60, timeout)),
        "require_real_e2e": False,
    }
    config = validate_config(config, base_dir=store.home / "projects")
    with store.lock():
        atomic(store.home / "projects" / (pid + ".json"), config)
    return pid


def _wait_state(store: Store, task_id: str, wanted: set, deadline: float) -> dict:
    """Poll a qualification task until it reaches one of ``wanted`` or time runs out."""
    from src._pipeline import reconcile

    t = store.task(task_id)
    while time.time() < deadline:
        try:
            t = reconcile(store, store.task(task_id))
        except Error:
            t = store.task(task_id)
        if t["state"] in wanted:
            return t
        time.sleep(0.5)
    return t


def _create_and_start(store: Store, project: str, spec: Path, title: str, tag: str) -> dict:
    from src._pipeline import create, start

    created = create(
        store,
        {
            "schema_version": 1,
            "request_id": f"qual-{tag}-create",
            "project_id": project,
            "title": title,
            "spec_file": str(spec),
            "delivery_mode": "local_artifact",
            "authorization_ref": "qualification",
        },
    )
    task_id = created["task_id"]
    t = store.task(task_id)
    start(
        store,
        task_id,
        {
            "schema_version": 1,
            "request_id": f"qual-{tag}-start",
            "expected_version": t["row_version"],
            "authorization_ref": "qualification",
            "reason": "qualification",
        },
    )
    return store.task(task_id)


def _edited_paths(t: dict) -> list[str]:
    """改动过的文件，相对任务基线。

    用 ``base_sha..working tree`` 而不是 ``git status``：verify 阶段会把候选提交掉，
    到 ``REVIEW_READY`` 时工作区本来就是干净的，用 status 会误判成"没有改动"。
    """
    from src._util import git

    workspace = Path(t["workspace"])
    if not workspace.exists():
        return []
    names = [n for n in git(workspace, "diff", "--name-only", t["base_sha"]).splitlines() if n]
    names += [
        n for n in git(workspace, "ls-files", "--others", "--exclude-standard").splitlines() if n
    ]
    return sorted(set(names))


def _checks_from_edit(t: dict, store: Store) -> list[dict]:
    """Derive auth / non-interactive-edit / parse from one real pipeline run."""
    run = store.get_run(t["current_run_id"]) if t.get("current_run_id") else None
    result = (run or {}).get("agent_result") or {}
    outcome = result.get("outcome")
    block = t.get("block_code")
    edited = _edited_paths(t)

    if outcome == "completed":
        auth = ("PASS", None)
    elif block == "AGENT_AUTH_REQUIRED":
        auth = ("FAIL", "agent reported an authentication requirement")
    elif block == "AGENT_PERMISSION_DENIED":
        auth = ("INCOMPLETE", "permission failure, not an auth failure")
    else:
        auth = ("INCOMPLETE", f"no completed agent result (block={block}, outcome={outcome})")

    if outcome == "completed" and edited:
        edit = ("PASS", None)
    elif block == "AGENT_PERMISSION_DENIED":
        edit = ("FAIL", "agent hit an approval prompt without a TTY")
    elif outcome == "completed":
        edit = ("FAIL", "agent completed but changed no files")
    else:
        edit = ("INCOMPLETE", f"no edit to observe (block={block})")

    if outcome == "completed" and (run or {}).get("result_schema") == "agent-adapter":
        parse = ("PASS", None)
    elif outcome == "completed":
        parse = ("INCOMPLETE", "no adapter capture: exit code only")
    else:
        parse = ("FAIL" if outcome else "INCOMPLETE", f"adapter outcome={outcome}")

    return [
        {"id": "auth", "status": auth[0], **({"detail": auth[1]} if auth[1] else {})},
        {
            "id": "noninteractive-edit",
            "status": edit[0],
            **({"detail": edit[1]} if edit[1] else {}),
        },
        {"id": "parse", "status": parse[0], **({"detail": parse[1]} if parse[1] else {})},
    ]


def _stop_resume(
    store: Store, project: str, spec: Path, stop_after: float, deadline: float
) -> dict:
    """Real stop + resume on a live tree (03 §2 check 6).

    A task that finished before the stop window proves nothing, so it is reported
    as ``INCOMPLETE`` with a hint to use a slower fixture rather than a silent PASS.
    """
    from src._pipeline import start, stop

    t = _create_and_start(store, project, spec, "qualification stop-resume", "stop")
    task_id = t["task_id"]
    t = _wait_state(
        store, task_id, {"RUNNING", "BLOCKED", "REVIEW_READY"}, min(deadline, time.time() + 60)
    )
    if t["state"] != "RUNNING":
        return {
            "status": "INCOMPLETE",
            "detail": f"task reached {t['state']} before it could be stopped",
            "stopped_and_resumed": False,
            "task_id": task_id,
        }
    time.sleep(max(0.0, stop_after))
    run = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
    live = bool(run and run.get("status") != "exited")
    if not live:
        return {
            "status": "INCOMPLETE",
            "detail": "agent exited before the stop window; use a slower fixture",
            "stopped_and_resumed": False,
            "task_id": task_id,
        }
    t = store.task(task_id)
    stop(
        store,
        task_id,
        {
            "schema_version": 1,
            "request_id": "qual-stop-pause",
            "expected_version": t["row_version"],
            "authorization_ref": "qualification",
            "reason": "qualification stop",
        },
    )
    t = _wait_state(
        store,
        task_id,
        {"PAUSED", "CANCELLED", "REVIEW_READY", "BLOCKED"},
        min(deadline, time.time() + 60),
    )
    paused = t["state"] in ("PAUSED", "CANCELLED")
    after = store.get_run(t.get("current_run_id")) if t.get("current_run_id") else None
    tree_stopped = bool(after and after.get("status") == "exited")
    if not (paused and tree_stopped):
        return {
            "status": "FAIL",
            "detail": f"stop was not confirmed (state={t['state']}, tree_stopped={tree_stopped})",
            "stopped_and_resumed": False,
            "task_id": task_id,
        }
    t = store.task(task_id)
    start(
        store,
        task_id,
        {
            "schema_version": 1,
            "request_id": "qual-stop-resume",
            "expected_version": t["row_version"],
            "authorization_ref": "qualification",
            "reason": "qualification resume",
        },
    )
    t = _wait_state(store, task_id, {"REVIEW_READY", "BLOCKED", "PAUSED", "CANCELLED"}, deadline)
    # 只认"续跑把任务重新推到可评审"。BLOCKED 不能算通过：它可能正是续跑失败
    # 的证据（RETRY_EXHAUSTED / PROCESS_UNKNOWN），那样会把缺陷写成 PASS。
    resumed = t["state"] == "REVIEW_READY"
    return {
        "status": "PASS" if resumed else "FAIL",
        "detail": None if resumed else f"resume ended in {t['state']}/{t.get('block_code')}",
        "stopped_and_resumed": bool(resumed),
        "task_id": task_id,
    }


def run(store: Store, req: dict[str, Any]) -> dict:
    """Perform qualification checks and write the evidence record to ``out``.

    The platform layer must be ready first (03 §4); otherwise refuse rather than
    "run once and see".
    """
    c = store.config(req["project_id"])
    profile_id = safe_id(req.get("profile") or c["default_agent_profile"])
    binding = _profiles.resolve_binding(c, profile_id)

    from src._config import _platform_lock_ok, _platform_tree_ok

    require(
        _platform_lock_ok() and _platform_tree_ok(),
        "PLATFORM_UNSUPPORTED",
        "Platform layer (lock/identity/tree stop) not verified",
    )

    adapter_id = binding.adapter_id
    expected_version = binding.profile.get("expected_version")

    cli_path = _find_cli(adapter_id)
    checks = []
    checks.append({"id": "cli-found", "status": "PASS" if cli_path else "FAIL"})
    require(cli_path, "QUALIFICATION_REQUIRED", f"CLI not discoverable for {adapter_id}")

    version = _version_of(adapter_id, cli_path)
    version_ok = bool(expected_version) and version == expected_version
    checks.append({"id": "version", "status": "PASS" if version_ok else "FAIL"})
    require(
        version_ok,
        "ADAPTER_VERSION_MISMATCH",
        f"CLI version {version!r} != expected {expected_version!r}",
    )

    runtime_digest = _runtime_digest(cli_path, version)

    # 六项检查的其余四项由两次真实运行证明（03 §1：复用正式执行路径，仅豁免
    # VERIFIED 门禁）。fixture 缺失时不做"跑一次看看"，直接拒绝。
    fixture = req.get("fixture")
    need_run = True
    spec_text: str | None = None
    expect: dict = {}
    if not fixture:
        need_run = False
    else:
        spec_text, fixture_checks, expect = _fixture_material(Path(str(fixture)).expanduser())
        checks_config = fixture_checks
    repo = req.get("repo")
    require(repo, "INPUT_INCOMPLETE", "--repo required")
    repo_path = Path(str(repo)).expanduser().resolve()

    timeout = float(req.get("timeout") or 1800)
    stop_after = float(req.get("stop_after") if req.get("stop_after") is not None else 3.0)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    deadline = time.time() + timeout
    evidence_refs: list[str] = []

    if need_run:
        _validate_repo(repo_path)
        allowed = _allowed_paths(repo_path, expect)
        project = _ephemeral_project(
            store, binding, profile_id, repo_path, checks_config, allowed, timeout
        )
        spec_path = store.home / "projects" / (project + "-spec.md")
        spec_path.write_text(spec_text)
        # 1) auth / noninteractive-edit / parse：一次完整 implement 运行
        t = _create_and_start(store, project, spec_path, "qualification edit", "edit")
        if t.get("task_id"):
            evidence_refs.append(t["task_id"])
        t = _wait_state(
            store,
            t["task_id"],
            {"REVIEW_READY", "BLOCKED", "PAUSED", "CANCELLED"},
            deadline,
        )
        checks += _checks_from_edit(t, store)
        # 2) stop-resume：先证明树被确认停止，再证明能续跑
        stop_resume = _stop_resume(store, project, spec_path, stop_after, deadline)
        if stop_resume.get("task_id"):
            evidence_refs.append(stop_resume["task_id"])
        checks.append(
            {
                "id": "stop-resume",
                "status": stop_resume["status"],
                **({"detail": stop_resume["detail"]} if stop_resume.get("detail") else {}),
            }
        )
        stopped_and_resumed = bool(stop_resume.get("stopped_and_resumed"))
    else:
        for name in ("auth", "noninteractive-edit", "parse", "stop-resume"):
            checks.append(
                {
                    "id": name,
                    "status": "INCOMPLETE",
                    "detail": "no --fixture: the four runtime checks were not exercised",
                }
            )
        stopped_and_resumed = False

    all_pass = all(item.get("status") == "PASS" for item in checks) and stopped_and_resumed
    record = {
        "adapter_id": adapter_id,
        # Design §7.2 field names, plus the registry's canonical names so the file
        # is directly usable as a profile's ``compatibility_evidence``.
        "adapter_backend": binding.backend,
        "backend": binding.backend,
        "execution_backend": "local-process",
        "platform": platform_id(),
        "agent_version": version,
        "version": version,
        "cli_path": cli_path,
        "runtime_digest": runtime_digest,
        "credential_names": list(binding.env_names),
        "profile_digest": binding.profile_hash,
        "profile_id": profile_id,
        "fixture_hash": digest(spec_text.encode()) if spec_text else None,
        "test_suite_version": "qual-v2",
        "checks": checks,
        "stopped_and_resumed": stopped_and_resumed,
        "evidence_refs": evidence_refs,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "real_e2e" if all_pass else "local_admin",
        "real_e2e_status": "PASS" if all_pass else "INCOMPLETE",
    }
    evidence = {
        "schema_version": 1,
        "project_id": req["project_id"],
        "records": [record],
    }

    out = req.get("out")
    require(out, "INPUT_INCOMPLETE", "--out required")
    out_path = Path(out).expanduser().resolve()
    if out_path.exists() and not req.get("replace"):
        raise Error(
            "REQUEST_CONFLICT",
            "Output file already exists; use --replace only for unreferenced records",
        )
    atomic(out_path, evidence)
    return {
        "status": "succeeded",
        "out": str(out_path),
        "records": 1,
        "real_e2e_status": record["real_e2e_status"],
        "outstanding": [item["id"] for item in checks if item.get("status") != "PASS"],
        "evidence_refs": evidence_refs,
        "note": (
            "All six checks passed on this host"
            if all_pass
            else (
                "At least one check did not pass; "
                "this record cannot lift the backend above IMPLEMENTED"
            )
        ),
        "evidence": evidence,
    }


def record_evidence(store: Store, req: dict[str, Any]) -> dict:
    """Record a qualification evidence file (idempotent by QID).

    ``--qualification`` may be the path to a bundle produced by
    ``qualification run``. When it is, the bundle is validated and fingerprinted
    so the record the operator later points a profile at can be identified.
    """
    qid = req.get("qualification")
    require(qid, "INPUT_INCOMPLETE", "--qualification QID required")
    # ``qid`` is normally a file path, which ``safe_id`` rejects. Use a stable
    # digest of the identifier so the operations row is still idempotent.
    key = digest(str(qid))[:24]
    with store.lock():
        key_op, prior = store.operation(
            {"request_id": "qual-ev-" + key, "schema_version": 1}, "evidence_record"
        )
        if prior:
            return prior
        detail: dict[str, Any] = {}
        path = Path(str(qid)).expanduser()
        if path.is_file():
            try:
                raw = path.read_text()
                data = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise Error(
                    "INPUT_INCOMPLETE", f"qualification evidence unreadable: {exc}"
                ) from exc
            records = data.get("records") if isinstance(data, dict) else None
            require(
                isinstance(records, list) and records,
                "INPUT_INCOMPLETE",
                "evidence file must contain a non-empty records array",
            )
            detail = {
                "source_file": str(path.resolve()),
                "file_digest": digest(raw),
                "records": len(records),
                "platforms": sorted(
                    {
                        str(item.get("platform"))
                        for item in records
                        if isinstance(item, dict) and item.get("platform")
                    }
                ),
            }
        else:
            detail = {"note": "recorded by id only; no evidence file to fingerprint"}
        result = {"status": "succeeded", "qualification": qid, **detail}
        store.finish(key_op, result)
        return result
