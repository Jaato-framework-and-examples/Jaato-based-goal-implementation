<!-- Copy this, replace the bracketed parts, pass the file to interrogate.py.

     The closing instruction matters more than it looks: a session created
     under a GOAL profile will otherwise answer and then fight its own
     completion processor, which wants a patch this question never involved.
     `suspended` is also the honest outcome — the session is not finishing
     anything, only pausing again. -->

Not a new goal — a question about work you have already done. [State plainly
whether anything is wrong. If the work was accepted, say so: an agent that
thinks it is in trouble writes apologies instead of accounts.]

[THE OBSERVATION. Quote it exactly — the log line, the file content, the diff.
Give what you saw, not your reading of it. A session handed an interpretation
tends to agree with it; one handed evidence goes and checks.]

[THE ASK. If you want commands run, list them and say to report the verbatim
output INCLUDING failures — "do not work around a failure, I want the failure".
An agent's instinct is to route around a broken thing and report success.]

Then tell me, in prose:
- [what you actually want to know]
- [what you think the cause was]
- [what would have worked instead, phrased as you would want to receive it]

When you have answered, end this turn with `signal_completion` and
outcome='suspended' — you are not finishing a goal, only answering — with a
resume_at a few minutes out and resume_reason "answered an operator question".
