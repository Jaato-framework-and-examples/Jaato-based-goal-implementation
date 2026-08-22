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

    # ------------------------------------------------------------ turn cycle

    async def _await_completion(self, client: IPCRecoveryClient) -> Dict[str, Any]:
        """Wait for the agent's next ``signal_completion`` and return its payload.

        Subscribes before the turn is driven so a fast completion cannot land
        before the handler is armed. Returns the validated payload dict; an
        agent whose profile declared no schema would yield ``{}``, which the
        caller treats as a protocol error rather than a completion.
        """
        done = asyncio.Event()
        captured: Dict[str, Any] = {}

        def on_completed(event) -> None:
            captured.update(getattr(event, "payload", None) or {})
            captured.setdefault("_success", getattr(event, "success", True))
            done.set()

        client.subscribe_once(EventType.AGENT_COMPLETED, on_completed)
        await done.wait()
        return captured

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
            self.log(f"[budget] {self.budget}")

            self.session_id = await client.create_session(
                profile=self.profile,
                agent=self.agent,
                cascade_driver_id=self.cascade_id,
                timeout=60.0,
            )
            if not self.session_id:
                self.log("spawn refused — the cascade budget may have no headroom")
                return 1

            await client.send_message(self.goal)

            while True:
                payload = await self._await_completion(client)
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
