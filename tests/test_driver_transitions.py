"""Tests for the driver's decision logic, with the daemon faked out.

These cover the branch handling and the continuation contract — the parts that
decide whether a goal advances — without needing a running daemon or a model.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from jaato_sdk import (
    ConnectionClosedError,
    ReconnectingError,
    SessionNotConfirmed,
    SessionRefused,
)

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
    """A client that refuses the spawn, in either of the two shapes it can.

    `error_type` mirrors the daemon's shape: `create_session` returns None on
    refusal and the reason arrives separately as an ErrorEvent.

    `raises` mirrors the SDK's shape: the recovery client checks its connection
    state before sending and raises instead of returning, so a daemon that is
    down or restarting fails the spawn without the daemon ever hearing of it.
    Both can happen on one call — the daemon can name the budget and then go
    away — so the event still fires first when both are set.
    """

    def __init__(
        self,
        error_type=None,
        raises=None,
        budget_set_raises=None,
        budget_get_raises=None,
    ) -> None:
        self._error_type = error_type
        self._raises = raises
        self._budget_set_raises = budget_set_raises
        self._budget_get_raises = budget_get_raises
        self._handlers = []
        self.disconnected = False

    def subscribe(self, _event_type, handler):
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    async def connect(self, timeout: float = 0.0) -> bool:
        return True

    async def cascade_budget_set(self, *_a, **_kw) -> None:
        if self._budget_set_raises is not None:
            raise self._budget_set_raises
        return None

    async def cascade_budget_get(self, *_a, **_kw) -> None:
        if self._budget_get_raises is not None:
            raise self._budget_get_raises
        return None

    async def create_session(self, **_kw):
        if self._error_type is not None:
            for handler in list(self._handlers):
                handler(_FakeErrorEvent(self._error_type, "no headroom left"))
        if self._raises is not None:
            raise self._raises
        return None

    async def disconnect(self) -> None:
        self.disconnected = True


def _run_refused_spawn(tmp_path, error_type=None, raises=None, lines=None) -> int:
    """Drive run() to the spawn refusal and return its exit code.

    `lines`, when given, collects what the driver logged on the way out.
    """
    cascade = _cascade(tmp_path)
    cascade.session_id = None
    if lines is not None:
        cascade.log = lines.append
    client = _FakeClient(error_type, raises)
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


def test_spawn_transport_failure_exits_instead_of_crashing(tmp_path):
    """A daemon restarting mid-spawn must not take the driver down with it.

    `create_session` guards on connection state and RAISES rather than
    returning None when the client is not CONNECTED. Those types subclass
    Exception directly, so `except BudgetExhausted` (a RuntimeError) does not
    catch them and nothing between here and sys.exit does either — the process
    died with a traceback instead of reporting an exit code.
    """
    assert _run_refused_spawn(tmp_path, raises=ReconnectingError()) == 1


def test_transport_failure_names_its_cause(tmp_path):
    """The generic message is the fallback for having no cause, not a default.

    "see the daemon error above" is a lie when the daemon never heard of the
    spawn: there is nothing above to see. The exception is the only account of
    what happened, so it has to reach the log.
    """
    lines = []
    _run_refused_spawn(
        tmp_path, raises=ReconnectingError("Client is reconnecting"), lines=lines
    )
    refusals = [line for line in lines if "spawn refused" in line]
    assert refusals == ["spawn refused — ReconnectingError: Client is reconnecting"]


def test_exhausted_budget_outranks_a_transport_failure(tmp_path):
    """The ceiling still decides the exit code when the connection also drops.

    The daemon can name the budget and be gone before the call returns. Exit 2
    is decided by what the daemon SAID, not by how the call ended — routing the
    exception around this check would turn a correct budget stop (a valid
    outcome, invariant 7) into a generic failure. This is the case most likely
    to regress, because the exception path is the one a fix tends to add last.
    """
    assert _run_refused_spawn(
        tmp_path,
        error_type="CascadeExhaustedError",
        raises=ReconnectingError(),
    ) == 2


def test_exhausted_budget_survives_the_daemon_raising_its_refusal(tmp_path):
    """The ceiling verdict must not depend on HOW the daemon says no.

    `create_session` no longer returns None when the daemon refuses — it raises
    `SessionRefused`, which subclasses RuntimeError. So does BudgetExhausted,
    but they are SIBLINGS, so `except BudgetExhausted` does not catch it: an
    exhausted budget went from exit 2 to an unhandled traceback. This mirrors
    the real client, which dispatches the ErrorEvent to handlers before turning
    it into the exception, so both signals are present on one call.
    """
    assert _run_refused_spawn(
        tmp_path,
        error_type="CascadeExhaustedError",
        raises=SessionRefused(
            "the daemon refused session.new: no headroom left",
            error_type="CascadeExhaustedError",
        ),
    ) == 2


def test_non_budget_daemon_refusal_exits_one_with_its_cause(tmp_path):
    """A refusal the daemon explains is still exit 1, and still says why."""
    lines = []
    code = _run_refused_spawn(
        tmp_path,
        error_type="ConfigurationError",
        raises=SessionRefused(
            "the daemon refused session.new: no such profile",
            error_type="ConfigurationError",
        ),
        lines=lines,
    )
    assert code == 1
    assert [line for line in lines if "spawn refused" in line] == [
        "spawn refused — SessionRefused: the daemon refused session.new: "
        "no such profile"
    ]


def test_unconfirmed_spawn_exits_rather_than_crashing(tmp_path):
    """The third #635 shape: sent, unanswered — a session MAY be running.

    The driver has no retry, so `may_exist` changes nothing it does today; what
    matters here is that this exits instead of raising. If retry is ever added,
    branch on `may_exist` — session.new has no idempotency key, so retrying an
    unconfirmed create spawns a SECOND session with its own runner and pool
    slot rather than resuming the first.
    """
    assert _run_refused_spawn(
        tmp_path,
        raises=SessionNotConfirmed(
            "the connection dropped before session.new was answered",
            cause="disconnect",
        ),
    ) == 1


def _run_budget_failure(tmp_path, *, set_raises=None, get_raises=None) -> tuple:
    """Drive run() to a budget failure. Returns (exit_code, logged_lines).

    Unlike `_run_refused_spawn` this leaves `_budget_refusal` in place — the
    point is to exercise the real ceiling-verification path, including the
    `cascade_budget_get` inside it.
    """
    lines = []
    cascade = _cascade(tmp_path)
    cascade.session_id = None
    cascade.log = lines.append
    client = _FakeClient(budget_set_raises=set_raises, budget_get_raises=get_raises)
    cascade._new_client = lambda: client
    return asyncio.run(cascade.run()), lines


def test_budget_set_transport_failure_refuses_to_start(tmp_path):
    """A ceiling that could not be declared has not been declared.

    `cascade_budget_set` sits behind the same pre-send state gate as the spawn,
    so a daemon restart makes it raise. Starting anyway would run a goal with
    nothing to stop it, which invariant 7 forbids — and crashing here would
    look identical to the daemon refusing the budget while being a different
    fact entirely.
    """
    code, lines = _run_budget_failure(
        tmp_path, set_raises=ConnectionClosedError()
    )
    assert code == 1
    assert any(
        "cascade budget was not accepted: ConnectionClosedError:" in line
        for line in lines
    )


def test_budget_readback_transport_failure_refuses_to_start(tmp_path):
    """The read-back is the proof, and an unanswerable question proves nothing.

    `_budget_refusal` reports what the daemon SAID. When the connection cannot
    carry the question there is nothing said, so it raises rather than
    returning a reason it does not have — "no ceiling" and "could not ask" must
    not collapse into one value. The caller turns it into a refusal here.
    """
    code, lines = _run_budget_failure(tmp_path, get_raises=ReconnectingError())
    assert code == 1
    assert any(
        "cascade budget was not accepted: ReconnectingError:" in line
        for line in lines
    )


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


def _run_finish(tmp_path, verifier):
    """Drive run() through `finished` completions and return the exit code.

    The fake client emits a completion for the initial send AND for each
    session.wake, so a refused finish can be observed looping rather than
    terminating — which is the behaviour that matters: rejection is feedback,
    not failure.
    """
    from jaato_sdk import EventType

    cascade = _cascade(tmp_path)
    cascade.verify_finished = verifier
    handlers = {}

    class _Done:
        payload = {"outcome": "finished", "progress_note": "n",
                   "result": {"ok": 1}, "patch_path": "fix.diff"}
        success = True

    class _C:
        def subscribe(self, event_type, h):
            handlers.setdefault(event_type, []).append(h)
            return lambda: None

        subscribe_once = subscribe

        def _fire(self):
            for h in handlers.get(EventType.AGENT_COMPLETED, []):
                h(_Done())

        async def connect(self, timeout: float = 0.0):
            return True

        async def cascade_budget_set(self, *a, **kw):
            return None

        async def create_session(self, **kw):
            return "s1"

        async def send_message(self, *a, **kw):
            self._fire()

        async def attach_session(self, *a, **kw):
            return True

        async def execute_command(self, *a, **kw):
            self._fire()          # session.wake drives the next turn

        async def disconnect(self):
            return None

    client = _C()
    cascade._new_client = lambda: client

    async def _no_refusal(_c, **_kw):
        return None

    cascade._budget_refusal = _no_refusal

    async def _no_sleep():
        return None

    cascade._sleep_until_due = _no_sleep
    return asyncio.run(cascade.run())


def test_a_refused_finish_resumes_instead_of_failing(tmp_path):
    """Rejection is feedback. The agent is told why and woken to try again.

    Exiting here would throw away a recoverable turn — the agent's claim was
    wrong, not its session. The reason rides the same replay channel as any
    other state, so it survives the eviction that happens in between.
    """
    seen = []

    def verifier(payload):
        seen.append(payload.get("patch_path"))
        return "the suite failed on a clean checkout" if len(seen) == 1 else None

    assert _run_finish(tmp_path, verifier) == 0
    assert len(seen) == 2, "the agent should have been resumed and re-checked"


def test_the_verifier_sees_the_payload(tmp_path):
    """A completion names its own evidence; the driver decides what it finds."""
    got = {}

    def verifier(payload):
        got.update(payload)
        return None

    assert _run_finish(tmp_path, verifier) == 0
    assert got["patch_path"] == "fix.diff"


def test_no_verifier_means_the_agents_word_stands(tmp_path):
    """The default is unchanged — verification is opt-in for the operator."""
    assert _run_finish(tmp_path, None) == 0
