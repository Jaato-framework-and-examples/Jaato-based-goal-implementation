"""CLI entry point: ``goal-cascade "<goal>"``.

Thin argument plumbing over :class:`goal_cascade.driver.GoalCascade`. Defaults
resolve relative to the repository so the example runs from a fresh checkout
with only a provider key set.
"""

from __future__ import annotations

import argparse
import asyncio
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
        # than simply dying at the ceiling. Rungs are declared here so the
        # example shows the shape; tier names come from the profile's models.
        degrade=[{"at": 80.0, "action": "warn"}],
    )
    return asyncio.run(cascade.run())


if __name__ == "__main__":
    sys.exit(main())
