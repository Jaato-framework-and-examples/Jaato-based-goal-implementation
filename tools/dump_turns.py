#!/usr/bin/env python3
"""Render a session's history as readable turns.

    python tools/dump_turns.py <session.json> [> transcript.md]

Sessions are written to ``<config_root>/sessions/<id>.json``. The file holds
the whole conversation — prose, every tool call with its arguments, every
result — which is the only place some things are visible: which tool the agent
reached for first, whether a call failed and how it recovered, whether it
looked something up or assumed it.

Several findings in this repo came out of reading these rather than the
driver's stdout. The driver prints outcomes; the transcript shows the reasoning
that produced them, and the two can disagree — a run can exit 0 having wasted
three calls on a wrong assumption, and only the transcript says so.

Absolute paths are rewritten to ``<workspace>`` so a transcript can be
committed without carrying someone's home directory into a public repo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _clean(text: str, workspace: str | None) -> str:
    """Strip the absolute workspace path out of captured output."""
    return text.replace(workspace, "<workspace>") if workspace else text


def _block(body: str, limit: int, workspace: str | None) -> str:
    body = _clean((body or "").strip(), workspace)
    if len(body) > limit:
        body = body[:limit] + "\n… [truncated]"
    return f"```\n{body}\n```\n"


def _calls(row: dict) -> list:
    """The turn's tool calls.

    ``function_calls`` is a LIST of per-call records — name, start, end,
    duration — not a count. Rendering it directly puts every timestamp in one
    table cell, which is how this was first shipped.
    """
    calls = row.get("function_calls")
    return calls if isinstance(calls, list) else []


def _call_count(row: dict) -> int:
    return len(_calls(row))


def _tool_names(row: dict) -> str:
    """Which tools the turn used, in order, each named once.

    The sequence is the interesting part — whether the agent looked something
    up before acting on it is visible here and nowhere else.
    """
    seen = []
    for call in _calls(row):
        name = call.get("name") if isinstance(call, dict) else None
        if name and name not in seen:
            seen.append(name)
    return ", ".join(f"`{n}`" for n in seen) or "—"


def render(session: Path) -> str:
    data = json.loads(session.read_text(encoding="utf-8"))
    workspace = data.get("workspace_path")
    driven = len(data.get("turn_accounting") or [])
    out = [f"# Session `{data['session_id']}`",
           "",
           f"Profile `{data.get('profile_name')}`. "
           f"{data['turn_count']} model steps across {driven} driven "
           f"turn(s) — the first, plus one per resume.",
           ""]

    turn = 0
    for message in data.get("history") or []:
        role = message.get("role")
        for part in message.get("parts") or []:
            if role == "user":
                turn += 1
                out += [f"## Turn {turn} — input", "",
                        _block(part.get("text", ""), 2500, workspace)]
            elif role == "model":
                text = (part.get("text") or "").strip()
                if text:
                    out += ["> " + _clean(text, workspace).replace("\n", "\n> "), ""]
                if part.get("name"):
                    args = _clean(json.dumps(part.get("args") or {}), workspace)
                    if len(args) > 700:
                        args = args[:700] + " …"
                    out += [f"**`{part['name']}`**", f"```json\n{args}\n```", ""]
            elif role == "tool":
                result = part.get("result")
                if not isinstance(result, str):
                    result = json.dumps(result)
                flag = " — ERROR" if part.get("is_error") else ""
                out += [f"<sub>→ `{part.get('name')}`{flag}</sub>",
                        _block(result, 900, workspace)]

    accounting = data.get("turn_accounting") or []
    if accounting:
        out += ["---", "", "## Accounting", "",
                "One row per turn the driver drove — the first, plus one per",
                "resume. Tokens are per turn, not cumulative.", "",
                "| turn | calls | tools | prompt | output | seconds |",
                "|---|---|---|---|---|---|"]
        for row in accounting:
            out.append(f"| {row.get('turn')} | {_call_count(row)} "
                       f"| {_tool_names(row)} | {row.get('prompt')} "
                       f"| {row.get('output')} "
                       f"| {row.get('duration_seconds', 0):.0f} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    session = Path(sys.argv[1])
    if not session.is_file():
        print(f"no such session file: {session}", file=sys.stderr)
        return 2
    print(render(session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
