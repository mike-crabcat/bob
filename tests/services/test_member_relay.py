"""Member relay (share_to_group) — the 2026-09-17 Mike split: non-owners
share exact content into groups they're in, under a mechanically-prepended
attribution line; nothing wakes, so requester text never enters a
privileged prompt. Delivery reuses the group-send primitive (rate-limit
budget shared); requester membership binds the target exactly as it does
for steering."""

from __future__ import annotations

OWNER_ID = "c-mike"
BRAD_ID = "c-brad"
GROUP_KEY = "agent:main:whatsapp:group:120363430111642553"
GROUP_JID = "120363430111642553@g.us"
DM_KEY = "agent:main:whatsapp:dm:447523520214"


async def _seed(ctx, *, brad_member: bool = True) -> None:
    now = "2026-09-17 00:00:00"
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_default, is_trusted, "
        "created_at, updated_at) VALUES (?, 'Mike', '+61400000000', 1, 1, ?, ?)",
        (OWNER_ID, now, now))
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES (?, 'Brad', '+447523520214', 1, ?, ?)",
        (BRAD_ID, now, now))

    from server.repositories.conversations import ConversationRepository
    from server.repositories.groups import GroupRepository
    groups = GroupRepository(ctx.db)
    convs = ConversationRepository(ctx.db)
    await groups.ensure_group(GROUP_JID, "Bob and Nikesh", now)
    await convs.register_endpoint(GROUP_KEY, endpoint_kind="group",
                                  address=GROUP_JID)
    await convs.register_endpoint(DM_KEY, endpoint_kind="dm",
                                  contact_id=BRAD_ID)
    if brad_member:
        await ctx.db.execute(
            """INSERT INTO whatsappgroup_members
               (id, group_id, contact_id, joined_at, created_at, updated_at)
               SELECT 'gm-1', id, ?, ?, ?, ? FROM whatsappgroups
               WHERE whatsapp_jid = ?""",
            (BRAD_ID, now, now, now, GROUP_JID))


def _capture_delivery(monkeypatch):
    """Capture _deliver_group_send where member_relay imported it."""
    calls: list[dict] = []

    async def _fake(ctx, *, group_key, group_id, message, origin_session_key,
                    idempotency_key, provenance, now, media_path=""):
        calls.append({"group_key": group_key, "group_id": group_id,
                      "message": message, "provenance": provenance,
                      "media_path": media_path,
                      "origin": origin_session_key})
        return {"ok": True}

    import server.services.member_relay as relay_mod
    monkeypatch.setattr(relay_mod, "_deliver_group_send", _fake)
    return calls


async def _share(ctx, requester: str, **kwargs) -> dict:
    import json

    from server.services.member_relay import make_relay_tools
    tools = make_relay_tools(ctx, DM_KEY, requester)
    share = next(t for t in tools if t.name == "share_to_group")
    defaults = {"group": "Bob and Nikesh", "text": "the Everton song"}
    defaults.update(kwargs)
    return json.loads(await share.handler(**defaults))


async def test_member_share_prefixes_and_delivers(ctx, monkeypatch):
    await _seed(ctx)
    calls = _capture_delivery(monkeypatch)

    result = await _share(ctx, BRAD_ID)
    assert result["ok"] is True, result
    assert result["shared_with"] == "Bob and Nikesh"

    assert len(calls) == 1
    call = calls[0]
    # The attribution line is mechanical: requester's name, delivery-side
    assert call["message"].startswith("📣 Brad asked me to share:\n")
    assert "the Everton song" in call["message"]
    assert call["group_key"] == GROUP_KEY
    assert call["provenance"]["member_relay"] is True
    assert call["provenance"]["requester_name"] == "Brad"
    assert call["provenance"]["requester_contact_id"] == BRAD_ID


async def test_non_member_cannot_share(ctx, monkeypatch):
    """Membership binds the relay target (same resolution as steer) — a
    non-member gets candidates, and nothing is delivered."""
    await _seed(ctx, brad_member=False)
    calls = _capture_delivery(monkeypatch)

    result = await _share(ctx, BRAD_ID)
    assert result["ok"] is False
    assert "Brad belongs to" in result["error"]  # no-groups branch
    assert calls == []


async def test_owner_shares_via_owner_wide_pool(ctx, monkeypatch):
    """The operator's reach is Bob's reach for relays too."""
    await _seed(ctx)
    _capture_delivery(monkeypatch)

    result = await _share(ctx, OWNER_ID)
    assert result["ok"] is True, result


async def test_dm_target_and_empty_content_rejected(ctx, monkeypatch):
    await _seed(ctx)
    _capture_delivery(monkeypatch)

    result = await _share(ctx, BRAD_ID, group="my chat", text="hi")
    assert result["ok"] is False
    assert "groups only" in result["error"]

    result = await _share(ctx, BRAD_ID, text="", media_path="")
    assert result["ok"] is False
    assert "nothing to share" in result["error"]


async def test_media_only_share_sends_bare_prefix(ctx, monkeypatch):
    await _seed(ctx)
    calls = _capture_delivery(monkeypatch)

    result = await _share(ctx, BRAD_ID, text="", media_path="scratch/x.mp3")
    assert result["ok"] is True, result
    assert calls[0]["message"] == "📣 Brad asked me to share:"
    assert calls[0]["media_path"] == "scratch/x.mp3"
