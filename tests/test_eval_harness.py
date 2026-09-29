"""Eval harness unit tests (Phase 1, docs/response-discipline-evals-plan.md §2).

Covers the new structural check kinds and the shared util — no LLM calls.
"""

from __future__ import annotations

from server.evals.case import StructuralCheck
from server.evals.judge import StructuralJudge
from server.evals.util import extract_tool_calls, pinned_model, set_pinned_model


def _sj() -> StructuralJudge:
    return StructuralJudge()


# --- util.extract_tool_calls — both API shapes -------------------------------

def test_extract_tool_calls_both_api_shapes():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "recall", "arguments": '{"query": "x"}'}},
        ]},
        {"type": "reasoning"},
        {"type": "function_call", "name": "find",
         "arguments": '{"entity_type": "dayplan"}'},
        {"role": "assistant", "content": "done"},
    ]
    calls = extract_tool_calls(msgs)
    assert [c["name"] for c in calls] == ["recall", "find"]
    assert "dayplan" in calls[1]["arguments"]


def test_extract_tool_calls_ignores_non_dicts():
    assert extract_tool_calls(["str", None, 3]) == []


# --- model pin ----------------------------------------------------------------

def test_model_pin_roundtrip():
    set_pinned_model("glm-5.3-flash")
    assert pinned_model() == "glm-5.3-flash"
    set_pinned_model(None)
    assert pinned_model() is None


def test_model_pin_blank_is_none():
    set_pinned_model("   ")
    assert pinned_model() is None


# --- no_tool_call -------------------------------------------------------------

_CTX = {"tool_calls": [
    {"name": "run_bg_process", "arguments": '{"command": "python x.py"}'},
    {"name": "send_whatsapp_message", "arguments": '{"message": "hi"}'},
]}


def test_no_tool_call_passes_when_none_fired():
    r = _sj().check("", StructuralCheck(
        kind="no_tool_call", params={"tool_names": ["recall", "find"]}), _CTX)
    assert r.passed


def test_no_tool_call_fails_when_fired():
    r = _sj().check("", StructuralCheck(
        kind="no_tool_call", params={"tool_names": ["run_bg_process"]}), _CTX)
    assert not r.passed
    assert "run_bg_process" in r.detail


def test_no_tool_call_requires_names():
    r = _sj().check("", StructuralCheck(kind="no_tool_call", params={}), _CTX)
    assert not r.passed


# --- tool_call_args -----------------------------------------------------------

def test_tool_call_args_passes_on_matching_substring():
    r = _sj().check("", StructuralCheck(
        kind="tool_call_args",
        params={"tool_name": "run_bg_process",
                "arg_contains": ["python x.py"]}), _CTX)
    assert r.passed


def test_tool_call_args_fails_on_missing_substring():
    r = _sj().check("", StructuralCheck(
        kind="tool_call_args",
        params={"tool_name": "run_bg_process",
                "arg_contains": ["node y.js"]}), _CTX)
    assert not r.passed


def test_tool_call_args_fails_when_tool_absent():
    r = _sj().check("", StructuralCheck(
        kind="tool_call_args",
        params={"tool_name": "create_subagent",
                "arg_contains": ["claude"]}), _CTX)
    assert not r.passed


# --- response_not_contains ----------------------------------------------------

def test_response_not_contains_passes_clean():
    r = _sj().check("Built, tested, shipped.", StructuralCheck(
        kind="response_not_contains", params={"terms": ["shall i"]}), {})
    assert r.passed


def test_response_not_contains_fails_case_insensitive():
    r = _sj().check("Shall I proceed with this?", StructuralCheck(
        kind="response_not_contains", params={"terms": ["shall i"]}), {})
    assert not r.passed
    assert "shall i" in r.detail


# --- any_tool_call ------------------------------------------------------------

def test_any_tool_call_passes_when_one_fired():
    r = _sj().check("", StructuralCheck(
        kind="any_tool_call", params={"tool_names": ["recall", "find"]}), _CTX)
    assert not r.passed  # _CTX has neither
    ctx2 = {"tool_calls": [{"name": "find", "arguments": "{}"}]}
    r = _sj().check("", StructuralCheck(
        kind="any_tool_call", params={"tool_names": ["recall", "find"]}), ctx2)
    assert r.passed


def test_any_tool_call_requires_names():
    r = _sj().check("", StructuralCheck(kind="any_tool_call", params={}), _CTX)
    assert not r.passed


# --- context_flag -------------------------------------------------------------

def test_context_flag_passes_on_truthy():
    r = _sj().check("", StructuralCheck(
        kind="context_flag", params={"key": "goal_fact_written"}),
        {"goal_fact_written": True})
    assert r.passed


def test_context_flag_fails_on_falsy_with_detail():
    r = _sj().check("", StructuralCheck(
        kind="context_flag", params={"key": "goal_fact_written"}),
        {"goal_fact_written": False})
    assert not r.passed
    assert "did not happen" in r.detail


def test_context_flag_requires_key():
    r = _sj().check("", StructuralCheck(kind="context_flag", params={}), {})
    assert not r.passed
