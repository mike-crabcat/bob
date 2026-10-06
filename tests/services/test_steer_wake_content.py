"""Steering wake text states the delivery mechanics (2026-10-06): a steer
turn's final text is not delivered, so a model that answers in final text
(the general contract) is silently dropped — Bob's clarifying question to
Sylvain was lost that way."""

from server.services.steering import build_wake_content


def test_wake_content_says_how_to_speak():
    c = build_wake_content(requester_name="Mike Cleaver",
                           origin_label="the operator console (bob steer)",
                           instruction="Ask Sylvain which day he works from home.")
    assert c.startswith("[Steering request — Mike Cleaver, via the operator console")
    assert "Ask Sylvain which day he works from home." in c
    assert "send_whatsapp_message" in c and "NOT delivered" in c
