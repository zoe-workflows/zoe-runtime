"""工作区基线与隔离（v1.2 §12.9）。

两件事，语义相反，绝不能混：

* ``baseline()``：``start`` 在 spawn **之前**落盘"启动时长什么样"。没有它就无法
  区分"本次 run 的改动"与"用户原有的未提交改动"，盲目清理会删掉用户的工作；
* ``quarantine()``：闸门（模型身份 / 费用）判定该 run 不可交付时，把改动**移出**
  工作区但不丢——归档为 ``runs/<id>/worktree.patch`` 并**在可还原性校验通过后**
  才恢复干净。

三个刻意的选择：

1. 先 ``add -A`` 再 ``diff --cached``：只用 ``git diff`` 会漏掉新增文件，而新增
   文件恰恰是最常见的交付物；
2. 必须 ``git apply --check --reverse`` 通过才丢弃：这是"能还原"的可执行证明，
   不是主观判断。校验不过就不清理 —— **丢数据的风险远大于误提交的风险**；
3. ``clean -fd`` 不加 ``-x`` 且限定 ``allowed_paths``：不加 ``-x`` 才不会删
   ``node_modules``/``venv`` 这类 ignored 目录，限定路径避免清到仓库外的产物。
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

from src._util import Error

#: 归档文件相对 run 目录的名字（属于任务证据，不得自动删除）。
PATCH_NAME = "worktree.patch"


def _git(repo: Path, *args, text: bool = True):
    """Run git with the same hardening ``_util.git`` applies, but bytes-capable.

    A binary patch cannot round-trip through ``str`` with ``text=True``: the
    cached diff of a binary file (or any non-UTF-8 blob) would be corrupted and
    the "restorable" claim would be false.
    """
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.attributesFile=/dev/null",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=text,
        timeout=120,
    )
    if proc.returncode != 0:
        stderr = (
            proc.stderr
            if isinstance(proc.stderr, str)
            else (proc.stderr or b"").decode("utf-8", "replace")
        )
        raise Error("ENVIRONMENT_ERROR", (stderr or "git command failed")[-2000:])
    return proc.stdout


def _git_rc(repo: Path, *args) -> int | None:
    """同 ``_git`` 的加固参数，但返回退出码。

    存在的理由只有一个：``merge-base --is-ancestor`` 用 ``1`` 表示"否"，那是**答案**
    不是错误，而 ``_git`` 会把非 0 一律转成 ``ENVIRONMENT_ERROR``。
    ``None`` 表示连判定都做不到（git 缺失、超时、仓库不可读）。
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.attributesFile=/dev/null",
                *args,
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode


def base_reachable(repo: Path, base_sha: str | None, base_ref: str = "HEAD") -> bool | None:
    """``base_sha`` 是否仍可作为 ``base_ref`` 的祖先（v1.6）。

    三个答案必须分开：

    * ``True``：基线仍在目标分支历史里。分支只是前进了（``base_ref`` 有了新提交），
      这**不是**漂移——任务在冻结基线上工作、最后由宿主合并，本来就是设计；
    * ``False``：基线已不在该历史里（rebase / force-push / 分支被改写）。此时再开
      worktree，worker 会在一棵"已被丢弃的树"上干活，最后以 ``VALIDATION_FAILED``
      收场：时间白花，现象还像代码问题；
    * ``None``：判定不了（git 缺失、超时、ref 不存在、仓库不可读）。**不阻断**——
      这是"提前失败"的优化，不是安全边界，真正的校验仍在 verify 阶段。观测失败
      不该变成拒绝的理由（与 ``PROCESS_UNKNOWN`` 的处置不同：那里是写者状态未知）。
    """
    if not base_sha:
        return None
    root = Path(repo)
    if not root.is_dir():
        return None
    exists = _git_rc(root, "cat-file", "-e", f"{base_sha}^{{commit}}")
    if exists is None:
        return None
    if exists != 0:
        # 仓库可读却没有这个对象：基线真的没了（gc 把它回收了，或强推后不可达）。
        # 仓库本身不可读时同样返回 1，所以要先确认"仓库是好的"再说"基线没了"。
        readable = _git_rc(root, "rev-parse", "--git-dir")
        return False if readable == 0 else None
    ancestor = _git_rc(root, "merge-base", "--is-ancestor", base_sha, base_ref)
    if ancestor in (0, 1):
        return ancestor == 0
    return None


def _porcelain(repo: Path) -> list[str]:
    text = _git(repo, "status", "--porcelain") or ""
    paths = []
    for line in text.splitlines():
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.append(path)
    return paths


