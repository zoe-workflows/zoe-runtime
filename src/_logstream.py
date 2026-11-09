"""Incremental, resumable reading of ``runs/<run_id>/<log>.log``.

``task logs`` could already return a bounded tail, but a tail is not a stream:
the host had to re-stat, re-read and guess which bytes were new. This module
supplies the missing half — a cursor and a follower — plus a per-line projection
that turns an adapter's ``stream-json`` records into small, comparable events.

Boundaries that are deliberate (v2 §3-3):

* the cursor is ``run_id:log_generation:byte_offset`` — an offset alone cannot
  survive rotation, and a run id alone cannot survive a restart;
* only **complete** lines are emitted; a partial line stays buffered, so a
  resume never replays half a record and never invents a lookahead;
* every emitted string is redacted, including "raw" text — projection is about
  shape, never about bypassing masking;
* the record classifier is **not** the terminal-result parser: ``parse_output``
  looks for the last ``type=result`` record, which is exactly the thing a
  follower must not wait for.
"""

from __future__ import annotations

import codecs
import json
import os
import re
from pathlib import Path

from src._util import redact_secrets

#: ``run_id:generation:offset``; run ids never contain ``:`` (safe_id).
CURSOR = re.compile(r"^(?P<run>[^:\s]+):(?P<generation>\d+):(?P<offset>\d+)$")

#: Defaults; all bounded so one pathological line cannot exhaust memory.
MAX_LINE_BYTES = 64 * 1024
MAX_READ_BYTES = 256 * 1024

#: Snapshot window defaults (v2.2 §S8-C/D). ``--tail-bytes`` defaults to ``None``
#: in the parser so "not given" and "given as 65536" stay distinguishable:
#: ``--follow`` must keep its old "start at EOF" meaning unless the host really
#: asked for a look-back window.
DEFAULT_TAIL_BYTES = 65536
MAX_TAIL_BYTES = 1024 * 1024

#: Snapshot event cap: a window of 1 MiB can hold thousands of records, and the
#: host wants "what happened lately", not the whole window. Truncation keeps the
#: **most recent** events and is reported separately from byte truncation.
MAX_SNAPSHOT_EVENTS = 200

#: Tool input fields worth surfacing, in priority order (path-ish first).
_TARGET_FIELDS = ("file_path", "path", "notebook_path", "target_file", "command", "pattern", "url")

_TEXT_LIMIT = 400
_TARGET_LIMIT = 120


def parse_cursor(text: str | None) -> tuple[str, int, int] | None:
    """Parse ``run:gen:offset``; ``None`` means "no usable cursor" (never guess)."""
    if not text:
        return None
    match = CURSOR.match(str(text).strip())
    if not match:
        return None
    return match.group("run"), int(match.group("generation")), int(match.group("offset"))


def format_cursor(run_id: str, generation: int, offset: int) -> str:
    return f"{run_id}:{int(generation)}:{int(offset)}"


def _clip(text: str, limit: int) -> str:
    return _clip_marked(text, limit)[0]


def _clip_marked(text: str, limit: int) -> tuple[str, bool]:
    """Collapse whitespace, cut to ``limit``, and say whether a cut happened.

    The flag is what makes "content truncation" countable separately from byte
    or event truncation (§S8-D-3): a host must not read ``events_truncated``
    when what happened was one long sentence losing its tail.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed, False
    return collapsed[: limit - 1] + "…", True


def _target(payload) -> str | None:
    """Best-effort "what did this tool touch", without pretending to be exact."""
    if isinstance(payload, str):
        return _clip(payload, _TARGET_LIMIT)
    if not isinstance(payload, dict):
        return None
    for field in _TARGET_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            if field == "command":
                # The first token identifies the program; the rest is often a
                # heredoc or a many-line script and belongs in the raw log.
                value = value.strip().split()[0]
            return _clip(value, _TARGET_LIMIT)
    return None


def _blocks(message) -> list:
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
    return []


def project_record(record: dict) -> list[dict]:
    """Map one ``stream-json`` record to zero or more compact events.

    Unknown shapes degrade to ``{"kind": "record", "type": ...}`` rather than
    being dropped: an adapter we do not recognise should still be followable.
    """
    kind = record.get("type")
    if kind == "assistant" or kind == "user":
        out = []
        for block in _blocks(record.get("message") or record):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text") or ""
                if text.strip():
                    clipped_text, clipped = _clip_marked(text, _TEXT_LIMIT)
                    event = {"kind": "text", "text": clipped_text}
                    if clipped:
                        event["clipped"] = True
                    out.append(event)
            elif block_type == "tool_use":
                out.append(
                    {
                        "kind": "tool",
                        "name": block.get("name") or "tool",
                        "target": _target(block.get("input")),
                        "role": kind,
                    }
                )
            elif block_type == "tool_result":
                out.append(
                    {
                        "kind": "tool_result",
                        "ok": not bool(block.get("is_error")),
                        "role": kind,
                    }
                )
        if out:
            return out
        return [{"kind": "record", "type": kind}]
    if kind == "result":
        return [
            {
                "kind": "result",
                "subtype": record.get("subtype"),
                "is_error": bool(record.get("is_error")),
                "duration_ms": record.get("duration_ms"),
                "num_turns": record.get("num_turns"),
            }
        ]
    if kind == "system":
        return [
            {
                "kind": "init",
                "subtype": record.get("subtype"),
                "model": record.get("model"),
                "session_id": record.get("session_id"),
            }
        ]
    return [{"kind": "record", "type": kind or "unknown"}]


def _redact_tree(value):
    """Redact every string inside a projected event, keys included are left alone."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_tree(item) for key, item in value.items()}
    return value


