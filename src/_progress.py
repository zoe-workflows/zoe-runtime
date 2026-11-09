"""Run observation facts (``runs/<run_id>/progress.json``).

The observation layer answers exactly two questions — *was there new output* and
*when was it observed* — and refuses to answer a third one (*is the agent
healthy*). Silence is not proof of a hang, and output is not proof of progress;
a host that needs a verdict must read ``state``/``phase``/``process_state`` too.

Design constraints (v2 §3-2):

* written by the backend's existing 0.3 s monitoring loop, never by a second
  watcher, so there is one observer per run;
* throttled by **both** a minimum interval and a minimum byte delta — a burst of
  output must not turn into a burst of writes;
* never stored in the state DB and never emitted as an event: the events table
  is the state-transition audit trail and ``watch`` queries it every round;
* thresholds come from the host environment, **not** from the project config,
  because ``policy_revision`` is a digest of that config: putting a hint
  threshold there would raise ``POLICY_CHANGED`` on in-flight tasks.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from src._util import atomic, read

#: Payload version of ``progress.json``.
API_VERSION = 2

#: Observation settings, deliberately host-level (see module docstring).
ENV_MIN_INTERVAL = "ZOE_PROGRESS_MIN_INTERVAL"
ENV_MIN_BYTES = "ZOE_PROGRESS_MIN_BYTES"
ENV_STALE_AFTER = "ZOE_OBSERVATION_STALE_SECONDS"

DEFAULT_MIN_INTERVAL = 10.0
DEFAULT_MIN_BYTES = 8 * 1024
DEFAULT_STALE_SECONDS = 120.0

#: States reported for the observed process; ``unknown`` is not an error.
ALIVE, EXITED, UNKNOWN = "alive", "exited", "unknown"


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def settings() -> dict:
    """Effective observation settings (host-level, never part of the policy)."""
    return {
        "min_write_interval": _env_float(ENV_MIN_INTERVAL, DEFAULT_MIN_INTERVAL),
        "min_byte_delta": _env_float(ENV_MIN_BYTES, DEFAULT_MIN_BYTES),
        "stale_after_seconds": _env_float(ENV_STALE_AFTER, DEFAULT_STALE_SECONDS),
        "source": "host_env",
    }


def path(store, t: dict, run_id: str) -> Path:
    return store.path(t) / "runs" / run_id / "progress.json"


class Observer:
    """Throttled writer for one run's ``progress.json``.

    One instance per backend execution; ``tick`` is cheap enough for the 0.3 s
    loop (one ``stat`` plus an in-memory comparison) and only occasionally
    writes.
    """

    def __init__(
        self,
        store,
        t: dict,
        run_id: str,
        log_path: Path,
        log_name: str = "agent",
        *,
        min_interval: float | None = None,
        min_bytes: float | None = None,
    ):
        self.store = store
        self.t = t
        self.run_id = run_id
        self.log_name = log_name
        self.log_path = Path(log_path)
        self.settings = settings()
        self.min_interval = (
            self.settings["min_write_interval"] if min_interval is None else float(min_interval)
        )
        self.min_bytes = self.settings["min_byte_delta"] if min_bytes is None else float(min_bytes)
        self.started_at = time.time()
        self.generation = 1
        self.bytes = 0
        self.last_output_at: float | None = None
        self._identity: tuple | None = None
        self._written_bytes = -1
        self._written_state: str | None = None
        self._last_write = 0.0
        self.path = path(store, t, run_id)

    # -- internal ---------------------------------------------------------- #
    def _sample(self) -> tuple[int, tuple | None]:
        """Read the log's size and identity; detect rotation/truncation."""
        try:
            stat = self.log_path.stat()
        except OSError:
            return self.bytes, self._identity
        identity = (stat.st_dev, stat.st_ino)
        if self._identity is not None and identity != self._identity:
            # A different file at the same path: new generation, offsets reset.
            self.generation += 1
            self.bytes = 0
        elif stat.st_size < self.bytes:
            # Same file, truncated in place: offset semantics restart as well.
            self.generation += 1
            self.bytes = 0
        if stat.st_size > self.bytes:
            self.last_output_at = time.time()
        self._identity = identity
        self.bytes = stat.st_size
        return self.bytes, identity

    def _payload(self, process_state: str) -> dict:
        observed_at = time.time()
        if self.last_output_at is not None:
            quiet = max(0.0, observed_at - self.last_output_at)
        else:
            # No output yet: silence is measured from launch, never invented.
            quiet = max(0.0, observed_at - self.started_at)
        return {
            "api_version": API_VERSION,
            "run_id": self.run_id,
            "log": self.log_name,
            "log_generation": self.generation,
            "bytes": self.bytes,
            "started_at": self.started_at,
            "observed_at": observed_at,
            "last_output_at": self.last_output_at,
            "quiet_seconds": round(quiet, 3),
            "process_state": process_state,
        }

    # -- public ------------------------------------------------------------ #
    def tick(self, process_state: str = ALIVE, *, force: bool = False) -> bool:
        """Sample and (maybe) write. Returns whether a write happened."""
        self._sample()
        now = time.time()
        due = now - self._last_write >= self.min_interval
        changed = (
            abs(self.bytes - self._written_bytes) >= self.min_bytes
            or process_state != self._written_state
        )
        if not (force or (due and changed)):
            return False
        atomic(self.path, self._payload(process_state))
        self._written_bytes = self.bytes
        self._written_state = process_state
        self._last_write = now
        return True

    def finish(self, process_state: str = EXITED) -> None:
        """Final write: the observation must say the process is gone."""
        self.tick(process_state, force=True)


class NullObserver:
    """Observer for executions that are not the agent turn.

    Verification commands reuse the run id but start a fresh ``Observer``
    (``started_at`` reset, ``bytes`` back to 0). Letting them write the same
    ``progress.json`` replaced the agent's observation with the last check's, while
    ``task status`` kept reporting it as fresh and current — so the numbers no
    longer described the process the file is named after.
    """

    def tick(self, process_state: str = ALIVE, *, force: bool = False) -> bool:
        return False

    def finish(self, process_state: str = EXITED) -> None:
        return None


def view(store, t: dict, run: dict | None, *, stale_after: float | None = None) -> dict | None:
    """Read ``progress.json`` and state what the numbers do and do not mean.

    Nothing here is recomputed from task state: a missing file stays ``None``
    rather than being replaced by an inferred progress value.
    """
    if not run or not run.get("run_id"):
        return None
    path_ = path(store, t, run["run_id"])
    try:
        data = read(path_)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    settings_ = settings()
    threshold = settings_["stale_after_seconds"] if stale_after is None else float(stale_after)
    now = time.time()
    observed_at = data.get("observed_at")
    age = max(0.0, now - float(observed_at)) if observed_at else None
    last_output = data.get("last_output_at")
    started = data.get("started_at")
    if last_output:
        quiet = max(0.0, now - float(last_output))
    elif started:
        quiet = max(0.0, now - float(started))
    else:
        quiet = None
    stale = bool(age is None or age > threshold)
    return {
        **data,
        "observed_age_seconds": None if age is None else round(age, 3),
        "quiet_seconds": None if quiet is None else round(quiet, 3),
        "stale_after_seconds": threshold,
        # 观测进程停了就不能再显示健康心跳：这个布尔是给人看的结论，
        # 但结论只关于"观测"，不关于"Agent 是否在推进"。
        "observations_stale": stale,
        "note": (
            "observation facts only: no output does not prove a hang, "
            "output does not prove progress"
        ),
    }
