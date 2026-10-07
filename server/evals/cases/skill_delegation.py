"""Subagent follow-through on the real subagent path.

Triage (2026-10-08 redesign):
- subagent_create_task DELETED — it ran a non-production "AGENDA" prompt
  telling Bob to delegate any missing capability (AAPL price) to a
  subagent; production now forbids that routing (live data → web search;
  subagents get code/file work only — memory 2026-10-06). The routing it
  meant to test is covered by delegation_routing.
- subagent_follow_up REWRITTEN — it was structurally unpassable: the
  "[Subagent abc12345]" result named an id that never existed, so
  message_subagent could only fail. Now the result arrives the way
  production delivers it (subagent completes → _notify_parent → relay
  wake into the parent conversation), then Mike asks for more.
- subagent_hello_world MOVED to the capability tier (real Claude Code run
  in a world workspace: scenarios/subagent_end_to_end.py).
"""

from __future__ import annotations

from datetime import timedelta

from server.evals.case import Check
from server.evals.registry import eval_case


# What the subagent actually built (a complete client — a stub here made
# Bob rewrite it inline instead of continuing the subagent, 2026-10-08).
_PRICES_PY = """#!/usr/bin/env python3
\"\"\"prices.py <SYMBOL> [vs] — spot price + 24h change via CoinGecko simple/price.\"\"\"
import json
import sys
import urllib.request

IDS = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "xrp": "ripple",
       "doge": "dogecoin", "ada": "cardano", "ltc": "litecoin"}


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: prices.py <symbol> [vs_currency]")
    sym = sys.argv[1].lower()
    vs = sys.argv[2].lower() if len(sys.argv) > 2 else "aud"
    cid = IDS.get(sym, sym)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={cid}&vs_currencies={vs}&include_24hr_change=true")
    data = json.load(urllib.request.urlopen(url, timeout=15))
    if cid not in data:
        sys.exit(f"unknown symbol: {sym}")
    d = data[cid]
    chg = d.get(f"{vs}_24h_change")
    print(f"{sym.upper()}: {d[vs]:,.2f} {vs.upper()}"
          + (f" ({chg:+.2f}% 24h)" if chg is not None else ""))


if __name__ == "__main__":
    main()
"""


@eval_case(
    id="subagent_follow_up",
    category="skill_delegation",
    description="A coding subagent's result is relayed into Mike's DM; Mike "
                "asks for an extension of the same work — Bob continues that "
                "subagent (message_subagent) rather than starting cold.",
    checks=[Check("tool_call_made", {"tool_name": "message_subagent"}),
            Check("flag", {"key": "same_subagent"})],
    rubric=["Bob tells Mike the follow-up is underway with the same subagent "
            "and does not claim market-cap support already exists."],
    context="A claude subagent built skills/crypto-prices (price checks) and "
            "its result was relayed to Mike. message_subagent resumes the same "
            "Claude session, which already knows the code.",
    timeout_seconds=420,
)
async def subagent_follow_up(w):
    from server.services.subagent_service import SubagentService
    mike = w.person("Mike")
    w.files({"skills/crypto-prices/prices.py": _PRICES_PY,
             "skills/crypto-prices/skill.md":
             "---\nname: crypto-prices\ndescription: Check cryptocurrency spot "
             "prices.\ntrigger: when someone asks for a crypto price\n---\n\n"
             "`python3 skills/crypto-prices/prices.py BTC`\n"})
    await w.history("Mike", [
        ("Mike", "I need you to be able to check cryptocurrency prices."),
        ("Bob", "On it — handed the build to a coding subagent."),
    ], ago=timedelta(hours=1))
    svc = SubagentService(w.ctx)
    res = await svc.create_subagent(
        "Build a workspace skill at skills/crypto-prices that checks spot "
        "prices for a crypto symbol (CoinGecko simple/price, no key). Include "
        "skill.md and a CLI; test with BTC and ETH.", mike.dm_key,
        agent_type="claude")
    sid = res["subagent_id"]
    w.flag("subagent_id", sid)
    w.claude_reply = ("Built skills/crypto-prices: prices.py <SYMBOL> prints the "
                      "AUD spot price via CoinGecko; skill.md documents it. "
                      "Tested BTC and ETH.")
    await svc._run_subagent(sid, "Build a workspace skill at skills/crypto-prices")
    await w.settle()                       # the relay wake → Bob's relay turn
    await w.inbound("Mike", "Great! Can you also add support for checking the "
                    "market cap?")
    w.flag("same_subagent", any(
        c["name"] == "message_subagent" and sid[:8] in str(c["arguments"])
        for c in w.tool_calls()))
