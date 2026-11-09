"""Read-only task preparation (config-friction v5 §B7).

``prepare`` answers "can this task be created and started, and what would it
freeze?" without writing anything. It never constructs a ``Store``: the project
profile is read straight from ``$ZOE_HOME/projects`` so a dry run cannot create
``state.db``, and the task library is not touched at all.

Three operations, deliberately separate (§B7.1):

* default      — preflight only; reads project configuration, spec and repository
* ``--out``    — save that report; an existing file is refused unless ``--force``
* ``--emit``   — write ``spec.json`` + ``create.json`` drafts; existing drafts stay
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from src import _config, _contracts, _paths, _profiles
from src._util import Error, atomic, digest, git, read, safe_id


def _require(ok, code, message, field=None):
    """``require`` with a field pointer, so a diagnostic can name the input."""
    if not ok:
        raise Error(code, message, details={"field": field} if field else None)


#: Drafts keep the ``<FILL:...>`` markers that validation refuses, so an
#: unfinished draft can never be mistaken for a runnable spec (§B8). There is
#: deliberately one template: a second one only added another placeholder to
#: fill in, which is cost without information.
#: Diagnostics that stop ``prepared`` from becoming true.
BLOCKING = {
    "PROJECT_AMBIGUOUS",
    "PROJECT_UNREGISTERED",
    "INPUT_INCOMPLETE",
    "POLICY_DENIED",
    "AUTH_REQUIRED",
    "SPEC_CLARIFICATION_REQUIRED",
    # 一个把工作目录钉到别处的 check，验证的不是这个任务的 worktree（见 _pinned_cwds）。
    # 它必须阻断：证据里的 exit code 会被绑到 worktree 指纹上，命令却跑在另一个树上，
    # 那种"通过"比失败更难发现。
    "CHECK_ESCAPES_WORKSPACE",
    # progress_refs 指向的文档不在 allowed_paths 内：Worker 根本无权更新它，
    # 于是"进度回写"这件事在派发那一刻就不可能发生。提前拒绝比事后解释便宜。
    "PROGRESS_REF_OUT_OF_SCOPE",
}

#: 形如 ``cd /abs``、``git -C /abs``、``--cwd=/abs`` 的"改工作目录"写法。
#: checks 的 cwd 已经是任务 worktree，再钉一次绝对路径只会让它验另一棵树。
_PINNED_CWD = (
    re.compile(r"(?:^|[\s;&|(])cd\s+(?P<path>/[^\s;&|)]+)"),
    re.compile(r"\bgit\s+(?:-C|--git-dir=)\s*(?P<path>/[^\s;&|)]+)"),
    re.compile(r"--cwd[=\s]+(?P<path>/[^\s;&|)]+)"),
)

_LANGUAGE_MARKERS = (
    ("go.mod", "go"),
    ("package.json", "node"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("Cargo.toml", "rust"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
)


def _spec_semantics_review(document, scope, diagnostics: list, advice: list) -> None:
    """v3：在派发前把"验收的形状"说出来。

    分两档，因为两档的处置不同：

    * ``diagnostics`` —— 必然失败或必然越权（``PROGRESS_REF_OUT_OF_SCOPE``）；
    * ``advice`` —— 能跑，但验收注定退化成"一次 check 通过"。这是建议，不是
      阻断：判定严格程度是项目选择。**默不作声**才是问题——2026-11-06 的
      spec 全部通过了 prepare，因为 prepare 从不看验收标准长什么样。
    """
    if not isinstance(document, dict):
        return
    for index, ref in enumerate(_contracts.progress_refs(document)):
        doc = ref.get("doc")
        if scope is not None and isinstance(doc, str) and not _paths.covers(doc, scope):
            diagnostics.append(
                _diag(
                    "PROGRESS_REF_OUT_OF_SCOPE",
                    "progress_refs[{}].doc".format(index),
                    "{} 不在 allowed_paths 内，Worker 无权更新它".format(doc),
                    "把该文档加入 allowed_paths，或去掉这条 progress_refs",
                    False,
                    "explicit",
                )
            )
    open_items = _contracts.open_decisions(document)
    if open_items:
        advice.append(
            _diag(
                "DECISIONS_OPEN",
                "decisions",
                "设计裁定未拍板：{}（不阻断 create，但会阻断 accept）".format(
                    ", ".join(str(item.get("id")) for item in open_items)
                ),
                "由 owner 拍板后回写 decisions[].status=resolved（task revise）",
                False,
                "explicit",
            )
        )
    block = _contracts.acceptance_review_block(document)
    if block["required"] and not block["independent"]:
        advice.append(
            _diag(
                "REVIEW_NOT_INDEPENDENT",
                "acceptance_review.independent",
                "要求逐条判定，但不要求独立上下文：实现者自审与独立判定在记录上无法区分",
                "设为 true；确有 owner 自审的场景请在交付报告里写明",
                False,
                "explicit",
            )
        )
    criteria = [
        item
        for item in document.get("acceptance_criteria") or []
        if isinstance(item, dict)
    ]
    check_ac_ids = {
        (item.get("verification") or {}).get("check_id")
        for item in criteria
        if (item.get("verification") or {}).get("type") == "check"
    }
    if len(criteria) == 1:
        advice.append(
            _diag(
                "AC_SINGLE_ITEM",
                "acceptance_criteria",
                "只有一条 AC：任何一次检查通过都会让整份验收变成 PASS",
                "至少补一条证伪式 AC（做错了必然失败），manual 项写 judge",
                False,
                "explicit",
            )
        )
    elif criteria and len(check_ac_ids) == 1 and len(check_ac_ids) == len(criteria):
        advice.append(
            _diag(
                "AC_SINGLE_SIGNAL",
                "acceptance_criteria",
                "全部 AC 都指向同一个 check：多条验收只是同一次信号的三次复述",
                "拆成机制检查（check）+ 业务闭环（manual，带 supporting_checks）",
                False,
                "explicit",
            )
        )


def _diag(code, field, reason, suggested_change=None, requires_approval=False, source="derived"):
    return {
        "code": code,
        "field": field,
        "reason": reason,
        "suggested_change": suggested_change,
        "requires_approval": bool(requires_approval),
        "source": source,
    }


def _pinned_cwds(check: dict) -> list[str]:
    """Absolute working directories a check pins for itself (see ``_PINNED_CWD``)."""
    found: list[str] = []
    for arg in check.get("argv") or []:
        if not isinstance(arg, str):
            continue
        for pattern in _PINNED_CWD:
            for match in pattern.finditer(arg):
                path = match.group("path").rstrip("/")
                if path and path not in found:
                    found.append(path)
    return found


def _scope_granularity(scope, repo_root: Path | None) -> list[dict]:
    """Advisory: which declared scope entries are directories rather than files.

    A directory entry is legitimate for a task that owns a whole package, and it
    is exactly what makes a **parallel** batch's boundary meaningless: the Worker
    can rewrite any file under it. This reports the granularity; whether it is
    acceptable is the orchestrator's call (the runtime cannot know if the task
    will run alongside others).
    """
    warnings: list[dict] = []
    if not scope or repo_root is None:
        return warnings
    for index, entry in enumerate(scope):
        candidate = Path(entry)
        if not candidate.is_absolute():
            candidate = repo_root / entry
        try:
            if not candidate.is_dir():
                continue
        except OSError:
            continue
        warnings.append(
            {
                "field": f"allowed_paths[{index}]",
                "code": "SCOPE_DIRECTORY_LEVEL",
                "message": (
                    f"'{entry}' is a directory; a Worker may rewrite any file under it "
                    "(file-level entries are what keep a parallel batch's boundaries meaningful)"
                ),
            }
        )
    return warnings


def _load_config(home: Path, project_id: str) -> dict:
    path = home / "projects" / f"{project_id}.json"
    _require(path.exists(), "INPUT_INCOMPLETE", f"Unknown project {project_id!r}", "project")
    return read(path)


def _resolve_project(
    home: Path, project: str | None, repo: str | None
) -> tuple[str | None, bool, list[str]]:
    """Return ``(project_id, registered, candidates)``.

    Matching is deliberately narrow: a repository matches a registration only
    when their git roots are the same. A path inside another repository's
    worktree is *not* adopted by the outer project — a nested checkout is a
    different project, and guessing here would attribute tasks to the wrong
    policy (§B7.2).
    """
    if project:
        # 与 ``store.config`` 同一约束：不允许 ``../`` 把读取带出 home。
        identifier = safe_id(project)
        return identifier, (home / "projects" / f"{identifier}.json").exists(), []
    _require(repo, "INPUT_INCOMPLETE", "prepare needs --project or --repo", "repo")
    target = str(Path(repo).expanduser().resolve())
    toplevel = _paths.git_toplevel(target) or target
    matches: list[str] = []
    for path in sorted((home / "projects").glob("*.json")):
        try:
            data = read(path)
        except (Error, ValueError, OSError):
            continue
        registered_repo = data.get("repo")
        if not registered_repo:
            continue
        if _paths.same_path(toplevel, registered_repo):
            identifier = safe_id(data.get("project_id") or path.stem)
            if identifier not in matches:
                matches.append(identifier)
    if len(matches) > 1:
        return None, False, matches
    if len(matches) == 1:
        return matches[0], True, []
    return None, False, []


def _repo_block(repo: str | None, config: dict | None) -> dict:
    root = repo or (config or {}).get("repo")
    block: dict[str, Any] = {
        "path": str(Path(root).resolve()) if root else None,
        "base_ref": (config or {}).get("base_ref", "HEAD"),
        "base_sha": None,
        "dirty": None,
        "language_hints": [],
        "observed_at": None,
    }
    if not root:
        return block
    block["observed_at"] = time.time()
    path = Path(root)
    try:
        block["base_sha"] = git(str(path), "rev-parse", "HEAD").strip() or None
        block["dirty"] = bool(git(str(path), "status", "--porcelain").strip())
    except (Error, OSError):
        # An unreadable repository is reported as "unknown", never as clean.
        block["base_sha"] = None
        block["dirty"] = None
    try:
        # 去重并保持声明顺序：``pyproject.toml`` 与 ``setup.py`` 同时存在时，
        # 未去重会给出一份 ``["python", "python"]`` 的提示。
        block["language_hints"] = list(
            dict.fromkeys(
                language for marker, language in _LANGUAGE_MARKERS if (path / marker).exists()
            )
        )
    except OSError:
        block["language_hints"] = []
    return block


def _profile_block(config: dict | None, profile: str | None) -> dict:
    """Surface the profile that would be used, without choosing a new one (§B10)."""
    block: dict[str, Any] = {
        "selected": None,
        "source": "explicit" if profile else "default",
        "authorized": None,
        "model": None,
        "model_source": None,
        "model_unresolved": False,
        "conflicts": [],
    }
    if not config:
        return block
    identifier = profile or config.get("default_agent_profile")
    block["selected"] = identifier
    profiles = config.get("agent_profiles") or {}
    entry = profiles.get(identifier) if isinstance(profiles, dict) else None
    if not isinstance(entry, dict):
        block["conflicts"].append(
            {"code": "INPUT_INCOMPLETE", "detail": f"unknown agent profile {identifier!r}"}
        )
        return block
    # 与 ``authorize()`` 走同一个访问器：非数组形态在那里等于"什么都不授权"，
    # 这里的预演必须给出同样的答案，否则 prepare 会说"可以开始"而 start 拒绝。
    actions = _config.authorized_actions(config)
    # 空列表在 ``authorize()`` 里等于"什么都不授权"，所以这里是 False 而不是
    # None：``None`` 会被读成"未知"，而实际结果一定是被拒。
    block["authorized"] = "execute" in actions
    block["actions_declared"] = bool(actions)
    block["model"] = entry.get("model")
    block["model_source"] = "profile" if entry.get("model") else "cli-default"
    block["model_unresolved"] = bool(entry.get("allow_cli_default_model"))
    if entry.get("model") and entry.get("allow_cli_default_model"):
        block["conflicts"].append(
            {
                "code": "MODEL_PIN_CONFLICT",
                "detail": "model is pinned while allow_cli_default_model is true",
            }
        )
    try:
        _profiles.resolve_binding(config, identifier)
    except Error as exc:
        block["conflicts"].append({"code": exc.code, "detail": str(exc)})
    return block


def _checks_block(config: dict | None, document: dict | None) -> dict:
    # ``suggested`` 曾在报告里恒为 ``[]``：既没有赋值点，也没有文档消费方。
    # 一个"永远是空数组"的字段只会让人以为预演失败，直接去掉。
    block: dict[str, Any] = {"selected": [], "preflight": []}
    if not config:
        return block
    declared = set()
    for criterion in (document or {}).get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        verification = criterion.get("verification") or {}
        check_id = verification.get("check_id") if isinstance(verification, dict) else None
        if check_id:
            declared.add(check_id)
    scope = (document or {}).get("verification_scope") or {}
    for check_id in scope.get("checks") or []:
        if isinstance(check_id, str):
            declared.add(check_id)
    block["selected"] = [{"id": check_id, "source": "spec"} for check_id in sorted(declared)]
    if not declared:
        block["selected"] = [
            {"id": check.get("id"), "source": "project"}
            for check in config.get("checks") or []
            if isinstance(check, dict)
        ]
    block["preflight"] = _config.checks_preflight(config)
    return block


def _emit_drafts(
    directory: Path, title: str | None, project_id: str | None, spec_path: str | None
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    kept: list[str] = []

    spec_target = directory / "spec.json"
    if spec_target.exists():
        kept.append(str(spec_target))
    elif spec_path:
        # A caller-supplied spec is copied verbatim so the draft directory is
        # self-contained; placeholders inside it keep failing validation.
        atomic(spec_target, read(spec_path))
        created.append(str(spec_target))
    else:
        draft = {
            "schema_version": 3,
            "title": title or "<FILL:一句话目标>",
            "allowed_paths": ["<FILL:目标路径>"],
            # v3 草稿刻意写成"两条 AC、两种判者"：一条机器能判、一条必须有人判，
            # 并且要求逐条判定独立完成。这是唯一一种不会退化成"pytest 过了就算
            # 做完"的形状，把它当模板比在文档里讲道理有效。
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "statement": "<FILL:可判定的完成条件（做错了必然失败）>",
                    "verification": {
                        "type": "check",
                        "check_id": "<FILL:项目已批准的 check id>",
                    },
                },
                {
                    "id": "AC-2",
                    "statement": "<FILL:业务闭环（机器判不了的那一半）>",
                    "verification": {
                        "type": "manual",
                        "judge": "independent",
                        "supporting_checks": ["<FILL:为这条提供机器证据的 check id>"],
                        "instructions": "<FILL:谁判、依据哪条证据判>",
                    },
                },
            ],
            "acceptance_review": {
                "required": True,
                "mode": "either",
                "independent": True,
            },
            "decisions": [],
            "progress_refs": [],
        }
        atomic(spec_target, draft)
        created.append(str(spec_target))

    create_target = directory / "create.json"
    if create_target.exists():
        kept.append(str(create_target))
    else:
        request = {
            "schema_version": 2,
            "request_id": str(uuid.uuid4()),
            "project_id": project_id or "<FILL:project_id>",
            "title": title or "<FILL:一句话目标>",
            "spec_file": str(spec_target),
            "delivery_mode": "local_artifact",
            "authorization_ref": "<FILL:authorization_ref>",
        }
        atomic(create_target, request)
        created.append(str(create_target))

    return {"directory": str(directory), "created": created, "kept": kept}


def prepare(
    home,
    *,
    project: str | None = None,
    repo: str | None = None,
    spec: str | None = None,
    out: str | None = None,
    emit: str | None = None,
    title: str | None = None,
    profile: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build the preparation report. Never writes runtime state."""
    _require(
        out is None or emit is None,
        "INPUT_INCOMPLETE",
        "--out and --emit are separate operations; pass only one",
        "out",
    )
    now = time.time()
    home_path = Path(home)
    project_id, registered, candidates = _resolve_project(home_path, project, repo)

    diagnostics: list[dict] = []
    if candidates:
        diagnostics.append(
            _diag(
                "PROJECT_AMBIGUOUS",
                "project_id",
                "several registered projects cover this repository: " + ", ".join(candidates),
                "pass --project explicitly",
                False,
            )
        )
    elif not registered:
        diagnostics.append(
            _diag(
                "PROJECT_UNREGISTERED",
                "project_id",
                "no registered project covers this repository",
                "register the profile shown below, then re-run prepare",
                True,
            )
        )

    config = _load_config(home_path, project_id) if registered else None
    policy_hash = digest(config) if config else None
    project_block = {
        "project_id": project_id,
        "registered": registered,
        "policy_hash": policy_hash,
        "policy_revision": policy_hash[:16] if policy_hash else None,
        "candidates": candidates,
    }

    document = None
    spec_valid = None
    spec_text = None
    scope = None
    scope_source = "project"
    if spec:
        # 未注册时不做项目范围的 widening 校验：没有项目就没有边界。
        # ``allowed_paths=None`` 是"不校验"的显式写法；不能再用 ``["."]``
        # 冒充，那会让 ``is_within`` 把任何路径都当成在范围内。
        boundary = config or {"allowed_paths": None, "protected_paths": []}
        try:
            loaded = _contracts.load_spec(spec, boundary)
        except Error as exc:
            spec_valid = False
            details = exc.details or {}
            diagnostics.append(
                _diag(
                    exc.code,
                    details.get("field") or "spec",
                    str(exc),
                    exc.to_dict().get("hint"),
                    False,
                    "explicit",
                )
            )
        else:
            spec_valid = True
            document = loaded.get("document")
            spec_text = loaded.get("text")
            scope = _contracts.spec_scope(document)
            if scope is not None:
                scope_source = "spec"
        if not registered:
            # 结构合法 ≠ 范围合法：没有项目就没有可比的授权边界。说清楚，
            # 否则报告里的 ``scope_source="spec"`` 会被读成"范围已确认"。
            scope_source = "unregistered"
            diagnostics.append(
                _diag(
                    "SCOPE_UNVERIFIED",
                    "allowed_paths",
                    "scope cannot be checked against a project policy until the "
                    "project is registered",
                    "register the project, then re-run prepare to confirm the scope",
                    False,
                    "derived",
                )
            )
    elif emit is None:
        diagnostics.append(
            _diag(
                "INPUT_INCOMPLETE",
                "spec",
                "prepare needs --spec, or --emit to generate a draft",
                "pass --spec <file>, or --emit <dir> to start from a template",
                False,
                "explicit",
            )
        )

    # v3：验收形状的建议单独一栏，不与"能不能派发"混在一起——把建议塞进
    # diagnostics 会让 `prepared=false` 变成噪声，也会让真正的问题被淹没。
    advice: list[dict] = []
    _spec_semantics_review(document, scope, diagnostics, advice)
    checks = _checks_block(config, document)
    unresolved = [row for row in checks["preflight"] if row.get("status") == "unresolved"]
    for row in unresolved:
        diagnostics.append(
            _diag(
                "CHECK_UNRESOLVED",
                f"checks[{row.get('check_id')}].argv[0]",
                f"cannot resolve {row.get('argv0')!r} for check {row.get('check_id')!r}",
                "install the tool, declare an absolute path, or run it through a shell",
                False,
            )
        )
    # 静态可判的一类"验证跑错树"：check 自己把 cwd 钉到了别的地方。
    for check in (config or {}).get("checks") or []:
        if not isinstance(check, dict):
            continue
        for pinned in _pinned_cwds(check):
            diagnostics.append(
                _diag(
                    "CHECK_ESCAPES_WORKSPACE",
                    f"checks[{check.get('id')}].argv",
                    f"check {check.get('id')!r} pins its working directory to {pinned!r}; "
                    "checks already run inside the task worktree, so this validates "
                    "another tree while its exit code is recorded against this candidate",
                    "drop the `cd`/`-C` and pass repository-relative arguments",
                    False,
                    "explicit",
                )
            )

    if scope is not None and not _paths.subtract(
        scope, (config or {}).get("protected_paths") or []
    ):
        diagnostics.append(
            _diag(
                "POLICY_DENIED",
                "allowed_paths",
                "the declared scope is entirely covered by protected_paths",
                "widen the scope, or ask for the parent directory",
                True,
                "explicit",
            )
        )

    profile_block = _profile_block(config, profile)
    # "授权不含 execute"是一个已知的否答，必须在 diagnostics 里说出来；否则报告
    # 只有 prepared=false，而 next 让用户"修上面的诊断"，上面却什么都没有。
    if profile_block["authorized"] is False:
        diagnostics.append(
            _diag(
                "AUTH_REQUIRED",
                "authorized_actions",
                "project does not authorize 'execute'; task start would be refused",
                "add 'execute' to authorized_actions and re-register the project",
                True,
                "explicit",
            )
        )
    blocking = [item for item in diagnostics if item["code"] in BLOCKING]
    # ``prepared`` 的语义是"能不能创建并启动"。``start`` 的第一道闸门是
    # ``authorize(..., "execute")``；未授权时仍报 prepared=true，会让 next 把用户
    # 直接送进 AUTH_DENIED——报告内部也自相矛盾（authorized=false 却 prepared=true）。
    prepared = bool(
        registered
        and spec_valid
        and not blocking
        and not unresolved
        and profile_block["authorized"]
    )

    repo_block = _repo_block(repo, config)
    report: dict[str, Any] = {
        "api_version": 2,
        "dry_run": True,
        "prepared": prepared,
        "unregistered": not registered,
        "project": project_block,
        "repo": repo_block,
        "spec": {
            "provided": bool(spec),
            "valid": spec_valid,
            "effective_allowed_paths": scope,
            "scope_source": scope_source,
        },
        "allowed_paths": {
            "suggested": [
                {"path": entry, "source": "spec", "observed_at": now} for entry in (scope or [])
            ],
            "warnings": (_config.path_warnings(config) if config else [])
            + _scope_granularity(
                scope, Path(repo_block["path"]) if repo_block.get("path") else None
            ),
        },
        "checks": checks,
        "profile": profile_block,
        "diagnostics": diagnostics,
        # v3：不阻断、但值得在派发前改一改的验收形状问题（AC 太粗、
        # 不要求独立判者、裁定未拍板）。``prepared`` 不看这一栏。
        "advice": advice,
        "snapshot": {
            "policy_hash": policy_hash,
            # 与 ``create`` 同源：都取 spec 文本的 digest，而不是文件字节——
            # 冻结快照是包裹格式，直接读字节会得到另一个值。
            "spec_hash": digest(spec_text.encode()) if spec_text else None,
            "base_sha": None,
            "observed_at": now,
            "checks_preflight": _count_statuses(checks["preflight"]),
            "static_checks_done": [
                name
                for name, ok in (
                    ("spec_valid", bool(spec_valid)),
                    ("checks_preflight", not unresolved),
                    ("scope_covered", not any(d["code"] == "POLICY_DENIED" for d in diagnostics)),
                )
                if ok
            ],
            "static_checks_skipped": [
                {"check": f"checks[{row.get('check_id')}]", "reason": f"status={row.get('status')}"}
                for row in checks["preflight"]
                if row.get("status") in ("deferred", "shell")
            ],
        },
        "next": _next_steps(registered, prepared, project_id),
    }
    report["snapshot"]["base_sha"] = report["repo"]["base_sha"]
    if emit:
        report["emitted"] = _emit_drafts(Path(emit), title, project_id, spec)
    if out:
        target = Path(out)
        if target.exists() and not force:
            raise Error(
                "STATE_CONFLICT",
                f"{target} already exists; pass --force to overwrite the report",
                details={"field": "out"},
            )
        atomic(target, report)
    return report


def _count_statuses(rows) -> dict[str, int]:
    counts = {"ok": 0, "shell": 0, "deferred": 0, "unresolved": 0, "invalid": 0}
    for row in rows or ():
        status = row.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _next_steps(registered: bool, prepared: bool, project_id: str | None) -> list[str]:
    if not registered:
        return [
            "zoe project register --config <profile>.json --dry-run",
            "zoe project register --config <profile>.json",
        ]
    if not prepared:
        return [
            "fix the diagnostics above, then re-run: zoe task prepare --project "
            + str(project_id)
            + " --spec <file>"
        ]
    return [
        "zoe task create --request-file <create.json>",
        "zoe task start <task_id>",
    ]
