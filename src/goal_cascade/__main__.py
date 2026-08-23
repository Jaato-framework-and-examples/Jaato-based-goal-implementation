"""CLI entry point: ``goal-cascade "<goal>"``.

Thin argument plumbing over :class:`goal_cascade.driver.GoalCascade`. Defaults
resolve relative to the repository so the example runs from a fresh checkout
with only a provider key set.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from .driver import GoalCascade

DEFAULT_GOAL = (
    "Get the job in fixtures/ to pass, then write a short REPORT.md explaining "
    "what was wrong and how many attempts it took. Start it with "
    "`python fixtures/slow_job.py start`, and check it with "
    "`python fixtures/slow_job.py status`. It takes a while to finish — do not "
    "wait for it inside a turn."
)


def _reset_demo_fixture(repo: Path) -> None:
    """Put the demo fixture back to its broken state before a run.

    Only for the built-in goal. A caller who supplied their own goal is
    pointing this at their own workspace, and resetting files there would be
    us deciding what their runtime state should be.

    Runs are only comparable if they start from the same place. The agent's fix
    is an edit to ``fixtures/job_config.json``, so without this the second run
    of the demo finds the bug already fixed and has nothing to diagnose. The
    swe-bench example resets for the same reason — see
    ``examples/swe-bench/setup.py``.
    """
    fixture = repo / "fixtures" / "slow_job.py"
    result = subprocess.run([sys.executable, str(fixture), "reset"],
                            capture_output=True, text=True)
    print(f"[reset] {result.stdout.strip() or result.stderr.strip()}")

    report = repo / "REPORT.md"
    if report.exists():
        report.unlink()
        print("[reset] REPORT.md removed")

    state = repo / ".goal-cascade-state"
    if state.is_dir():
        shutil.rmtree(state)
        print("[reset] .goal-cascade-state/ removed")


def main() -> int:
    """Parse arguments and run one goal to a terminal outcome."""
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        prog="goal-cascade",
        description="Pursue a goal across suspends and resumes until it is met.",
    )
    parser.add_argument("goal", nargs="?", default=DEFAULT_GOAL,
                        help="the goal statement (defaults to the demo goal)")
    parser.add_argument("--workspace", default=str(repo))
    parser.add_argument("--env-file", default=str(repo / ".env"))
    parser.add_argument("--socket", default="/tmp/jaato.sock")
    parser.add_argument("--profile", default="goal-actor")
    parser.add_argument("--agent", default="goal-actor")
    parser.add_argument("--max-turns", type=int, default=40,
                        help="cascade ceiling in assistant TURNS (not resumes)")
    parser.add_argument("--max-usd", type=float, default=1.0)
    parser.add_argument("--max-seconds", type=int, default=3600)
    args = parser.parse_args()

    if args.goal == DEFAULT_GOAL:
        _reset_demo_fixture(repo)

    cascade = GoalCascade(
        args.goal,
        workspace=args.workspace,
        env_file=args.env_file,
        socket=args.socket,
        profile=args.profile,
        agent=args.agent,
        budget={
            "turns": args.max_turns,
            "usd": args.max_usd,
            "seconds": args.max_seconds,
        },
        # A brownout ladder: the goal gets cheaper as it burns budget rather
        # than simply dying at the ceiling.
        #
        # A rung cheapens a goal by REBINDING a tier, not by naming an action —
        # `model_tiers` here is a sparse overlay onto the table in
        # .jaato/profiles/goal-actor.yaml, and `planner` is the tier the actor
        # runs in. At 80 % of any budget dimension it drops from sonnet to
        # haiku and keeps going; the ceiling itself still stops it at 100 %.
        #
        # Rungs latch (fire once) and stack, so a ladder is ordered by `at`.
        #
        # The last two rungs are what actually bound the goal. `limits` alone
        # do NOT: they gate SPAWNS, and this cascade spawns exactly once, at
        # the start, when the budget is untouched. Nothing is ever admitted
        # again, so nothing is ever refused — a goal that never converges would
        # run past every limit set here. Only the degrade ladder reaches a
        # session that is already running.
        #
        #   95% finalize — tell the actor to wrap up and answer with what it
        #                  has, so a partial REPORT.md still gets written
        #  100% abort    — hard stop, for an actor that talks past the first
        #                  warning. Latches, so it cannot be resumed past.
        degrade=[
            {
                "at": 80.0,
                "model_tiers": {"planner": {"model": "anthropic/claude-haiku-4.5"}},
            },
            {"at": 95.0, "action": "finalize"},
            {"at": 100.0, "action": "abort"},
        ],
    )
    return asyncio.run(cascade.run())


if __name__ == "__main__":
    sys.exit(main())
