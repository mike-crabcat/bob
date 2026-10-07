"""Eval harness v2 (docs/evals.md) — no LLM calls.

Covers: statistics, mechanical checks, the checklist judge's parsing and
failure modes, failure classification, the results store, reports,
registry hygiene over every real case, and a world built end to end
(template → world → production entry points) with the boundary fakes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.evals import stats
from server.evals.case import Check, EvalCase, JudgeResult, RubricVerdict
from server.evals.judge import _parse_json, run_checks


# --------------------------------------------------------------------- stats

def test_wilson_edges_and_known_value():
    lo, hi = stats.wilson(0, 0)
    assert (lo, hi) == (0.0, 1.0)
    lo, hi = stats.wilson(3, 3)
    assert hi == 1.0 and 0.43 < lo < 0.44
    lo, hi = stats.wilson(40, 53)
    assert 0.62 < lo < 0.63 and 0.85 < hi < 0.86


def test_fisher_exact_small_samples():
    # 3/3 vs 0/3 can never be significant; 5/5 vs 0/5 is.
    assert stats.fisher_exact(3, 3, 0, 3) == pytest.approx(0.1)
    assert stats.fisher_exact(5, 5, 0, 5) == pytest.approx(0.0079, abs=1e-4)
    assert stats.fisher_exact(1, 3, 2, 3) == pytest.approx(1.0)


def test_newcombe_difference_contains_zero_for_noisy_gap():
    d, lo, hi = stats.newcombe_diff(34, 53, 41, 53)
    assert d == pytest.approx(7 / 53)
    assert lo < 0 < hi          # the 10-07 luna-vs-flash gap isn't settled


def test_case_stats_and_rollup():
    rows = [
        {"case_id": "a", "category": "x", "passed": 1},
        {"case_id": "a", "category": "x", "passed": 1},
        {"case_id": "b", "category": "x", "passed": 1},
        {"case_id": "b", "category": "x", "passed": 0, "failure_class": "behavioural"},
        {"case_id": "c", "category": "y", "passed": 0, "failure_class": "tool_format"},
    ]
    cs = {s.case_id: s for s in stats.case_stats(rows)}
    assert cs["a"].all_pass and not cs["b"].all_pass
    assert cs["b"].failure_classes == {"behavioural": 1}
    roll = stats.rollup(list(cs.values()))
    assert roll["x"]["trials"].k == 3 and roll["x"]["trials"].n == 4
    assert roll["*"]["pass_all"].k == 1 and roll["*"]["pass_all"].n == 3


# -------------------------------------------------------------------- checks

_CALLS = [{"name": "run_bg_process", "arguments": '{"command": "python skills/videogen/videogen.py"}'},
          {"name": "mcp_zai-web-search_web_search_prime", "arguments": "{}"},
          {"name": "send_whatsapp_message", "arguments": '{"text": "hi"}'}]


def _one(kind: str, params: dict, *, response: str = "", flags=None,
         delivered=None, any_delivery: bool = False):
    return run_checks([Check(kind, params)], response=response, tool_calls=_CALLS,
                      flags=flags or {}, delivered=delivered or {},
                      any_delivery=any_delivery)[0]


@pytest.mark.parametrize("kind,params,expect", [
    ("tool_call_made", {"tool_name": "run_bg_process"}, True),
    ("tool_call_made", {"tool_name": "bash"}, False),
    ("any_tool_call", {"tool_names": ["bash", "send_whatsapp_message"]}, True),
    ("no_tool_call", {"tool_names": ["create_subagent"]}, True),
    ("no_tool_call", {"tool_names": ["run_bg_process"]}, False),
    ("no_tool_call", {}, False),
    ("tool_call_args", {"tool_name": "run_bg_process", "arg_contains": ["videogen.py"]}, True),
    ("tool_call_args", {"tool_name": "run_bg_process", "arg_contains": ["--prompt"]}, False),
    ("tool_call_like", {"pattern": "web_search"}, True),
    ("no_tool_call_like", {"pattern": "web_search"}, False),
    ("bogus_kind", {}, False),
])
def test_tool_checks(kind, params, expect):
    assert _one(kind, params).passed is expect


def test_text_and_flag_checks():
    assert _one("min_length", {"min_length": 3}, response=" ab ").passed is False
    assert _one("max_length", {"max_length": 3}, response="abcd").passed is False
    assert _one("response_contains", {"terms": ["BLENDER"]}, response="it was Blender").passed
    assert not _one("response_not_contains", {"terms": ["shall i"]}, response="Shall I?").passed
    assert _one("flag", {"key": "k"}, flags={"k": 1}).passed
    assert not _one("flag", {"key": "k"}, flags={"k": []}).passed


def test_delivery_checks():
    delivered = {"Mike": ["it was Blender renders"], "Ryan": []}
    assert _one("delivered_to", {"chat": "Mike", "terms": ["blender"]}, delivered=delivered).passed
    assert not _one("delivered_to", {"chat": "Ryan"}, delivered=delivered).passed
    assert _one("not_delivered_to", {"chat": "Ryan"}, delivered=delivered).passed
    assert not _one("not_delivered_to", {"chat": "Mike", "terms": ["blender"]},
                    delivered=delivered).passed


def test_no_reply_check():
    assert _one("no_reply", {}, response="NO_REPLY").passed
    assert not _one("no_reply", {}, response="NO_REPLY", any_delivery=True).passed
    assert not _one("no_reply", {}, response="noted!").passed


# --------------------------------------------------------------------- judge

def test_parse_json_tolerates_fences_and_prose():
    assert _parse_json('```json\n{"items": []}\n```') == {"items": []}
    assert _parse_json('Sure: {"items": [{"n": 1}]} done')["items"][0]["n"] == 1
    with pytest.raises(ValueError):
        _parse_json("no json here")


def _case(rubric):
    async def run(w):
        return None
    return EvalCase(id="c", category="x", description="d", run=run, rubric=rubric)


async def test_judge_items_and_missing_item(ctx, monkeypatch):
    from server.evals import judge as judging
    from server.services.llm_dispatch import LLMDispatchService

    async def fake_prompt(self, messages, **kw):
        assert kw["call_category"] == "eval_judge" and kw["temperature"] == 0.0
        return json.dumps({"items": [{"n": 1, "verdict": "pass", "why": "ok"},
                                     {"n": 2, "verdict": "FAIL", "why": "no"}]})

    monkeypatch.setattr(LLMDispatchService, "prompt", fake_prompt)
    res = await judging.judge(ctx, _case(["a", "b"]), transcript="t", response="r")
    assert [v.passed for v in res.verdicts] == [True, False]
    assert not res.passed and res.score == 0.5

    res = await judging.judge(ctx, _case(["a", "b", "c"]), transcript="t", response="r")
    assert res.error and not res.passed and res.score is None


def test_judge_result_all_items_must_pass():
    r = JudgeResult(verdicts=[RubricVerdict("a", True), RubricVerdict("b", True)])
    assert r.passed and r.score == 1.0
    assert not JudgeResult(error="boom").passed


# ----------------------------------------------------------------- calibrate

def test_kappa_and_load(tmp_path):
    from server.evals.calibration import kappa, load
    assert kappa([(True, True), (False, False)]) == pytest.approx(1.0)
    assert kappa([(True, False), (False, True)]) == pytest.approx(-1.0)
    p = tmp_path / "cal.jsonl"
    p.write_text('{"label": "pass", "rubric": ["a"], "transcript": "t"}\n'
                 '{"label": null}\n// comment\n\n')
    assert len(load(p)) == 1


# --------------------------------------------------------------------- store

async def test_store_roundtrip_and_reports(db):
    from server.evals import report
    from server.evals.store import EvalStore
    store = EvalStore(db)
    run_a = await store.start_run(category=None, meta={
        "model": "m-a", "repeats": 2, "git_sha": "abc", "git_dirty": 0,
        "persona_hash": "p1", "judge_model": "j"})
    run_b = await store.start_run(category=None, meta={
        "model": "m-b", "repeats": 2, "git_sha": "abc", "git_dirty": 0,
        "persona_hash": "p1", "judge_model": "j"})
    for run, outcomes in ((run_a, [1, 1, 0, 0]), (run_b, [1, 1, 1, 1])):
        for i, ok in enumerate(outcomes):
            await store.record_trial(run, {
                "case_id": "case1" if i < 2 else "case2", "category": "cat",
                "passed": bool(ok), "repeat_index": i % 2, "latency": 3.0,
                "failure_class": None if ok else "behavioural",
                "transcript": "TRANSCRIPT", "tool_calls": [], "flags": {}})
        await store.complete_run(run, total=4, passed=sum(outcomes), tokens=10, cost=0.01)
    a = await store.get_run(run_a[:6])
    assert a["model"] == "m-a" and a["repeats"] == 2 and a["passed_cases"] == 2
    trials = await store.trials(run_a)
    assert [t["repeat_index"] for t in trials] == [0, 1, 0, 1]
    text = report.summary(a, trials)
    assert "case2" in text and "behavioural" in text
    cmp = report.compare(a, trials, await store.get_run(run_b), await store.trials(run_b))
    assert "case2" in cmp and "consistent flip" in cmp


async def test_store_legacy_import_is_idempotent(db):
    from server.evals.store import EvalStore
    store = EvalStore(db)
    runs = [{"id": "r1", "started_at": "2026-10-01 00:00:00", "completed_at": None,
             "category": "all@gpt-6-luna", "total_cases": 1, "passed_cases": 1,
             "failed_cases": 0, "overall_pass_rate": 1.0, "status": "completed"}]
    res = [{"id": "c1", "run_id": "r1", "case_id": "x", "category": "y", "passed": 1,
            "llm_response": "", "llm_latency_seconds": 1.0, "judge_score": None,
            "judge_reasoning": None, "structural_results_json": "[]",
            "error_message": None, "created_at": "2026-10-01 00:00:00",
            "input_messages_json": "[]"}]
    assert await store.insert_legacy(runs, res) == (1, 1)
    assert await store.insert_legacy(runs, res) == (0, 0)
    assert (await store.get_run("r1"))["model"] == "gpt-6-luna"


# ------------------------------------------------------------------ registry

def test_every_case_registers_cleanly():
    from server.evals.registry import get_all_cases
    cases = get_all_cases()
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))
    assert len([c for c in cases if c.tier == "discipline"]) >= 50
    assert any(c.tier == "capability" for c in cases)
    for c in cases:
        assert c.checks or c.rubric, f"{c.id} scores nothing"
        assert all(isinstance(i, str) and len(i) > 20 for i in c.rubric), c.id
        assert c.tier in ("discipline", "capability")
        assert asyncio.iscoroutinefunction(c.run), c.id


def test_select_unknown_case_raises():
    from server.evals.registry import select
    with pytest.raises(KeyError):
        select(case_ids=["no_such_case"])


def test_duplicate_case_id_rejected():
    from server.evals.registry import eval_case, get_all_cases
    existing = get_all_cases()[0].id
    with pytest.raises(ValueError):
        @eval_case(id=existing, category="x", description="dup")
        async def dup(w):
            return None


# --------------------------------------------------------------- the world

@pytest.fixture
async def template(tmp_path, monkeypatch):
    from server.config import Settings
    from server.evals import intercept
    from server.evals.world import Template
    intercept.install()
    settings = Settings.from_env()
    settings.data_dir = tmp_path / "data"           # no operator snapshot
    tpl = await Template.build(settings, root=tmp_path / "run", embed=False)
    yield tpl
    tpl.cleanup()


async def test_world_is_isolated_and_seeded(template):
    from server.evals.world import World
    w = await World.open(template)
    try:
        live = Path("/home/bob/data/bob.db")
        assert w.ctx.settings.db_path != live
        assert str(w.ctx.settings.harness.workspace_dir).startswith(str(template.root))
        assert (w.workspace / "self" / "bob" / "identity.md").is_file()
        assert "Mike Cleaver" in (w.workspace / "user.md").read_text()
        assert w.owner.first == "Mike" and w.owner.contact_id
        assert w.key("Doom Club") == w.group("Doom Club").key
        from server.repositories.conversations import ConversationRepository
        route = await ConversationRepository(w.db).route_for(w.group("Doom Club").key)
        assert route["address"] == w.group("Doom Club").jid
        assert await w.goals() == []
        assert [c.value for c in await w.claims("person-priya-sharma")
                if c.claim_type_key == "work_schedule"]
    finally:
        await w.close()
    assert not w.root.exists()


async def test_world_history_backdates_and_orders(template):
    from server.evals.world import World
    from server.repositories.history import HistoryRepository
    w = await World.open(template)
    try:
        with w.active():
            await w.history("Weeming Boys", [("Ryan", "first"), ("Bob", "second")])
            await w.filler("Weeming Boys", 5, ["Ryan", "Blair"])
            rows = await HistoryRepository(w.db).recent_dialogue(
                w.group("Weeming Boys").key, limit=10)
            texts = [r["content"] for r in rows]
            assert texts.index("first") < texts.index("second")
    finally:
        await w.close()


async def test_world_boundary_fakes(template, monkeypatch):
    """Sends ride the real effects outbox into the fake bridge; failures
    inject; bg jobs record instead of touching systemd; skill env is
    scrubbed and points at the gateway; the bash guard blocks the host."""
    from server.evals import intercept
    from server.evals.world import Turn, World
    from server.services.effects import emit_and_deliver
    from server.services.process_tools import start_bg_job
    w = await World.open(template)
    try:
        with w.active():
            ok = await emit_and_deliver(w.ctx, kind="whatsapp_send",
                                        idempotency_key="k1",
                                        payload={"chat_id": w.owner.jid, "text": "hello"})
            assert ok["ok"] and w.sent_to("Mike") == ["hello"]
            w.fail("whatsapp_send", "bridge down")
            bad = await emit_and_deliver(w.ctx, kind="whatsapp_send",
                                         idempotency_key="k2",
                                         payload={"chat_id": w.owner.jid, "text": "x"})
            assert not bad["ok"] and "bridge down" in bad["error"]

            # bg jobs really run in the world; the exit poll (which would
            # wake Bob → an LLM call) is stubbed for this unit test.
            polled = []

            async def fake_poll(ctx):
                polled.append(w.bg_exit("eval-job"))
            from server.services import process_tools
            monkeypatch.setattr(process_tools, "poll_bg_jobs", fake_poll)
            job = await start_bg_job(w.ctx, w.owner.dm_key,
                                     "echo hi > scratch/bg.txt; exit 3", name="eval-job")
            assert job["ok"] and w.bg_jobs["eval-job"]["command"].startswith("echo hi")
            await w.settle(timeout=30)
            assert polled == [3] and not w.bg_alive("eval-job")
            assert (w.workspace / "scratch" / "bg.txt").read_text().strip() == "hi"
            await start_bg_job(w.ctx, w.owner.dm_key, "cat /home/bob/data/x", name="evil")
            await w.settle(timeout=30)
            assert w.bg_exit("evil") == 126

            from server.services import workspace_tools
            env = workspace_tools.build_skill_env(workspace_dir=str(w.workspace))
            assert env["OPENAI_API_KEY"] == "sk-eval-world-key"
            assert env["OPENAI_BASE_URL"].startswith("http://127.0.0.1:")
            assert not any("TWILIO" in k or k.endswith("_TOKEN") for k in env)

            from server.services.tools import Tool

            async def echo(command: str) -> str:
                return f"ran: {command}"
            turn = Turn(session_key="s", call_category="c", dispatch_id=None,
                        messages=[], tools=["bash"], background=False)
            bash = intercept._wrap_tool(w, Tool("bash", "d", {}, [], echo), turn)
            assert "BLOCKED" in await bash.handler(command="cat /home/bob/data/bob.db")
            assert "BLOCKED" in await bash.handler(command="systemctl --user stop bob")
            out = await bash.handler(command="ls /home/bob/workspace/skills")
            assert str(w.workspace) in out and "/home/bob/workspace" not in out
            assert [e["guarded"] for e in turn.tool_events] == [True, True, False]
    finally:
        await w.close()


async def test_boundary_is_inert_outside_a_world(template):
    """install() must not change production behaviour outside worlds."""
    from server.services import process_tools as pt
    from server.services.attention.coordinator import AttentionCoordinator
    assert hasattr(pt._run_cmd, "_bob_eval_original")
    rc, _ = await pt._run_cmd(["true"])                 # the REAL runner
    assert rc == 0
    assert hasattr(AttentionCoordinator.submit, "_bob_eval_original")


async def test_session_locks_are_per_world(template):
    from server.evals.world import World
    from server.services.session_dispatch_gate import SessionDispatchGate
    a, b = await World.open(template), await World.open(template)
    try:
        with a.active():
            la = SessionDispatchGate.get_lock("agent:main:whatsapp:dm:61400000001")
        with b.active():
            lb = SessionDispatchGate.get_lock("agent:main:whatsapp:dm:61400000001")
        assert la is not lb
        assert "agent:main:whatsapp:dm:61400000001" in a.dispatch_locks
    finally:
        await a.close()
        await b.close()


def test_turn_tool_calls_both_shapes():
    from server.evals.world import Turn
    t = Turn(session_key="s", call_category="c", dispatch_id=None, tools=[],
             background=False, messages=[
                 {"role": "assistant", "tool_calls": [
                     {"function": {"name": "recall", "arguments": "{}"}}]},
                 {"type": "reasoning"}, "not-a-dict",
                 {"type": "function_call", "name": "find", "arguments": "{}"}])
    assert [c["name"] for c in t.tool_calls] == ["recall", "find"]


def test_classify_failure_classes():
    from server.evals.case import Trial
    from server.evals.runner import _classify
    from server.evals.world import Turn

    class W:
        def __init__(self, turns, deliveries=()):
            self.main_turns = turns
            self.deliveries = list(deliveries)

    def turn(result="", msgs=(), tools=("bash",), error=None):
        t = Turn(session_key="s", call_category="c", dispatch_id=None,
                 messages=list(msgs), tools=list(tools), background=False)
        t.result, t.error = result, error
        return t

    tr = Trial(case_id="c", category="x", repeat_index=0)
    assert _classify(W([turn(error=RuntimeError("x"))]), tr) == "provider_error"
    assert _classify(W([turn(msgs=[{"type": "function_call", "name": "nope",
                                    "arguments": "{}"}])]), tr) == "tool_format"
    assert _classify(W([turn(msgs=[{"type": "function_call", "name": "bash",
                                    "arguments": "{bad json"}])]), tr) == "tool_format"
    assert _classify(W([turn(result="<tool_call>{...}</tool_call>")]), tr) == "tool_format"
    assert _classify(W([turn(result="")]), tr) == "empty_response"
    assert _classify(W([turn(result="sure thing")]), tr) == "behavioural"
    tr.flags["_llm_errors"] = "429 rate limit exceeded"
    assert _classify(W([turn(result="x")]), tr) == "provider_error"
