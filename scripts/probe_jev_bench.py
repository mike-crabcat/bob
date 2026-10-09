"""Attention-probe bench for TypeSafe Jev (OpenRouter decisions model).

Jev is not a chat model: it answers typed questions over a ``state`` object
via POST /api/alpha/decisions. The probe maps onto one ``choice`` question
(ACT / WAIT / STAND_DOWN). This replays (a) the golden probe set and (b) the
most recent live probe transcripts (read-only from llm_call_log), and
reports accuracy / agreement and latency. Nothing is written to bob.db.

    uv run python scripts/probe_jev_bench.py [-n 15] [--model ~typesafe/jev-latest]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

from server.config import Settings
from server.services.attention.tier2 import probe_system_prompt

URL = "https://openrouter.ai/api/alpha/decisions"

CRITERIA = {
    "STAND_DOWN": (
        "Default. Bob was not spoken to and is not mid-exchange: banter, jokes or "
        "reactions between others; messages addressed to another specific person; "
        "Bob spoke earlier but the conversation moved on; pure social exchange with "
        "nothing Bob can answer or act on. When ambiguous, STAND_DOWN."),
    "ACT": (
        "Bob should respond now: the batch continues an active exchange Bob was part "
        "of in the last few turns; Bob was asked something recently that is still "
        "unanswered; or a question/request aimed at the GROUP that Bob can answer or "
        "do (he is the group's assistant — it need not name him). Third-party names "
        "inside a question are topics, not addressees."),
    "WAIT": (
        "Defer and re-check shortly: the sender appears mid-thought (fragments, "
        "trailing '...'), or several people are actively deciding/discussing and the "
        "thread hasn't settled yet."),
}


def decide(key: str, model: str, transcript: str) -> tuple[dict, float]:
    body = {
        "model": model,
        "state": {
            "gate_policy": probe_system_prompt("Bob").split("\nRespond with ONLY")[0],
            "group_chat_batch": transcript,
        },
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": ("Group-chat attention gate for the chatbot Bob: given "
                                 "the policy and the pending batch, should Bob respond?"),
                "criteria": CRITERIA,
            }
        },
    }
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        out = {"error": f"HTTP {e.code}: {e.read()[:300]!r}"}
    return out, time.monotonic() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=15)
    ap.add_argument("--model", default="~typesafe/jev-latest")
    args = ap.parse_args()
    s = Settings.from_env()
    key = (Path(s.config_dir).expanduser() / "openrouter_api_key").read_text().strip()

    golden = json.loads((Path(__file__).resolve().parents[1] / "tests" / "fixtures"
                         / "probe_golden.json").read_text())["cases"]
    conn = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT created_at, model, messages_json, response_text FROM llm_call_log "
        "WHERE call_category='attention_probe' AND status='completed' "
        "AND session_key LIKE 'agent:main:whatsapp:group:%' "
        "ORDER BY created_at DESC LIMIT ?", (args.n,)).fetchall()

    lat, costs, conf = [], [], []

    def run(label: str, transcript: str, ref: str) -> bool:
        out, dt = decide(key, args.model, transcript)
        if "error" in out or "answers" not in out:
            print(f"  ERR  {label:<34} {str(out.get('error') or out)[:160]}")
            return False
        a = out["answers"]["decision"]
        lat.append(dt)
        costs.append((out.get("usage") or {}).get("cost") or 0)
        conf.append(a.get("confidence") or 0)
        ok = a.get("choice") == ref
        probs = " ".join(f"{k[:2]}={v:.2f}" for k, v in (a.get("probabilities") or {}).items())
        print(f"  {'ok ' if ok else 'MISS'} {label:<34} ref={ref:<10} jev={a.get('choice'):<10} "
              f"{dt:5.2f}s  {probs}")
        return ok

    print(f"model {args.model}\n\nGolden set ({len(golden)}):")
    g_ok = sum(run(c["name"], c["context"], c["label"].upper()) for c in golden)
    print(f"\nLive probes (last {len(rows)}; ref = live verdict):")
    l_ok = 0
    for created, model, mj, resp in rows:
        msgs = json.loads(mj)
        transcript = next((m["content"] for m in msgs if m.get("role") == "user"), "")
        try:
            ref = json.loads(resp).get("decision", "?")
        except ValueError:
            continue
        l_ok += run(f"{created[11:19]} ({model.split('/')[-1]})", transcript, ref)

    print(f"\ngolden accuracy {g_ok}/{len(golden)} · live agreement {l_ok}/{len(rows)}")
    if lat:
        lat_s = sorted(lat)
        print(f"latency p50={statistics.median(lat_s):.2f}s p90={lat_s[int(len(lat_s)*.9)]:.2f}s "
              f"max={lat_s[-1]:.2f}s · mean cost ${statistics.mean(costs):.6f}/call "
              f"· mean confidence {statistics.mean(conf):.2f}")


if __name__ == "__main__":
    main()