def project_line(text: str, projection: str = "auto") -> list[dict]:
    """Turn one complete log line into JSON-serialisable events.

    ``auto`` classifies per line — a startup banner followed by structured
    records is normal, so "first line decides forever" would be wrong. ``raw``
    keeps the text but still masks secrets; ``events`` is ``auto`` without the
    text fallback (used when a host wants only structured actions).

    A blank line produces **nothing**. That is a deliberate behaviour change
    (v2.2 §1.4): the follower used to emit ``{"kind":"raw","text":""}`` for every
    blank line while a snapshot skipped them, so the two "same" views of one
    byte range could never agree.
    """
    if not text.strip():
        return []
    raw_text, raw_clipped = _clip_marked(text, _TEXT_LIMIT)
    raw = {"kind": "raw", "text": raw_text}
    if raw_clipped:
        raw["clipped"] = True
    if projection == "raw":
        return [_redact_tree(raw)]
    try:
        record = json.loads(text.strip())
    except ValueError:
        record = None
    if isinstance(record, dict):
        events = _redact_tree(project_record(record))
        if projection == "events":
            # ``record`` is the "we saw an envelope but could not extract
            # anything" fallback. Useful in ``auto`` as a breadcrumb, pure
            # noise when the host asked for actions only.
            return [event for event in events if event.get("kind") != "record"]
        return events
    if projection == "events":
        return []
    return [_redact_tree(raw)]


def window_start(path, size: int, window: int) -> int:
    """Start offset of the newest window that begins on a record boundary.

    The rule is **forward alignment** (v2.2 §S8-C): never look further back than
    ``size - window``, then skip the partial head until the next newline. Backing
    up to the previous newline would be unbounded on a file without any.

    When ``size - window`` already sits right after a newline, that position is
    kept: the line starting there is inside the window and dropping it would
    lose one window's worth of records at the boundary.
    """
    if size <= 0:
        return 0
    if window <= 0:
        return size
    start = max(0, size - window)
    if start == 0:
        return 0
    with open(path, "rb") as handle:
        handle.seek(start - 1)
        if handle.read(1) == b"\n":
            return start
        handle.seek(start)
        probe = handle.read(min(window, size - start))
    index = probe.find(b"\n")
    return size if index < 0 else start + index + 1


def align_offset(path, offset: int, size: int) -> int:
    """Align a caller-supplied ``--after`` offset **forward** to a boundary.

    Same rule as :func:`window_start` and for the same reason: a cursor we did
    not produce (a hand-written one, or one from a different window) must not
    make us replay a half record, and must not make us scan backwards without a
    bound. ``offset`` is returned unchanged when it already follows a newline.
    """
    offset = int(offset)
    if offset <= 0:
        return 0
    if offset >= size:
        return size
    with open(path, "rb") as handle:
        handle.seek(offset - 1)
        if handle.read(1) == b"\n":
            return offset
        handle.seek(offset)
        chunk = handle.read(MAX_READ_BYTES)
    index = chunk.find(b"\n")
    return offset + index + 1 if index >= 0 else size


def complete_offset(data: bytes, base: int) -> int:
    """Absolute end offset of the last complete record in ``data``."""
    index = data.rfind(b"\n")
    return base + index + 1 if index >= 0 else base


def read_window(path, start: int, length: int) -> bytes:
    """Bounded read: never loads the whole log (v2.2 §S8-D-1)."""
    if length <= 0:
        return b""
    with open(path, "rb") as handle:
        handle.seek(int(start))
        return handle.read(int(length))


def one_record_span(path, start: int, size: int, window: int) -> int:
    """Read length that guarantees at least one complete record when possible.

    A caller resuming with ``--after`` and a small ``--tail-bytes`` would
    otherwise stop making progress on any record longer than the window: the
    cursor cannot advance over an incomplete record, so every page would be
    empty and ``has_more`` would stay true forever. The window therefore grows
    to the end of the next record, capped at ``MAX_READ_BYTES`` — the same
    ceiling ``LogFollower`` uses for one line, so paging and following agree on
    what "one record" can cost.
    """
    available = max(0, size - int(start))
    if available == 0 or window <= 0:
        return 0
    base = min(int(window), available)
    if base >= available:
        return base
    cap = min(available, MAX_READ_BYTES)
    data = read_window(path, start, cap)
    index = data.find(b"\n")
    if index < 0:
        return cap
    return max(base, index + 1)