def baseline(t: dict) -> dict:
    """Snapshot the workspace **before** spawning anything (§12.9.1)."""
    workspace = Path(t["workspace"])
    if not workspace.exists():
        # 第一次派发时 worktree 还没建：它会被 `run_pipeline` 从 ``base_sha`` 重新
        # 创建，因此"启动时干净"是构造事实，而不是猜测。不把这种情况记成
        # "无法采集基线"，否则第一次运行的隔离永远只能退化为保留。
        return {
            "head": t.get("base_sha"),
            "dirty_paths": [],
            "porcelain_sha256": hashlib.sha256(b"").hexdigest(),
            "captured_at": time.time(),
            "available": True,
            "note": "workspace not created yet; it will be created from base_sha",
        }
    try:
        head = (_git(workspace, "rev-parse", "HEAD") or "").strip() or None
        dirty = _porcelain(workspace)
    except (Error, OSError, subprocess.SubprocessError) as exc:
        return {
            "head": None,
            "dirty_paths": [],
            "porcelain_sha256": None,
            "captured_at": time.time(),
            "available": False,
            "note": f"baseline could not be captured: {exc}",
        }
    return {
        "head": head,
        "dirty_paths": dirty,
        "porcelain_sha256": hashlib.sha256("\n".join(dirty).encode()).hexdigest(),
        "captured_at": time.time(),
        "available": True,
    }


def quarantine(store, t: dict, run: dict, reason: str) -> dict:
    """Archive the run's work as a patch, then restore a clean workspace.

    Returns the record that goes into ``block_details`` / the
    ``worktree_quarantined`` event. Never raises for a *refused* cleanup: the
    refusal itself is the honest outcome, and the workspace stays untouched.
    """
    workspace = Path(t["workspace"])
    run_dir = store.path(t) / "runs" / run["run_id"]
    patch_path = run_dir / PATCH_NAME
    base = run.get("workspace_baseline") or {}
    record: dict = {
        "reason": reason,
        "run_id": run["run_id"],
        "patch_path": str(patch_path),
        "patch_sha256": None,
        "patch_bytes": 0,
        "checkout_paths": [],
        "cleanup": "skipped",
        "detail": None,
        "at": time.time(),
    }

    # 幂等：已隔离过就不再执行，避免二次破坏。已有归档要如实回报摘要，
    # 否则第二次调用的记录看起来像"什么都没隔离"。
    if patch_path.exists():
        existing = patch_path.read_bytes()
        record.update(
            cleanup="skipped",
            detail="already quarantined",
            patch_sha256=hashlib.sha256(existing).hexdigest(),
            patch_bytes=len(existing),
        )
        return record
    if not workspace.exists() or not (workspace / ".git").exists():
        record["detail"] = "workspace missing or not a git checkout"
        return record
    if not base.get("available", False):
        record["detail"] = "no workspace baseline was captured at start"
        return record
    if base.get("dirty_paths"):
        # 不清理：启动时就有用户改动，无法区分哪些是本次 run 的。
        record["detail"] = "skipped: workspace was dirty at start"
        return record

    # 与 ``_backend.scope`` 同一表达式：任务级范围优先，``None`` 才沿用项目。
    task_scope = t.get("task_scope")
    allowed = list(
        (t.get("config", {}).get("allowed_paths") or []) if task_scope is None else task_scope
    )
    try:
        _git(workspace, "add", "-A")
        patch = _git(workspace, "diff", "--cached", "--binary", text=False) or b""
        _git(workspace, "reset")
    except (Error, OSError, subprocess.SubprocessError) as exc:
        record["detail"] = f"could not build the patch: {exc}"
        return record

    if not patch:
        record.update(cleanup="done", detail="no changes to quarantine")
        return record

    run_dir.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(patch)
    record.update(
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        patch_bytes=len(patch),
        # 归档怎么读，写进记录本身：否则使用者会先尝试 `git apply`（唯一不被
        # 支持的用法），而不是先看 stat。
        inspect=f"git apply --stat {patch_path}",
    )

    # 可还原性校验：这是最关键的一步。校验不过就**不动工作区**。
    try:
        subprocess.run(
            ["git", "apply", "--check", "--reverse", str(patch_path)],
            cwd=str(workspace),
            capture_output=True,
            timeout=120,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        record.update(cleanup="aborted", detail=f"patch is not restorable: {exc}")
        return record

    try:
        _git(workspace, "checkout", "--", ".")
        record["checkout_paths"] = list(allowed)
        if allowed:
            # 只清理 allowed_paths 内、且未忽略的文件：`-x` 会连 node_modules 一起删。
            _git(workspace, "clean", "-fd", "--", *allowed)
    except (Error, OSError, subprocess.SubprocessError) as exc:
        record.update(cleanup="partial", detail=f"cleanup stopped early: {exc}")
        return record

    residue = _porcelain(workspace)
    if residue:
        record.update(
            cleanup="partial",
            detail="workspace still has changes after cleanup",
            residue_paths=residue,
        )
    else:
        record.update(cleanup="done", detail=None)
    return record
