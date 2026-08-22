"""Tests for the driver's decision logic, with the daemon faked out.

These cover the branch handling and the continuation contract — the parts that
decide whether a goal advances — without needing a running daemon or a model.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from goal_cascade.driver import GoalCascade
from goal_cascade.store import DueRow, utcnow


def _cascade(tmp_path) -> GoalCascade:
    cascade = GoalCascade(
        "get the job to pass",
        workspace=str(tmp_path),
        env_file=str(tmp_path / ".env"),
        log=lambda _msg: None,
    )
    cascade.session_id = "s1"
    return cascade


def test_suspend_persists_the_state_that_must_survive(tmp_path):
    """progress_note and watch_handle are what the resumed turn inherits."""
    cascade = _cascade(tmp_path)
    resume_at = (utcnow() + timedelta(minutes=2)).isoformat()

    row = cascade._suspend({
        "outcome": "suspended",
        "progress_note": "fixed retries, restarted run #2",
        "resume_at": resume_at,
        "resume_reason": "run #2 still going",
        "watch_handle": {"status_file": "fixtures/.job-status.json", "run": 2},
    })

    assert row.progress_note == "fixed retries, restarted run #2"
    assert row.watch_handle["run"] == 2
    assert cascade.store.get("s1").resume_at == resume_at


def test_suspend_without_resume_at_is_a_contract_violation(tmp_path):
    """A missing timestamp must fail loudly, not become an invented wait.

    The driver used to default to "one minute from now", which scheduled a
    resume the agent never asked for and could not see. Enforcement now lives
    in the completion processor, which blocks a `suspended` payload with no
    `resume_at` server-side — so reaching the driver without one means the
    contract itself broke, and that should surface rather than be smoothed over.
    """
    cascade = _cascade(tmp_path)
    with pytest.raises(KeyError):
        cascade._suspend({"outcome": "suspended", "progress_note": "n"})
    assert cascade.store.seconds_until_next() is None


def test_continuation_replays_note_and_handle_verbatim(tmp_path):
    """The whole safety argument for same-session resume lives in this text.

    History GC may have dropped everything else, so these two fields must
    appear in the woken turn's prompt.
    """
    row = DueRow(
        session_id="s1",
        resume_at=utcnow().isoformat(),
        resume_reason="run #2 still going",
        watch_handle={"status_file": "fixtures/.job-status.json"},
        progress_note="fixed retries, restarted run #2",
    )

    text = GoalCascade._continuation_text(row)

    assert "fixed retries, restarted run #2" in text
    assert "fixtures/.job-status.json" in text
    assert "run #2 still going" in text
    assert "signal_completion" in text


def test_continuation_is_readable_when_the_agent_recorded_nothing(tmp_path):
    """A sparse suspend still produces a usable prompt, not a broken one."""
    text = GoalCascade._continuation_text(
        DueRow(session_id="s1", resume_at=utcnow().isoformat())
    )
    assert "(none recorded)" in text
    assert "(nothing recorded)" in text


def test_suspend_preserves_attempt_across_cycles(tmp_path):
    """Re-suspending the same session must not reset its idempotency counter."""
    cascade = _cascade(tmp_path)
    stamp = (utcnow() + timedelta(minutes=2)).isoformat()
    cascade._suspend({"progress_note": "a", "resume_at": stamp})
    cascade.store.claim("s1")
    cascade._suspend({"progress_note": "b", "resume_at": stamp})

    assert cascade.store.get("s1").attempt == 1
