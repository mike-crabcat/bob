"""Attention-probe latency bench: replay EXACT live probe prompts against
two models and compare latency + verdicts.

Prompts come verbatim from llm_call_log.messages_json (call_category =
'attention_probe', read-only). Calls go straight through OpenAIService —
the same transport the live probe uses, with the probe's parameters
(temperature 0, max_tokens 300, reasoning_effort low) — and bypass
LLMDispatchService so nothing is logged into bob.db. Model order
alternates per prompt so neither side systematically goes first.

    uv run python scripts/probe_latency_bench.py [-n 12] [-a MODEL] [-b MODEL] [--providers P]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import time

from server.config import Settings
from server.context import AppContext
from server.services.openai_service import OpenAIService, StreamResult


def load_prompts(db_path: str, n: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT created_at, model, messages_json, response_text, latency_seconds "
        "FROM llm_call_log WHERE call_category = 'attention_probe' "
        "AND status = 'completed' AND messages_json IS NOT NULL "
        "ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


def decision(text: str) -> str:
    try:
        return str(json.loads(text.strip()).get("decision", "?"))
    except (ValueError, AttributeError):
        return "PARSE_FAIL"


async def call(svc: OpenAIService, messages: list[dict], model: str) -> dict:
    sr = StreamResult()
    t0 = time.monotonic()
    try:
        text = await svc.prompt(messages=messages, model=model, temperature=0.0,
                                max_tokens=300, reasoning_effort="low",
                                stream_result=sr, call_meta={})
        err = None
    except Exception as e:  # noqa: BLE001 — a failed call is a data point
        text, err = "", f"{type(e).__name__}: {e}"[:120]
    return {"lat": time.monotonic() - t0, "ttft": sr.ttft_seconds,
            "pt": sr.prompt_tokens, "ct": sr.completion_tokens,
            "decision": decision(text) if not err else "ERROR", "err": err}


def pct(v: list[float], p: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=12)
    ap.add_argument("-a", default="z-ai/glm-5.3-flash")
    ap.add_argument("-b", default="gpt-6-luna")
    ap.add_argument("--providers", default=None,
                    help="override BOB_OPENROUTER_PROVIDERS pin for this run "
                         "(e.g. Z.AI, or 'off'); OpenRouter models only")
    args = ap.parse_args()

    settings = Settings.from_env()
    if args.providers is not None:
        settings.openrouter.providers = args.providers
    svc = OpenAIService(AppContext(db=None, settings=settings))  # type: ignore[arg-type]  # transport only, no DB
    prompts = load_prompts(str(settings.db_path), args.n)
    models = (args.a, args.b)
    res: dict[str, list[dict]] = {m: [] for m in models}

    print(f"{len(prompts)} live probe prompts · {models[0]} vs {models[1]}\n")
    hdr = f"{'#':>2} {'live':<11}"
    for m in models:
        hdr += f" | {m[-14:]:<14} {'lat':>6} {'ttft':>5} {'ct':>4}"
    print(hdr)
    for i, p in enumerate(prompts):
        messages = json.loads(p["messages_json"])
        order = models if i % 2 == 0 else models[::-1]
        out = {m: await call(svc, messages, m) for m in order}
        line = f"{i:>2} {decision(p['response_text']):<11}"
        for m in models:
            r = out[m]
            res[m].append(r)
            ttft = f"{r['ttft']:.2f}" if r["ttft"] is not None else "-"
            line += (f" | {r['decision']:<14} {r['lat']:>6.2f} {ttft:>5} "
                     f"{r['ct'] if r['ct'] is not None else '-':>4}")
        print(line)
        for m in models:
            if out[m]["err"]:
                print(f"     {m}: {out[m]['err']}")

    print()
    live = [decision(p["response_text"]) for p in prompts]
    for m in models:
        ok = [r for r in res[m] if not r["err"]]
        lats = [r["lat"] for r in ok]
        ttfts = [r["ttft"] for r in ok if r["ttft"] is not None]
        cts = [r["ct"] for r in ok if r["ct"] is not None]
        agree = sum(r["decision"] == d for r, d in zip(res[m], live))
        if not lats:
            print(f"{m}: all {len(res[m])} calls failed")
            continue
        print(f"{m}: n={len(ok)}/{len(res[m])} p50={statistics.median(lats):.2f}s "
              f"p90={pct(lats, 0.9):.2f}s max={max(lats):.2f}s mean={statistics.mean(lats):.2f}s"
              + (f" ttft_p50={statistics.median(ttfts):.2f}s" if ttfts else "")
              + (f" out_tok_p50={statistics.median(cts):.0f}" if cts else "")
              + f" · agrees with live verdict {agree}/{len(live)}")
    both = sum(ra["decision"] == rb["decision"]
               for ra, rb in zip(res[models[0]], res[models[1]]))
    print(f"{models[0]} vs {models[1]} verdict agreement: {both}/{len(prompts)}")


if __name__ == "__main__":
    asyncio.run(main())
