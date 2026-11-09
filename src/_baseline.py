"""并行批次的基线观测：目标分支在任务基线之后前进了什么（v1.6）。

回答的问题
----------
"这个候选的基线之后，目标分支又合入了哪些改动？其中哪些和这个候选撞在同一批
文件上、甚至被这个候选删掉了？"

为什么**不是** ``BASE_DRIFT``
-----------------------------
``BASE_DRIFT``（退出码 4）回答的是另一个问题："冻结基线已经不在目标分支历史里"
（rebase / force-push）。那是一个**拒绝**，下一步是重冻结。

本模块回答的是并行批次的常态：目标分支前进了几个提交。把它当作失败会造成两种
误判，而两种都在真实执行里发生过：

* 把"落后 N 个提交"判成失败 —— 大多数情况下 3-way 合并能自动处理，拒绝只会逼宿主
  ``merge --abort`` 重派，白花十几分钟；
* 把"重叠"判成冲突 —— 重叠只说明合并后**必须重新验证**，不说明合并会失败。

所以这里只产出事实，``kind`` 只是分类而不是结论：
``behind_by`` / ``overlapping_paths`` / ``reverts_merged_work``。

一个真会丢代码的信号
--------------------
``reverts_merged_work`` 是"候选删掉了某个文件，而那个文件在基线之后被别人改过"。
几何上它不常出现，一旦出现就是**合并会丢掉别人刚合入的改动**——这正是手工合并时
用 ``git diff --stat HEAD..cand`` 看到"删除已合入代码"的那一类灾难，只是这里比较的是
基线、不是当前 HEAD，所以不会把"候选还没跟上"误报成"候选在删代码"。

结构约定（与 ``_drift`` 同一立场）
----------------------------------
:func:`probe` **绝不抛异常**。它跑在即将进入 ``REVIEW_READY`` 的路径上，那里任何异常
都会把一条已验证成功的任务变成 ``BLOCKED``——用一个提示性事实换掉一个真实结论，
方向正好反了。读不到就是 ``unknown``，不是 ``none``。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

#: 只读查询的上限：一个卡住的 git 不该拖住整个流水线。
QUERY_TIMEOUT_SECONDS = 30

KIND_NONE = "none"
KIND_BASE_ADVANCED = "base_advanced"
KIND_OVERLAP = "overlap"
KIND_REVERTS = "reverts"
KIND_UNKNOWN = "unknown"

#: 列在响应里的路径上限：这是提示，不是清单导出。
PATH_LIMIT = 50

_MESSAGES = {
    KIND_NONE: "目标分支未在任务基线之后前进。",
    KIND_BASE_ADVANCED: "目标分支已前进，但改动与本次候选没有重叠文件。",
    KIND_OVERLAP: "目标分支的改动与本次候选落在同一批文件上：合并后必须重新验证，不能沿用合并前的证据。",
    KIND_REVERTS: "候选删除了基线之后被别人改过的文件：合并会丢掉那些改动，必须人工处理。",
    KIND_UNKNOWN: "无法确认目标分支相对任务基线的状态，不能当作没有前进。",
}


def _git(repo: Path, *args: str, timeout: float = QUERY_TIMEOUT_SECONDS):
    """Run a read-only git command and return ``(code, stdout)``; never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    if proc.returncode != 0:
        return proc.returncode, ""
    return 0, proc.stdout or ""


def _lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


def _result(kind: str, reason: str | None = None, **kwargs) -> dict:
    value = {
        "kind": kind,
        "reason": reason,
        "message": _MESSAGES.get(kind),
        "base_sha": None,
        "observed_base": None,
        "candidate_sha": None,
        "base_ref": None,
        "behind_by": None,
        "overlapping_paths": [],
        "reverts_merged_work": [],
        "checked_at": time.time(),
    }
    value.update(kwargs)
    return value


def probe(repo, base_sha, candidate_sha, base_ref: str = "HEAD") -> dict:
    """Classify what the target branch did after the task baseline.

    Returns ``{"kind", "reason", "behind_by", "overlapping_paths",
    "reverts_merged_work", ...}`` and never raises.
    """
    try:
        return _probe(Path(repo), base_sha, candidate_sha, base_ref)
    except Exception:  # noqa: BLE001 - structural guarantee, see module docstring
        return _result(KIND_UNKNOWN, "git_error", base_sha=base_sha, candidate_sha=candidate_sha)


def _probe(repo: Path, base_sha, candidate_sha, base_ref: str) -> dict:
    if not base_sha or not candidate_sha:
        # 没有候选就没有"回退"可言：这不是缺口，是还没走到那一步。
        return _result(
            KIND_NONE,
            "no_candidate",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            base_ref=base_ref,
        )

    code, out = _git(repo, "rev-parse", "--verify", "--quiet", str(base_ref))
    observed = out.strip()
    if code != 0 or not observed:
        return _result(
            KIND_UNKNOWN,
            "base_unresolved",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            base_ref=base_ref,
        )

    code, out = _git(repo, "rev-list", "--count", f"{base_sha}..{observed}")
    behind = int(out.strip()) if code == 0 and out.strip().isdigit() else None
    if behind is None:
        return _result(
            KIND_UNKNOWN,
            "count_failed",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            base_ref=base_ref,
            observed_base=observed,
        )
    if behind == 0:
        return _result(
            KIND_NONE,
            "base_unchanged",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            base_ref=base_ref,
            observed_base=observed,
            behind_by=0,
        )

    code, out = _git(repo, "diff", "--name-only", f"{base_sha}..{observed}")
    if code != 0:
        return _result(
            KIND_UNKNOWN,
            "diff_failed",
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            base_ref=base_ref,
            observed_base=observed,
            behind_by=behind,
        )
    moved_paths = set(_lines(out))

    code, out = _git(repo, "diff", "--name-only", f"{base_sha}..{candidate_sha}")
    candidate_paths = set(_lines(out))
    if code == 0:
        overlapping = sorted(moved_paths & candidate_paths)
    else:
        # 候选读不到时不用"空重叠"冒充"没有重叠"。
        overlapping = []
        candidate_paths = set()

    reverts: list[str] = []
    code, out = _git(repo, "diff", "--diff-filter=D", "--name-only", f"{base_sha}..{candidate_sha}")
    if code == 0:
        deleted = set(_lines(out))
        # 只保留"基线之后被改过"的那些：其余删除是基线内已有内容的正常删除。
        reverts = sorted(deleted & moved_paths)

    kind = KIND_REVERTS if reverts else (KIND_OVERLAP if overlapping else KIND_BASE_ADVANCED)
    return _result(
        kind,
        None,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        base_ref=base_ref,
        observed_base=observed,
        behind_by=behind,
        overlapping_paths=overlapping[:PATH_LIMIT],
        reverts_merged_work=reverts[:PATH_LIMIT],
    )
