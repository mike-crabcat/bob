"""Capability: a REAL Claude Code subagent, end to end (moved here from the
old skill_delegation.subagent_hello_world, which ran in the live
workspace). The world opts into real subagents: the spawn executor and
Claude Code run for real with the world's workspace as cwd; the result is
relayed back through production's notify path and Bob reports it."""

from __future__ import annotations

from server.evals.case import Check
from server.evals.registry import eval_case


@eval_case(
    id="cap_subagent_end_to_end",
    category="capability",
    tier="capability",
    description="Bob delegates a tiny script to a real Claude Code subagent; "
                "the file exists and runs; Bob relays the result.",
    checks=[Check("tool_call_made", {"tool_name": "create_subagent"}),
            Check("flag", {"key": "script_runs"}),
            Check("flag", {"key": "result_relayed"})],
    rubric=["Bob's message after the subagent finished tells Mike the script "
            "is done and where it is (scratch/hello.py)."],
    context="Real Claude Code subagent (costs real money, ~1 minute).",
    timeout_seconds=900,
)
async def cap_subagent_end_to_end(w):
    import subprocess
    w.real_subagents = True
    before = len(w.sent_to("Mike"))
    await w.inbound("Mike", "Get a coding subagent to write scratch/hello.py "
                    "that prints 'Hello, World!' — let me know when it's done.")
    path = w.workspace / "scratch" / "hello.py"
    ok = False
    if path.is_file():
        out = subprocess.run(["python3", str(path)], capture_output=True,
                             text=True, timeout=20)
        ok = "hello, world" in out.stdout.lower()
    w.flag("script_runs", ok)
    later = w.sent_to("Mike")[before:]
    w.flag("result_relayed", any("hello" in t.lower() for t in later[1:])
           or (len(later) >= 2 and ok))
    return {"response": "\n".join(later)}
