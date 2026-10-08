"""Owner-approved goal broadcasts (services/goal_broadcast.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from server.services import goal_broadcast as gb


def _goal(grant=None, status="active"):
    return {"id": "g1", "status": status,
            "payload_json": json.dumps({gb.GRANT_KEY: grant} if grant else {})}


def test_active_grant_respects_expiry_and_status():
    now = datetime.now(UTC)
    grant = {"groups": [{"gid": "1", "name": "A"}],
             "expires_at": (now + timedelta(hours=1)).isoformat()}
    assert gb.active_grant(_goal(grant)) == grant
    assert gb.active_grant(_goal(grant), now + timedelta(hours=2)) is None
    assert gb.active_grant(_goal(grant, status="completed")) is None
    assert gb.active_grant(_goal(None)) is None


def test_gid_normalisation():
    assert gb._gid("120363408889690088@g.us") == "120363408889690088"
    assert gb._gid("agent:main:whatsapp:group:120363408889690088") == "120363408889690088"


def test_content_key_stable_per_group_and_content():
    a = gb._content_key("g", "1", "hi", "p.png")
    assert a == gb._content_key("g", "1", "hi", "p.png")
    assert a != gb._content_key("g", "2", "hi", "p.png")
    assert a != gb._content_key("g", "1", "hi!", "p.png")


class _Groups:
    def __init__(self, db):
        pass

    async def actively_bound_groups(self):
        return [{"name": "AI doom", "whatsapp_jid": "111111111111@g.us"},
                {"name": "Bob's Pirate Radio", "whatsapp_jid": "222222222222@g.us"},
                {"name": "Bob Law", "whatsapp_jid": "333333333333@g.us"}]


@pytest.mark.asyncio
async def test_resolve_groups(monkeypatch):
    import server.repositories.groups as groups_mod
    monkeypatch.setattr(groups_mod, "GroupRepository", _Groups)
    import server.services.group_tools as gt

    async def _no_alias(db, name):
        return [{"name": "Bob Law", "whatsapp_jid": "333333333333@g.us"}] \
            if name == "Weeming Law" else []
    monkeypatch.setattr(gt, "groups_by_alias", _no_alias)
    ctx = type("C", (), {"db": None})()
    ok, _ = await gb.resolve_groups(ctx, ["Weeming Law"])
    assert [g["gid"] for g in ok] == ["333333333333"]
    ok, errs = await gb.resolve_groups(ctx, ["AI doom", "222222222222@g.us", "ai doom"])
    assert [g["gid"] for g in ok] == ["111111111111", "222222222222"]
    assert errs == []
    ok, errs = await gb.resolve_groups(ctx, ["Bob", "999999999999", "nope"])
    assert ok == []
    assert len(errs) == 3 and "ambiguous" in errs[0]
