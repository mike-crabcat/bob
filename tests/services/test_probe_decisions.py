"""Decisions-model attention probe (services/attention/decisions.py)."""

from unittest.mock import AsyncMock, patch

from server.services.attention import decisions as d
from server.services.attention import tier2


def _out(choice="ACT", conf=0.9, react=None, rconf=0.9):
    ans = {"decision": {"type": "choice", "choice": choice, "confidence": conf,
                        "probabilities": {"ACT": 0.9, "WAIT": 0.05, "STAND_DOWN": 0.05}}}
    if react:
        ans["react"] = {"type": "choice", "choice": react, "confidence": rconf}
    return {"answers": ans}


def test_is_decisions_model():
    assert d.is_decisions_model("openai/gpt-6-luna-decisions")
    assert d.is_decisions_model("~typesafe/jev-latest")
    assert not d.is_decisions_model("gpt-6-luna")
    assert not d.is_decisions_model("")


def test_parse_maps_choice_and_floor():
    r = d.parse_answers(_out("ACT", 0.9), reactions=False, floor=0.6)
    assert r["decision"] == "ACT" and "conf 0.90" in r["reason"]
    assert d.parse_answers(_out("ACT", 0.5), reactions=False, floor=0.6) is None
    assert d.parse_answers({"answers": {}}, reactions=False, floor=0.6) is None


def test_react_only_on_confident_stand_down():
    assert d.parse_answers(_out("STAND_DOWN", 0.9, "bob-fail"), reactions=True,
                           floor=0.6)["react"] == "bob-fail"
    assert d.parse_answers(_out("STAND_DOWN", 0.9, "none"), reactions=True,
                           floor=0.6)["react"] is None
    assert d.parse_answers(_out("ACT", 0.9, "bob-fail"), reactions=True,
                           floor=0.6)["react"] is None


def test_request_shape():
    body = d.build_request("openai/gpt-6-luna-decisions", "Mike: hi", "Bob", reactions=True)
    assert body["questions"]["decision"]["type"] == "choice"
    assert set(body["questions"]["decision"]["criteria"]) == {"ACT", "WAIT", "STAND_DOWN"}
    assert "none" in body["questions"]["react"]["criteria"]
    assert body["state"]["group_chat_batch"] == "Mike: hi"


async def test_decisions_used_when_confident(ctx):
    with patch.object(d, "decide", new=AsyncMock(return_value={
            "decision": "STAND_DOWN", "react": None, "reason": "x", "confidence": 0.9})), \
         patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock()) as chat:
        r = await tier2.probe_decide(ctx, "t", model="openai/gpt-6-luna-decisions",
                                     fallback_model="gpt-6-luna")
    assert r["decision"] == "STAND_DOWN" and "confidence" not in r
    chat.assert_not_called()


async def test_falls_back_to_chat_model(ctx):
    with patch.object(d, "decide", new=AsyncMock(return_value=None)), \
         patch("server.services.llm_dispatch.LLMDispatchService.prompt",
               new=AsyncMock(return_value='{"decision":"ACT","reason":"r"}')) as chat:
        r = await tier2.probe_decide(ctx, "t", model="openai/gpt-6-luna-decisions",
                                     fallback_model="gpt-6-luna")
    assert r["decision"] == "ACT"
    assert chat.await_args.kwargs["model"] == "gpt-6-luna"
