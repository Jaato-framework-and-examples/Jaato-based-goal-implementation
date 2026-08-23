"""Tests for the driver's decision logic, with the daemon faked out.

These cover the branch handling and the continuation contract — the parts that
decide whether a goal advances — without needing a running daemon or a model.
"""

from __future__ import annotations

import asyncio
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


class _FakeErrorEvent:
    """Minimal stand-in for the daemon's ErrorEvent."""

    def __init__(self, error_type: str, error: str) -> None:
        self.error_type = error_type
        self.error = error


class _FakeClient:
    """A client that refuses the spawn, reporting `error_type` first.

    Mirrors the daemon's shape: `create_session` returns None on refusal and
    the reason arrives separately as an ErrorEvent.
    """

    def __init__(self, error_type: str) -> None:
        self._error_type = error_type
        self._handlers = []
        self.disconnected = False

    def subscribe(self, _event_type, handler):
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    async def connect(self, timeout: float = 0.0) -> bool:
        return True

    async def cascade_budget_set(self, *_a, **_kw) -> None:
        return None

    async def cascade_budget_get(self, *_a, **_kw) -> None:
        return None

    async def create_session(self, **_kw):
        for handler in list(self._handlers):
            handler(_FakeErrorEvent(self._error_type, "no headroom left"))
        return None

    async def disconnect(self) -> None:
        self.disconnected = True


def _run_refused_spawn(tmp_path, error_type: str) -> int:
    """Drive run() to the spawn refusal and return its exit code."""
    cascade = _cascade(tmp_path)
    cascade.session_id = None
    client = _FakeClient(error_type)
    cascade._new_client = lambda: client
    # The ceiling is verified before the spawn; that path has its own tests.
    async def _no_refusal(_client, **_kw):
        return None
    cascade._budget_refusal = _no_refusal
    return asyncio.run(cascade.run())


def test_budget_exhausted_spawn_exits_two_not_one(tmp_path):
    """The ceiling stopping a goal must never look like the goal failing.

    A run stopped by its budget is a different result from a broken one, and
    automation keys on the exit code to tell them apart (invariant 7). This is
    the only path that raises BudgetExhausted, so without it both the exception
    and its handler are dead code.
    """
    assert _run_refused_spawn(tmp_path, "CascadeExhaustedError") == 2


def test_other_spawn_refusals_still_exit_one(tmp_path):
    """Exit 2 means the ceiling specifically, not any refused spawn."""
    assert _run_refused_spawn(tmp_path, "ConfigurationError") == 1


class _FakeTerminated:
    """Stand-in for SessionTerminatedEvent."""

    def __init__(self, reason: str, details=None) -> None:
        self.reason = reason
        self.details = details


def _arm_and_fire(tmp_path, event):
    """Arm the completion waiter, fire one SESSION_TERMINATED event, return payload.

    Routes by event type the way the real client does. An earlier version of
    this helper handed the event to every handler, so a termination also
    reached the AGENT_COMPLETED handler and was captured as a completion —
    the test failed for a reason that had nothing to do with the code.
    """
    from jaato_sdk import EventType

    cascade = _cascade(tmp_path)
    handlers = {}

    class _C:
        def subscribe(self, event_type, h):
            handlers.setdefault(event_type, []).append(h)
            return lambda: None

        subscribe_once = subscribe

    wait = cascade._arm_completion(_C())
    for h in handlers.get(EventType.SESSION_TERMINATED, []):
        h(event)
    return asyncio.run(wait(timeout=0.2))


def test_ceiling_stop_is_distinguished_from_a_failure(tmp_path):
    """A budget stop must not look like a broken goal (invariant 7).

    `reason` is a typed field on the terminal event, so this is a branch on a
    value rather than a match against the daemon's prose. Before the daemon
    emitted it, a mid-flight ceiling reached the driver as output text only —
    it waited out its timeout and could not say why it stopped.
    """
    payload = _arm_and_fire(tmp_path, _FakeTerminated(
        "budget_exhausted",
        {"reason": "turns 100%", "usage": {"turns": 2.0}},
    ))
    assert payload["_ceiling"] == "turns 100%"
    assert payload["_usage"] == {"turns": 2.0}


def test_a_real_failure_is_not_mistaken_for_the_ceiling(tmp_path):
    """Only the ceiling reason maps to the ceiling; everything else fails."""
    payload = _arm_and_fire(tmp_path, _FakeTerminated("error"))
    assert "_ceiling" not in payload
    assert payload["_failure"] == "error"


def test_a_natural_unload_is_neither(tmp_path):
    """The expected between-turns unload must not end the wait at all."""
    payload = _arm_and_fire(tmp_path, _FakeTerminated("natural"))
    assert "_ceiling" not in payload
    assert "_failure" in payload and "within" in payload["_failure"]
