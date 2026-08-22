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
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from jaato_sdk import ClientType, EventType, IPCRecoveryClient

from .store import DueRow, GoalStore, utcnow

# A resume that the agent scheduled unreasonably far out still has to be bounded
# by something the operator can see; the budget is the real ceiling, this only
# stops one absurd timestamp from parking the driver for a week.
MAX_SLEEP_SECONDS = 3600.0

# How long a single driven turn may run before the driver stops waiting on it.
# Not a budget — the cascade budget is the real ceiling. This exists only so a
# turn that emits no terminal event at all fails visibly instead of parking the
# driver forever.
TURN_TIMEOUT_SECONDS = 300.0


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
            captured["_failure"] = (
                getattr(event, "error", None)
                or getattr(event, "reason", None)
                or type(event).__name__
            )
            done.set()

        unsubscribes.append(
            client.subscribe_once(EventType.AGENT_COMPLETED, on_completed))
        for failure_event in (EventType.AGENT_ERROR, EventType.SESSION_TERMINATED):
            unsubscribes.append(client.subscribe(failure_event, on_failed))

        async def wait(timeout: float = TURN_TIMEOUT_SECONDS) -> Dict[str, Any]:
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
        """
        assert self.session_id is not None
        row = DueRow(
            session_id=self.session_id,
            resume_at=payload.get("resume_at")
            or (utcnow() + timedelta(minutes=1)).isoformat(),
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

    # ----------------------------------------------------------------- entry

    async def run(self) -> int:
        """Drive the goal to a terminal outcome. Returns a process exit code.

        Exit codes are distinct on purpose — a run stopped by its budget is a
        different result from a goal achieved, and must never look like success.
        """
        client = self._client = self._new_client()
        if not await client.connect(timeout=120.0):
            self.log("could not connect or autostart the daemon — run jaato-doctor")
            return 1
        try:
            await client.cascade_budget_set(
                self.cascade_id, limits=self.budget, degrade=self.degrade
            )
            refusal = await self._budget_refusal(client)
            if refusal is not None:
                self.log(f"[error] cascade budget was not accepted: {refusal}")
                self.log("[error] refusing to start — a goal with no ceiling"
                         " has nothing to stop it (see invariant 7)")
                return 1
            self.log(f"[budget] {self.budget}")

            self.session_id = await client.create_session(
                profile=self.profile,
                agent=self.agent,
                cascade_driver_id=self.cascade_id,
                timeout=60.0,
            )
            if not self.session_id:
                # The ceiling was verified above, so exhausted headroom is only
                # one candidate. Do not assert a cause the driver cannot see —
                # the daemon already logged the real one.
                self.log("spawn refused — see the daemon error above for the cause")
                return 1

            # Armed before the goal is sent, not after — see _arm_completion.
            wait_for_turn = self._arm_completion(client)
            await client.send_message(self.goal)

            while True:
                payload = await wait_for_turn()

                failure = payload.get("_failure")
                if failure:
                    self.log(f"[error] turn ended without completing: {failure}")
                    return 1

                outcome = payload.get("outcome")

                if outcome == "finished":
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


class BudgetExhausted(RuntimeError):
    """Raised when the cascade ceiling stops the goal before it completed.

    Distinct from a failure: the work was valid, the operator's ceiling simply
    ran out. Surfaced with exit code 2 so automation can tell the difference.
    """
