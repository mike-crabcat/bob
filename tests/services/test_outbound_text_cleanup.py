"""Every WhatsApp post is scrubbed at the transport choke point: the
2026-10-08 daily_ai_brief routine leaked OpenAI web_search citation
markers ("citeturn0search3") because routine sends bypassed the send
tool's cleanup."""

from server.services.whatsapp_bridge_service._service import _outbound_text

LEAKED = ("Anthropic shipped a new model today. citeturn0search3"
          "turn0news1 More soon.")


def test_citation_markers_stripped():
    out = _outbound_text(LEAKED)
    assert "" not in out and "" not in out and "" not in out
    assert "cite" not in out and "turn0search3" not in out
    assert out.startswith("Anthropic shipped a new model today.")
    assert "More soon." in out


def test_plain_text_untouched():
    assert _outbound_text("hello there") == "hello there"
