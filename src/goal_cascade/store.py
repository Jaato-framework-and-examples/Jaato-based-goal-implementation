"""Durable due-row store for suspended goal sessions.

One JSON file holding at most one *pending resume* per session, written with
the atomic rename + fsync dance so a crash mid-write cannot corrupt it.

Why this exists: the clock lives in the driver process, but the driver is
allowed to die.  Anything the driver knows that is needed to resume a goal —
when, which session, and the state to replay back into it — has to survive
outside its memory.  On restart :meth:`GoalStore.due` returns whatever was
already due while it was gone.

Lifecycle of a row:

1. ``put()`` when the agent suspends — the row now owns the resume.
2. ``due()`` returns it once ``resume_at`` has passed.
3. ``claim()`` marks it in-flight and returns a deterministic resume key, so a
   double-fire (two drivers, or a restart mid-resume) cannot double-drive the
   session — the daemon dedups on that key via ``session.wake``'s ``event_id``.
4. ``drop()`` when the agent reports ``finished`` or the cascade gives up.

There is no separate "claimed" persistence step: ``claim()`` bumps ``attempt``
and rewrites the file, so the key changes only when a resume genuinely advances.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utcnow() -> datetime:
    """Current UTC time, timezone-aware (naive datetimes compare badly)."""
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Models emit ``Z`` far more often than ``+00:00``. A value without any
    offset is assumed UTC rather than rejected — being strict here would fail a
    goal over a formatting nit.
    """
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class DueRow:
    """One suspended session awaiting resume.

    Attributes:
        session_id: The session to wake. Resuming continues this SAME session,
            so its history is intact (see README on why that is safe).
        resume_at: ISO-8601 UTC instant at which the row becomes due.
        resume_reason: What the agent said it was waiting for. Human-facing.
        watch_handle: What the agent asked to re-inspect. Replayed verbatim.
        progress_note: The agent's note to its future self. Replayed verbatim.
            Together with ``watch_handle`` this is the ONLY state guaranteed to
            reach the resumed turn — history GC may have dropped the rest.
        attempt: How many times this session has been resumed. Part of the
            resume key, so each genuine resume is distinct and each retry of the
            *same* resume is deduped.
    """

    session_id: str
    resume_at: str
    resume_reason: str = ""
    watch_handle: Dict[str, Any] = field(default_factory=dict)
    progress_note: str = ""
    attempt: int = 0

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """True when ``resume_at`` has passed.

        An unparseable ``resume_at`` counts as due: a malformed timestamp
        should surface as a turn the agent can correct, not as a goal that
        silently never resumes.
        """
        try:
            return parse_iso(self.resume_at) <= (now or utcnow())
        except (ValueError, TypeError):
            return True

    def resume_key(self) -> str:
        """Deterministic idempotency key for this session+attempt.

        Passed to ``session.wake`` as ``event_id``; the daemon treats a repeat
        as a benign duplicate, so a driver that crashes between waking and
        recording the result cannot drive the same resume twice.
        """
        return f"goal-resume:{self.session_id}:{self.attempt}"


class GoalStore:
    """Crash-safe collection of :class:`DueRow`, keyed by session id.

    The file is rewritten in full on every mutation. That is fine at this
    scale (one row per live goal) and removes any partial-update failure mode.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._rows: Dict[str, DueRow] = {}
        self._load()

    def _load(self) -> None:
        """Read rows from disk; a missing or corrupt file starts empty.

        A corrupt file is tolerated rather than fatal — losing the schedule is
        recoverable (the goal resumes late), while refusing to start is not.
        """
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw.get("rows", []):
            try:
                row = DueRow(**item)
            except TypeError:
                continue
            self._rows[row.session_id] = row

    def _save(self) -> None:
        """Persist atomically: write a temp file, fsync, then rename over."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rows": [asdict(r) for r in self._rows.values()]}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put(self, row: DueRow) -> None:
        """Record (or replace) the pending resume for a session."""
        self._rows[row.session_id] = row
        self._save()

    def drop(self, session_id: str) -> None:
        """Forget a session — it finished, errored, or was abandoned."""
        if self._rows.pop(session_id, None) is not None:
            self._save()

    def get(self, session_id: str) -> Optional[DueRow]:
        """Return the pending row for a session, if any."""
        return self._rows.get(session_id)

    def all(self) -> List[DueRow]:
        """Every pending row, soonest first."""
        return sorted(self._rows.values(), key=lambda r: r.resume_at)

    def due(self, now: Optional[datetime] = None) -> List[DueRow]:
        """Rows whose ``resume_at`` has passed, soonest first.

        On driver restart this is what recovers a goal that came due while no
        driver was running.
        """
        moment = now or utcnow()
        return [r for r in self.all() if r.is_due(moment)]

    def claim(self, session_id: str) -> Optional[DueRow]:
        """Mark a row in-flight and return it with its attempt advanced.

        Advancing before the wake — never after — is what makes a crash
        mid-resume safe: the next run computes a *new* resume key only if the
        claim was persisted, and the daemon dedups the old one.
        """
        row = self._rows.get(session_id)
        if row is None:
            return None
        row.attempt += 1
        self._save()
        return row

    def seconds_until_next(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds until the earliest pending row is due.

        ``None`` when nothing is pending; ``0.0`` when something is already due.
        The driver sleeps on this rather than polling on a fixed tick.
        """
        moment = now or utcnow()
        waits: List[float] = []
        for row in self._rows.values():
            try:
                waits.append((parse_iso(row.resume_at) - moment).total_seconds())
            except (ValueError, TypeError):
                return 0.0
        if not waits:
            return None
        return max(0.0, min(waits))