def project_complete_lines(data: bytes, base: int, projection: str) -> tuple[list[dict], int]:
    """Byte-level framing → per-line decode → projection.

    Returns ``(events, complete_end)``. Only complete records are projected; the
    partial tail stays out of ``events`` (the snapshot still returns it as
    ``text`` for debugging).

    The *rule* is shared with the follower, but not this call site: the follower
    frames continuously (it must buffer a half line across polls) and therefore
    calls :func:`project_line` per complete line. What keeps the two views equal
    is :func:`project_line` itself — it projects a blank line to nothing and
    classifies per line, so "skip blanks" and "one rule per line" cannot drift
    apart (§S8-D-4).
    """
    events: list[dict] = []
    end = int(base)
    for raw_line in data.split(b"\n")[:-1]:
        end += len(raw_line) + 1
        text = raw_line.decode("utf-8", "replace")
        if text.strip():
            events.extend(project_line(text, projection))
    return events, end


class LogFollower:
    """Follow one log file with a resumable cursor.

    ``poll`` is non-blocking: it returns whatever complete lines appeared since
    the last call, plus rotation notifications. Blocking (timeouts, sleeps) stays
    in the CLI so the follower stays trivially testable.
    """

    def __init__(
        self,
        path,
        *,
        offset: int = 0,
        generation: int = 1,
        max_line_bytes: int = MAX_LINE_BYTES,
        max_read_bytes: int = MAX_READ_BYTES,
    ):
        self.path = Path(path)
        self.generation = int(generation)
        self.offset = int(offset)
        self.max_line_bytes = int(max_line_bytes)
        self.max_read_bytes = int(max_read_bytes)
        self._fh = None
        self._identity: tuple | None = None
        self._pending = b""
        self._pending_base = int(offset)
        self._emitted = int(offset)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._opened()

    # -- cursor ------------------------------------------------------------ #
    def cursor(self, run_id: str) -> dict:
        """Where to resume: the end of the last **complete** line."""
        return {
            "run_id": run_id,
            "log_generation": self.generation,
            "offset": self._emitted,
            "text": format_cursor(run_id, self.generation, self._emitted),
        }

    # -- internals --------------------------------------------------------- #
    def _opened(self) -> None:
        try:
            self._fh = open(self.path, "rb")
        except OSError:
            self._fh = None
            return
        stat = os.fstat(self._fh.fileno())
        self._identity = (stat.st_dev, stat.st_ino)
        self._fh.seek(self.offset)
        self._pending_base = self.offset
        self._emitted = self.offset

    def _detect_rotation(self) -> dict | None:
        """Identity change or in-place truncation; size<offset alone is not enough."""
        try:
            stat = os.stat(self.path)
        except OSError:
            return None
        identity = (stat.st_dev, stat.st_ino)
        if self._identity is not None and identity != self._identity:
            return {"kind": "rotated", "reason": "replaced"}
        if stat.st_size < self.offset - len(self._pending):
            return {"kind": "rotated", "reason": "truncated"}
        return None

    def _reset(self, reason: str) -> dict:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None
        self.generation += 1
        self.offset = 0
        self._pending = b""
        self._pending_base = 0
        self._emitted = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._opened()
        return {
            "kind": "rotated",
            "reason": reason,
            "log_generation": self.generation,
            "offset": 0,
        }

    def _take_lines(self) -> list[dict]:
        out = []
        while True:
            index = self._pending.find(b"\n")
            if index < 0:
                break
            raw_line = self._pending[:index]
            self._pending = self._pending[index + 1 :]
            text = self._decoder.decode(raw_line, False)
            # The cursor still advances over a blank line — it is a real byte
            # range — but no event is produced: ``project_line`` skips blanks,
            # so the follower and the snapshot agree byte for byte (§S8-D-4).
            self._emitted = self._pending_base + index + 1
            self._pending_base = self._emitted
            if text.strip():
                out.append({"kind": "line", "text": text, "truncated": False})
        if len(self._pending) > self.max_line_bytes:
            # A single line larger than the cap: emit what we have, marked, and
            # keep going. Buffering without limit would be a memory bug.
            text = self._decoder.decode(self._pending, False)
            self._emitted = self._pending_base + len(self._pending)
            self._pending_base = self._emitted
            self._pending = b""
            if text.strip():
                out.append({"kind": "line", "text": text, "truncated": True})
        return out

    # -- public ------------------------------------------------------------ #
    def poll(self) -> list[dict]:
        events: list[dict] = []
        if self._fh is None:
            if self.path.exists():
                self._opened()
                events.append(
                    {
                        "kind": "opened",
                        "log_generation": self.generation,
                        "offset": self.offset,
                    }
                )
            else:
                return events
        rotation = self._detect_rotation()
        if rotation is not None:
            return [self._reset(rotation["reason"])]
        try:
            chunk = self._fh.read(self.max_read_bytes)
        except (OSError, ValueError):
            return events
        if not chunk:
            return events
        self.offset += len(chunk)
        self._pending += chunk
        events.extend(self._take_lines())
        return events


def tail_window(store_path, run_id: str, log_name: str, since_bytes: int = 0):
    """Convenience path helper so callers do not rebuild run directories."""
    return Path(store_path) / "runs" / run_id / (log_name + ".log")
