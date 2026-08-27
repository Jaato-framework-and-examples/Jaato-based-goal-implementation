"""The cascade driver: spawn a goal actor, then resume it until the goal is met.

The driver owns three things the agent cannot own for itself:

* **the clock** — the agent ends its turn when it needs to wait; something
  outside the session has to bring it back;
* **the state that must survive** — it re-injects ``progress_note`` and
  ``watch_handle`` verbatim on every resume, so correctness never depends on
  conversation history surviving garbage collection; and
* **the ceiling** — a cascade budget bounds the whole goal, so a goal that
  never converges still terminates.

It owns nothing else. It does not decide *when* to resume (the agent does, via
``resume_at``), nor what "done" means (the completion schema does).

Flow::

    connect → declare cascade budget → spawn goal actor → send the goal
      ├─ AgentCompletedEvent(outcome="finished")  → report, exit
      └─ AgentCompletedEvent(outcome="suspended") → persist due row
                                                    sleep until due
                                                    session.wake with the
                                                      replayed state
                                                    (repeat)

Resume uses the daemon's ``session.wake`` command rather than attach+send: it
cold-revives the session from disk if the runner was released (which
``signal_completion`` does on every suspend), and it dedups on ``event_id``, so
a driver that dies between waking and recording cannot double-drive a turn.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from jaato_sdk import ClientType, EventType, IPCRecoveryClient

# The two ways a spawn can fail without returning, kept greppable so the
# assumption is visible if the SDK's hierarchy moves:
#
#   * SessionCreateFailed — the daemon was reached and the spawn still produced
#     no session. `create_session` no longer returns None for this; its
#     contract is now "the new session ID, never None — a failure raises".
#   * ReconnectingError / ConnectionClosedError / ConnectionError — the recovery
#     client's pre-send state gate rejected the call before it left the process.
#
# They share no base but Exception, so neither implies the other. Note also that
# SessionCreateFailed subclasses RuntimeError, as does BudgetExhausted below —
# SIBLINGS, so `except BudgetExhausted` does not catch it and the ceiling still
# needs the explicit check further down.
from jaato_sdk import (
    ConnectionClosedError,
    ReconnectingError,
    SessionCreateFailed,
)

# Not re-exported from the package root, unlike the two above. Note that it
# SHADOWS the builtin and subclasses Exception, not OSError: an unaliased
# `except ConnectionError` here would bind the builtin and catch nothing the
# SDK actually raises. The alias keeps that trap visible at the catch site.
from jaato_sdk.client.recovery import ConnectionError as SdkConnectionError

from .store import DueRow, GoalStore

# A resume that the agent scheduled unreasonably far out still has to be bounded
# by something the operator can see; the budget is the real ceiling, this only
# stops one absurd timestamp from parking the driver for a week.
MAX_SLEEP_SECONDS = 3600.0

# How long a single driven turn may run before the driver stops waiting on it.
# Not a budget — the cascade budget is the real ceiling. This exists only so a
# turn that emits no terminal event at all fails visibly instead of parking the
# driver forever.
#
# This is a DEFAULT, not a limit: `turn_timeout` overrides it. The value that
# suits a fixture whose turns take seconds will cut off an agent doing real
# work — a single turn spent reading an unfamiliar codebase, editing it and
# running its tests can easily outlast five minutes, and killing that is
# discarding the work, not catching a hang.
TURN_TIMEOUT_SECONDS = 300.0

# ``SessionTerminatedEvent.reason`` when a degrade rung's terminal action stops
# the session at its ceiling. A typed value, not a log string: distinguishing
# "stopped at the ceiling" from "broke" is what exit code 2 is for, and doing it
# by matching the daemon's prose would be a guess dressed as a check.
CEILING_REASON = "budget_exhausted"

# How long to wait before waking an agent whose finish was refused. Short: it
# is not waiting on the world, only on being told what was wrong.
RETRY_AFTER_SECONDS = 5


class GoalCascade:
    """Drives one goal to completion across an arbitrary number of suspends.

    A single instance corresponds to one cascade identity (``cascade_driver_id``)
    and one goal-actor session. Resumes continue that same session, so the agent
    keeps its reasoning context; the replayed payload fields keep it correct even
    when that context has been compacted away.
    """

    def __init__(
        self,
        goal: str,
        *,
        workspace: str,
        env_file: str,
        socket: str = "/tmp/jaato.sock",
        profile: str = "goal-actor",
        agent: str = "goal-actor",
        state_path: str = ".goal-cascade-state/due.json",
        budget: Optional[Dict[str, float]] = None,
        degrade: Optional[list] = None,
        turn_timeout: float = TURN_TIMEOUT_SECONDS,
        verify_finished: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.goal = goal
        self.workspace = workspace
        self.env_file = env_file
        self.socket = socket
        self.profile = profile
        self.agent = agent
        self.budget = budget or {"turns": 40, "usd": 1.0, "seconds": 3600}
        self.degrade = degrade
        self.turn_timeout = turn_timeout
        self.verify_finished = verify_finished
        self.log = log
        self.store = GoalStore(Path(workspace) / state_path)
        self.cascade_id = uuid.uuid4().hex
        self.session_id: Optional[str] = None
        self.resumes = 0
        self._client: Optional[IPCRecoveryClient] = None

    # ---------------------------------------------------------------- client

    def _new_client(self) -> IPCRecoveryClient:
        """Build the recovery client with the knobs this pattern requires.

        ``ClientType.API`` is load-bearing: ``signal_completion`` is hidden from
        terminal-role clients, and it is the agent's only exit here.
        ``IPCRecoveryClient`` (not ``IPCClient``) because a goal outlives any
        single daemon lifetime — it must survive a restart mid-wait.
        """
        return IPCRecoveryClient(
            self.socket,
            client_type=ClientType.API,
            auto_start=True,
            env_file=self.env_file,
            workspace_path=self.workspace,
            on_status_change=lambda s: self.log(
                f"[connection] {getattr(s, 'state', s)}"
            ),
        )

    # ---------------------------------------------------------------- budget

    async def _budget_refusal(
        self, client: IPCRecoveryClient, *, timeout: float = 10.0
    ) -> Optional[str]:
        """Read the ceiling back; return why it is absent, or ``None`` if set.

        ``cascade_budget_set`` is fire-and-forget. A malformed budget is
        refused *asynchronously*, so the first visible symptom is an unrelated
        command failing later — which is exactly how an invalid degrade rung
        once surfaced as "spawn refused, may have no headroom".

        A ceiling that was never accepted enforces nothing, and invariant 7
        requires it to enforce. So the ceiling is a precondition to be proven
        before the goal is allowed to start, not something assumed from the
        absence of an error.

        The daemon answers ``cascade.budget.get`` with a ``SystemMessageEvent``
        carrying JSON: the declared limits, or ``{"declared": false}`` when the
        cascade id is uncapped.

        Reports only what the daemon said. A connection that cannot carry the
        question raises out of here instead, because "the daemon denies having a
        ceiling" and "the daemon could not be asked" are different facts and
        this return value has no way to hold both; the caller catches it and
        turns it into a refusal string there.
        """
        answered = asyncio.Event()
        state: Dict[str, Any] = {}

        def on_system(event) -> None:
            try:
                data = json.loads(getattr(event, "message", "") or "")
            except (json.JSONDecodeError, TypeError):
                return  # some other system message — keep listening
            if not isinstance(data, dict):
                return
            if "declared" not in data and "limits" not in data:
                return
            state.update(data)
            answered.set()

        unsubscribe = client.subscribe(EventType.SYSTEM_MESSAGE, on_system)
        try:
            await client.cascade_budget_get(self.cascade_id)
            await asyncio.wait_for(answered.wait(), timeout)
        except asyncio.TimeoutError:
            return f"daemon did not report budget state within {timeout:.0f}s"
        finally:
            unsubscribe()

        if state.get("declared") is False or not state.get("limits"):
            return f"daemon reports no ceiling for cascade {self.cascade_id}"
        return None

    def _watch_spawn_refusal(self, client: IPCRecoveryClient) -> "_SpawnRefusal":
        """Watch for the daemon's reason while a spawn is attempted.

        A refused spawn now raises ``SessionRefused``, which carries the
        daemon's own ``error_type`` — but this watcher predates that and still
        earns its place: it is what makes the ceiling legible on the paths the
        exception does not cover, and it is the source the exit-code decision
        actually reads. The one reason the driver must tell apart is an
        exhausted cascade budget: that is the operator's ceiling working, not
        the goal breaking, and invariant 7 requires the two to exit
        differently.

        The daemon refuses such a spawn rather than starting a doomed session —
        a ceiling that must spend to enforce itself is not a ceiling — and says
        so with ``ErrorEvent(error_type="CascadeExhaustedError")``. That event
        is the only evidence available, so it is captured across the call.
        """
        watcher = _SpawnRefusal()
        watcher.unsubscribe = client.subscribe(EventType.ERROR, watcher.record)
        return watcher

    # ------------------------------------------------------------ turn cycle

    def _arm_completion(
        self, client: IPCRecoveryClient
    ) -> Callable[[], Any]:
        """Subscribe for the turn's terminal event, and return a waiter.

        Call this BEFORE driving the turn — before ``send_message`` or
        ``session.wake`` — and await the returned waiter afterwards. Arming
        after the drive is a race: a turn that ends quickly can emit its event
        before the handler exists, and the driver then waits for something that
        already happened.

        A turn has three terminal outcomes, not one. ``AGENT_COMPLETED`` is the
        good one; ``AGENT_ERROR`` and ``SESSION_TERMINATED`` are the others, and
        a driver deaf to those hangs on every real failure — an expired API key
        ends the session without ever producing a completion.

        The waiter returns the validated payload. On failure it returns a dict
        carrying ``_failure``, which the caller reports rather than mistaking
        for a completion.
        """
        done = asyncio.Event()
        captured: Dict[str, Any] = {}
        unsubscribes = []

        def on_completed(event) -> None:
            captured.update(getattr(event, "payload", None) or {})
            captured.setdefault("_success", getattr(event, "success", True))
            done.set()

        def on_failed(event) -> None:
            if done.is_set():
                return
            # A session ending with reason "natural" is the expected unload
            # BETWEEN turns, not a failed turn. `signal_completion` releases
            # the runner slot, so the previous turn's termination routinely
            # lands after this turn has already been armed — reporting it
            # would turn every healthy suspend into an error. A real failure
            # arrives as AGENT_ERROR, or as a termination whose reason is
            # something other than "natural" (carrying `error_summary`).
            reason = getattr(event, "reason", None)
            if reason == "natural":
                return

            if reason == CEILING_REASON:
                # The ceiling stopped the goal. That is the operator's limit
                # working, not the goal breaking, and the two must not exit
                # alike. `reason` is a typed field — branching on it is what
                # makes this expressible at all; before the daemon emitted
                # this event the refusal arrived as prose, and the driver
                # simply waited out its timeout knowing nothing.
                details = getattr(event, "details", None) or {}
                captured["_ceiling"] = (
                    details.get("reason") or "cascade budget exhausted"
                )
                captured["_usage"] = details.get("usage") or {}
            captured["_failure"] = (
                getattr(event, "error", None)
                or getattr(event, "error_summary", None)
                or getattr(event, "reason", None)
                or type(event).__name__
            )
            done.set()

        unsubscribes.append(
            client.subscribe_once(EventType.AGENT_COMPLETED, on_completed))
        for failure_event in (EventType.AGENT_ERROR, EventType.SESSION_TERMINATED):
            unsubscribes.append(client.subscribe(failure_event, on_failed))

        async def wait(timeout: Optional[float] = None) -> Dict[str, Any]:
            timeout = self.turn_timeout if timeout is None else timeout
            try:
                await asyncio.wait_for(done.wait(), timeout)
            except asyncio.TimeoutError:
                captured["_failure"] = (
                    f"no terminal event within {timeout:.0f}s — the turn "
                    "neither completed nor reported an error"
                )
            finally:
                for unsubscribe in unsubscribes:
                    unsubscribe()
            return captured

        return wait

    def _suspend(self, payload: Dict[str, Any]) -> DueRow:
        """Persist the agent's requested resume as a due row.

        The row captures ``progress_note`` and ``watch_handle`` because those
        are replayed verbatim into the resumed turn — the store is the only
        thing standing between a compacted history and a lost goal.

        ``resume_at`` is read directly, with no default. A ``suspended``
        payload that omits it cannot reach here: the completion processor in
        ``.jaato/scripts/processors/goal_contract.py`` blocks that completion
        server-side and tells the agent to supply one. So a ``KeyError`` here
        means the contract itself was broken — worth failing on, and much
        better than the default this replaced, which quietly invented a
        one-minute wait the agent never asked for and could not see.
        """
        assert self.session_id is not None
        row = DueRow(
            session_id=self.session_id,
            resume_at=payload["resume_at"],
            resume_reason=payload.get("resume_reason", ""),
            watch_handle=payload.get("watch_handle") or {},
            progress_note=payload.get("progress_note", ""),
            attempt=self.store.get(self.session_id).attempt
            if self.store.get(self.session_id)
            else 0,
        )
        self.store.put(row)
        return row

    @staticmethod
    def _continuation_text(row: DueRow) -> str:
        """Compose the resume prompt, replaying the state the agent must keep.

        This is the guarantee that makes same-session resume safe: whatever
        garbage collection did to the transcript, these two fields arrive intact
        in the woken turn.

        Carries STATE, not orders. ``session.wake`` wraps this text as untrusted
        external content — deliberately, so a wake cannot inject instructions —
        which means the agent reads it as data. The closing line below is a
        reminder, not a control: what actually obliges the agent to exit through
        ``signal_completion`` is its persona, a trusted instruction source.
        Putting that requirement only here produced a resumed turn that checked
        the job, answered in prose, and stranded the goal.
        """
        handle = row.watch_handle or {}
        lines = [
            "Resuming your goal — you suspended yourself and asked to be woken now.",
            "",
            f"You were waiting for: {row.resume_reason or 'unspecified'}",
            f"Your note to yourself: {row.progress_note or '(none recorded)'}",
            f"What to re-inspect: {handle or '(nothing recorded)'}",
            "",
            "Re-inspect it, continue the goal, and finish this turn with"
            " signal_completion — `finished` if the goal is met, otherwise"
            " `suspended` with a fresh resume_at.",
        ]
        return "\n".join(lines)

    async def _sleep_until_due(self) -> None:
        """Sleep until the earliest pending row is due (bounded)."""
        wait = self.store.seconds_until_next()
        if wait is None or wait <= 0:
            return
        wait = min(wait, MAX_SLEEP_SECONDS)
        self.log(f"[suspend] sleeping {wait:.0f}s until resume is due")
        await asyncio.sleep(wait)

    async def _resume(self, client: IPCRecoveryClient) -> bool:
        """Wake the suspended session with its replayed state.

        Returns False when there is nothing to resume. Claiming before waking
        (rather than after) is what makes a crash here safe: the persisted
        attempt advances first, and the daemon dedups the old resume key.
        """
        assert self.session_id is not None
        row = self.store.claim(self.session_id)
        if row is None:
            return False
        self.resumes += 1
        self.log(f"[resume #{self.resumes}] {row.resume_reason or 'continuing'}")

        # Re-attach BEFORE waking. A suspend releases the runner slot, and a
        # cascade session that has gone quiet is unloaded — which also drops
        # this client's attachment. `session.wake` revives the session and
        # drives the turn, but it does not restore anyone's event stream, so a
        # driver that only wakes gets a turn it cannot observe: the agent
        # completes normally and the driver waits out its timeout having heard
        # nothing at all.
        #
        # This is NOT the attach-then-send that invariant 2 rules out. Nothing
        # is sent here; the wake below is still what drives the turn. Attaching
        # only restores delivery, and it must happen before the wake, or the
        # completion can land before anyone is listening.
        await client.attach_session(row.session_id)

        await client.execute_command(
            "session.wake",
            payload={
                "session_id": row.session_id,
                "text": self._continuation_text(row),
                "source": "goal-cascade",
                "event_id": row.resume_key(),
            },
        )
        return True

    def _reject_unproven_finish(self, payload: Dict[str, Any]) -> Optional[str]:
        """Ask the operator's verifier whether the finish is believable.

        Returns a reason to refuse, or ``None`` to accept. With no verifier
        configured the agent's word stands, which is the example's default.

        **Why this lives in the driver and not in a completion processor.** A
        completion processor runs on the agent's turn-exit path, inside the
        agent's session, against the workspace the agent edits. Any one of
        those disqualifies it as a trust boundary: a profile with
        ``defaultPolicy: allow`` and ``cli`` gives the agent arbitrary code
        execution as that uid, so no in-workspace artifact — however signed —
        is evidence about the world. Demonstrated, not assumed: writing a
        two-line ``passed`` status file by hand was enough to make this
        example's own processor accept a ``finished`` claim over a failing
        suite.

        The driver is the one party already outside that blast radius. It owns
        the clock and the ceiling; owning final acceptance is the same kind of
        thing, and it is a relocation rather than a new mechanism.

        Deliberately generic: the driver still does not know what "done" means
        for any particular goal. It knows only that the operator may hold
        evidence it should consult before believing one.

        The payload is passed through because a completion often NAMES its own
        evidence — a patch to re-apply, an artifact to re-check. The agent says
        where to look; the driver decides what it finds there.
        """
        if self.verify_finished is None:
            return None
        return self.verify_finished(payload)

    # ----------------------------------------------------------------- entry

    async def run(self) -> int:
        """Drive the goal to a terminal outcome. Returns a process exit code.

        Exit codes are distinct on purpose — a run stopped by its budget is a
        different result from a goal achieved, and must never look like success.
        A daemon that is down or restarting when the driver spawns its session
        is reported as a refused spawn, not raised: the caller gets an exit code
        on every path out of here.
        """
        client = self._client = self._new_client()
        if not await client.connect(timeout=120.0):
            self.log("could not connect or autostart the daemon — run jaato-doctor")
            return 1
        try:
            # Both budget calls go through the same pre-send state gate as the
            # spawn below, so a daemon that is down or restarting fails them by
            # raising rather than returning. That is not a distinct outcome from
            # a refused ceiling: either way the ceiling is unproven, and
            # invariant 7 forbids starting a goal whose ceiling is unproven. So
            # it funnels into the refusal string the check below already reads,
            # rather than becoming a second way to leave this method.
            try:
                await client.cascade_budget_set(
                    self.cascade_id, limits=self.budget, degrade=self.degrade
                )
                refusal = await self._budget_refusal(client)
            except (
                ReconnectingError,
                ConnectionClosedError,
                SdkConnectionError,
            ) as exc:
                refusal = f"{type(exc).__name__}: {exc}"
            if refusal is not None:
                self.log(f"[error] cascade budget was not accepted: {refusal}")
                self.log("[error] refusing to start — a goal with no ceiling"
                         " has nothing to stop it (see invariant 7)")
                return 1
            self.log(f"[budget] {self.budget}")

            refusal = self._watch_spawn_refusal(client)
            # A spawn that produces no session reports it by raising, not by
            # returning None: the daemon can refuse (SessionCreateFailed), or
            # the recovery client can reject the send before it leaves the
            # process because it is not CONNECTED — a daemon restart mid-run is
            # enough for the latter. Neither is a different OUTCOME from the
            # other, or from a None return: either way there is no session.
            # Carry the cause down to the falsy check below, which is the single
            # place this method decides an exit code, rather than deciding here.
            spawn_error: Optional[Exception] = None
            try:
                self.session_id = await client.create_session(
                    profile=self.profile,
                    agent=self.agent,
                    cascade_driver_id=self.cascade_id,
                    timeout=60.0,
                )
            except (
                SessionCreateFailed,
                ReconnectingError,
                ConnectionClosedError,
                SdkConnectionError,
            ) as exc:
                # No reset needed: session_id is None from __init__ and is
                # assigned only by the statement that just raised.
                spawn_error = exc
            finally:
                refusal.stop()

            if not self.session_id:
                # A ceiling that stopped the goal is not a failure — the work
                # was valid, the operator's limit simply ran out — so it must
                # not exit like one. That verdict does not depend on HOW the
                # spawn ended: if the daemon named the budget, the budget is the
                # answer whether that arrived as a None return or a refusal
                # raised. The ErrorEvent that sets `exhausted` is dispatched to
                # handlers before the SDK turns it into SessionRefused, so it
                # still reaches the watcher on the raising path.
                #
                # Everything else exits 1, naming the cause when the failure
                # carried one and deferring to the daemon's own log when it did
                # not — a spawn the daemon merely declined explains itself
                # there, but one the client never sent leaves nothing to read.
                if refusal.exhausted:
                    raise BudgetExhausted(
                        refusal.message or "cascade budget has no headroom left"
                    )
                if spawn_error is not None:
                    self.log(
                        f"spawn refused — {type(spawn_error).__name__}:"
                        f" {spawn_error}"
                    )
                else:
                    self.log(
                        "spawn refused — see the daemon error above for the cause"
                    )
                return 1

            # Armed before the goal is sent, not after — see _arm_completion.
            wait_for_turn = self._arm_completion(client)
            await client.send_message(self.goal)

            while True:
                payload = await wait_for_turn()

                ceiling = payload.get("_ceiling")
                if ceiling:
                    raise BudgetExhausted(
                        f"{ceiling} — usage {payload.get('_usage') or {}}"
                    )

                failure = payload.get("_failure")
                if failure:
                    self.log(f"[error] turn ended without completing: {failure}")
                    return 1

                outcome = payload.get("outcome")

                if outcome == "finished":
                    refusal = self._reject_unproven_finish(payload)
                    if refusal is not None:
                        # Refusing is not failing. The agent gets told why and
                        # woken to try again — a rejected finish is just
                        # another resume, carried on the same replay channel
                        # as any other state.
                        self.log(f"[rejected] {refusal}")
                        row = self._suspend({
                            "resume_at": (
                                datetime.now(timezone.utc)
                                + timedelta(seconds=RETRY_AFTER_SECONDS)
                            ).isoformat(),
                            "resume_reason": "the finish was refused",
                            "progress_note": (
                                f"My `finished` claim was REJECTED by the "
                                f"driver's verification: {refusal}. The tests "
                                "that count are re-run from a clean checkout "
                                "with the project's own test files restored, "
                                "so weakening or editing tests locally changes "
                                "nothing. Fix the underlying problem and "
                                "produce a corrected patch."
                            ),
                            "watch_handle": payload.get("watch_handle") or {},
                        })
                        await self._sleep_until_due()
                        wait_for_turn = self._arm_completion(client)
                        if not await self._resume(client):
                            self.log("[error] nothing to resume — state lost?")
                            return 1
                        continue
                    self.store.drop(self.session_id)
                    self.log(f"[finished] {payload.get('progress_note', '')}")
                    self.log(f"[result] {payload.get('result')}")
                    self.log(f"[stats] resumes={self.resumes}")
                    return 0

                if outcome != "suspended":
                    # No schema branch matched: either the profile declared no
                    # completion schema, or the agent errored out.
                    self.log(f"[error] unusable completion payload: {payload}")
                    return 1

                row = self._suspend(payload)
                self.log(f"[suspended] until {row.resume_at} — {row.resume_reason}")
                await self._sleep_until_due()
                # Arm before waking, for the same reason as the initial send:
                # the resumed turn must not be able to finish before anyone is
                # listening for it.
                wait_for_turn = self._arm_completion(client)
                if not await self._resume(client):
                    self.log("[error] nothing to resume — state lost?")
                    return 1
        except BudgetExhausted as exc:
            self.log(f"[stopped] {exc}")
            return 2
        finally:
            await client.disconnect()


class _SpawnRefusal:
    """Captures the daemon's stated reason for refusing a spawn.

    Records only the FIRST error seen. A refused spawn can be followed by
    unrelated noise as the session tears down, and the first reason is the one
    that explains the refusal.

    Attributes:
        exhausted: True when the daemon named the cascade budget — the signal
            that separates "the ceiling stopped this" from "this broke".
        message: The daemon's error text, for the operator.
    """

    #: The daemon's ``error_type`` when a spawn is refused for lack of headroom.
    EXHAUSTED_ERROR_TYPE = "CascadeExhaustedError"

    def __init__(self) -> None:
        self.exhausted = False
        self.message: Optional[str] = None
        self.unsubscribe: Optional[Callable[[], None]] = None
        self._seen = False

    def record(self, event) -> None:
        """Handler for ``EventType.ERROR`` while a spawn is in flight."""
        if self._seen:
            return
        self._seen = True
        self.message = getattr(event, "error", None)
        self.exhausted = (
            getattr(event, "error_type", None) == self.EXHAUSTED_ERROR_TYPE
        )

    def stop(self) -> None:
        """Unsubscribe; safe to call more than once."""
        if self.unsubscribe is not None:
            self.unsubscribe()
            self.unsubscribe = None


class BudgetExhausted(RuntimeError):
    """Raised when the cascade ceiling stops the goal before it completed.

    Distinct from a failure: the work was valid, the operator's ceiling simply
    ran out. Surfaced with exit code 2 so automation can tell the difference.
    """
