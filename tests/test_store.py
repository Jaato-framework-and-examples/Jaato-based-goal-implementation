"""Tests for the durable due-row store.

These pin the properties that make a goal survive its driver: restart recovery,
atomic persistence, and idempotent claiming.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from goal_cascade.store import DueRow, GoalStore, utcnow


def _row(session="s1", *, minutes=0, attempt=0) -> DueRow:
    return DueRow(
        session_id=session,
        resume_at=(utcnow() + timedelta(minutes=minutes)).isoformat(),
        resume_reason="waiting for the job",
        watch_handle={"status_file": "fixtures/.job-status.json"},
        progress_note="started run #1",
        attempt=attempt,
    )


def test_row_survives_a_new_store_instance(tmp_path):
    """A restarted driver must find the pending resume it left behind."""
    path = tmp_path / "due.json"
    GoalStore(path).put(_row(minutes=5))

    reloaded = GoalStore(path)
    row = reloaded.get("s1")
    assert row is not None
    assert row.progress_note == "started run #1"
    assert row.watch_handle["status_file"] == "fixtures/.job-status.json"


def test_row_due_while_the_driver_was_gone_is_returned(tmp_path):
    """A resume that came due during downtime fires on the next start."""
    path = tmp_path / "due.json"
    GoalStore(path).put(_row(minutes=-5))

    assert [r.session_id for r in GoalStore(path).due()] == ["s1"]


def test_future_row_is_not_due(tmp_path):
    store = GoalStore(tmp_path / "due.json")
    store.put(_row(minutes=10))
    assert store.due() == []
    assert 0 < store.seconds_until_next() <= 600


def test_claim_advances_attempt_and_changes_the_resume_key(tmp_path):
    """Each genuine resume gets a distinct idempotency key."""
    store = GoalStore(tmp_path / "due.json")
    store.put(_row())

    first = store.claim("s1").resume_key()
    second = store.claim("s1").resume_key()

    assert first != second
    assert first.endswith(":1") and second.endswith(":2")


def test_claim_is_persisted_before_the_wake(tmp_path):
    """A crash after claiming must not replay the same resume key.

    The attempt counter is written to disk by claim() itself, so a fresh store
    computes the *next* key rather than repeating the in-flight one.
    """
    path = tmp_path / "due.json"
    store = GoalStore(path)
    store.put(_row())
    claimed = store.claim("s1").resume_key()

    assert GoalStore(path).get("s1").attempt == 1
    assert GoalStore(path).claim("s1").resume_key() != claimed


def test_drop_removes_the_row(tmp_path):
    path = tmp_path / "due.json"
    store = GoalStore(path)
    store.put(_row())
    store.drop("s1")

    assert GoalStore(path).get("s1") is None
    assert GoalStore(path).seconds_until_next() is None


def test_corrupt_state_file_starts_empty_rather_than_raising(tmp_path):
    """Losing the schedule is recoverable; refusing to start is not."""
    path = tmp_path / "due.json"
    path.write_text("{not json", encoding="utf-8")
    assert GoalStore(path).all() == []


@pytest.mark.parametrize("stamp", ["2020-01-01T00:00:00Z", "2020-01-01T00:00:00"])
def test_timestamps_with_and_without_zone_are_accepted(tmp_path, stamp):
    """Models emit `Z` more often than `+00:00`; neither should fail a goal."""
    store = GoalStore(tmp_path / "due.json")
    store.put(DueRow(session_id="s1", resume_at=stamp))
    assert store.due()


def test_unparseable_timestamp_is_treated_as_due(tmp_path):
    """A malformed resume_at should surface as a turn, never as a lost goal."""
    store = GoalStore(tmp_path / "due.json")
    store.put(DueRow(session_id="s1", resume_at="soon-ish"))
    assert [r.session_id for r in store.due()] == ["s1"]
