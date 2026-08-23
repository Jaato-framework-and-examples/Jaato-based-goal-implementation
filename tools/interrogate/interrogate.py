#!/usr/bin/env python3
"""Wake a FINISHED session and ask it to account for something, in prose.

The cascade driver resumes a session to advance a goal. This does the opposite:
it revives a session whose goal is already `finished` and asks a question about
what it did, capturing the answer as text rather than as a completion payload.

Why it is a separate harness and not a driver flag: the driver's job is to
carry a goal forward, and every line of it assumes the next turn advances that
goal. Interrogation has no goal, no due row, no ceiling to enforce — it wants
one turn and one answer. Bolting it onto the driver would put a reviewer's
question inside the machinery that decides whether work is done.

What it is for: run 37 produced a CORRECT one-line fix and a REPORT.md whose
"Root Cause" quoted code that was never in the file. The completion validator
could confirm the suite passed and the report existed; it cannot confirm the
report is true. So the question is whether the session, resumed, can account
for the discrepancy — which is also a test of whether same-session resume
preserves enough to make that possible.

The question travels through `session.wake`, so the daemon wraps it as
untrusted content: the agent reads it as data, not as an instruction from its
operator. That is the same channel the driver uses, and the same caveat applies
— what obliges the agent to answer at all is its persona, not this text.

HOW A TURN ENDS, which is the one thing to get right. The session answers under
ITS OWN completion contract — `session.wake` revives a session with the profile
it was created under, and an interrogator cannot impose a different one. So a
session whose contract demands goal artifacts (a patch path, a report) will
answer your question and then fight its own processor trying to finish.

Two ways round it, and both are legitimate:

* Ask it to end the turn `suspended` rather than `finished`. It is not
  completing a goal, only pausing again, so this is also the honest outcome.
  Works with any two-branch profile and needs no new configuration. The
  question template in this directory does exactly that.
* Create the session under `profiles/interrogator.yaml`, whose contract asks
  for nothing but an answer. Only possible when you decide before the session
  exists that you will want to question it.

Usage:
    interrogate.py <session_id> <workspace> <env_file> <question-file> [config_root]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from jaato_sdk import ClientType, EventType, IPCRecoveryClient

TERMINAL_TIMEOUT = 300.0


async def interrogate(session_id: str, workspace: str, env_file: str,
                      question: str, config_root: str | None = None) -> int:
    client = IPCRecoveryClient(
        "/tmp/jaato.sock",
        client_type=ClientType.API,          # signal_completion stays available
        auto_start=True,
        env_file=env_file,
        workspace_path=workspace,
        config_root=config_root,
        apparmor=True,     # ask the question INSIDE the confinement it ran under
        on_status_change=lambda s: print(f"[connection] {getattr(s,'state',s)}"),
    )
    if not await client.connect(timeout=120.0):
        print("could not connect to the daemon")
        return 1

    said: list[str] = []
    done = asyncio.Event()

    def on_output(event) -> None:
        text = getattr(event, "text", "") or ""
        if text:
            said.append(text)

    def on_terminal(event) -> None:
        # A completion ends the answer. So does a non-natural termination;
        # "natural" is the ordinary unload between turns and must not be
        # mistaken for the end of the reply.
        if getattr(event, "reason", None) == "natural":
            return
        done.set()

    try:
        # Arm BEFORE waking, or a fast answer lands before anyone listens.
        unsubs = [
            client.subscribe(EventType.AGENT_OUTPUT, on_output),
            client.subscribe_once(EventType.AGENT_COMPLETED, on_terminal),
            client.subscribe(EventType.SESSION_TERMINATED, on_terminal),
            client.subscribe(EventType.AGENT_ERROR, on_terminal),
        ]
        # Attach so the woken turn's events reach us: a quiet cascade session
        # is unloaded, and session.wake revives it without restoring anyone's
        # event stream.
        await client.attach_session(session_id)
        await client.execute_command(
            "session.wake",
            payload={
                "session_id": session_id,
                "text": question,
                "source": "interrogator",
                "event_id": f"interrogate:{session_id}:1",
            },
        )
        try:
            await asyncio.wait_for(done.wait(), TERMINAL_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[error] no terminal event within {TERMINAL_TIMEOUT:.0f}s")
        for u in unsubs:
            u()
    finally:
        await client.disconnect()

    print("\n=== what the session said ===\n")
    print("".join(said).strip() or "(no prose captured)")
    return 0


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    session_id, workspace, env_file, qfile = sys.argv[1:5]
    config_root = sys.argv[5] if len(sys.argv) > 5 else None
    question = Path(qfile).read_text(encoding="utf-8")
    return asyncio.run(
        interrogate(session_id, workspace, env_file, question, config_root))


if __name__ == "__main__":
    sys.exit(main())
