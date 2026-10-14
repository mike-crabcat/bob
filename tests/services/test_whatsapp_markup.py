"""Markdown → WhatsApp markup at the outbound choke point (2026-10-14:
gpt-6-luna's ``**bold**`` and ``([domain](url))`` citations rendered raw
in AI Doom)."""

from server.services.whatsapp_bridge_service._service import _outbound_text
from server.services.whatsapp_markup import to_whatsapp

AI_DOOM = (
    "For the three from the preview:\n\n"
    "- **Gemini 4 Argon:** Google pitches a frontier model. "
    "([deepmind.google](https://deepmind.google/models/gemini/))\n"
    "- **Ledger implant:** Ledger confirmed an implant. "
    "([bleepingcomputer.com](https://www.bleepingcomputer.com/news/security/"
    "ledger-confirms/))")


def test_ai_doom_brief_gets_numbered_sources():
    out = to_whatsapp(AI_DOOM)
    assert "- *Gemini 4 Argon:* Google pitches a frontier model. [1]\n" in out
    assert "- *Ledger implant:* Ledger confirmed an implant. [2]\n" in out
    assert out.endswith(
        "\n\nSources:\n[1] https://deepmind.google/models/gemini/\n"
        "[2] https://www.bleepingcomputer.com/news/security/ledger-confirms/")
    assert "](" not in out and "**" not in out


def test_single_citation_inline_bare_url():
    out = to_whatsapp("Meta shipped it. ([reuters.com](https://reuters.com/x))")
    assert out == "Meta shipped it. https://reuters.com/x"


def test_repeated_url_shares_a_number():
    out = to_whatsapp("A ([a.com](https://a.com/1)) B ([b.com](https://b.com/2)) "
                      "C ([a.com](https://a.com/1))")
    assert out.startswith("A [1] B [2] C [1]")
    assert out.count("https://a.com/1") == 1


def test_multi_link_citation_group():
    out = to_whatsapp("X happened ([a.com](https://a.com/1), [b.com](https://b.com/2)).")
    assert out.startswith("X happened [1][2].")


def test_descriptive_link_keeps_label():
    out = to_whatsapp("See [the release notes](https://example.com/notes) first.")
    assert out == "See the release notes (https://example.com/notes) first."


def test_url_with_parens():
    out = to_whatsapp("[Perth](https://en.wikipedia.org/wiki/Perth_(disambiguation)) "
                      "is big")
    assert "https://en.wikipedia.org/wiki/Perth_(disambiguation)" in out
    assert "[" not in out


def test_emphasis_and_headings():
    assert to_whatsapp("## Today's **news**") == "*Today's news*"
    assert to_whatsapp("__big__ and ~~gone~~") == "_big_ and ~gone~"


def test_whatsapp_native_markup_untouched():
    for s in ("*bold* _it_ ~s~", "2 * 3 = 6", "snake_case_name", "a__b",
              "plain https://example.com/a_b link", "- item\n- item",
              "price: $5 [approx]"):
        assert to_whatsapp(s) == s


def test_code_untouched():
    s = "Run `ls **/*.md` then:\n```\n[x](https://a.com) **y**\n```"
    assert to_whatsapp(s) == s


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("BOB_WHATSAPP_MARKDOWN", "off")
    assert to_whatsapp("**x**") == "**x**"


def test_choke_point_applies_it():
    assert _outbound_text("**hi** ([a.com](https://a.com))") == "*hi* https://a.com"


def test_bold_url_left_bare():
    out = to_whatsapp("📻 **https://example.ts.net/radio**")
    assert out == "📻 https://example.ts.net/radio"


def test_openai_utm_tag_dropped():
    assert to_whatsapp("10 min away ([goanna.com.au](https://goanna.com.au/?utm_source=openai))") \
        == "10 min away https://goanna.com.au/"
    assert to_whatsapp("x https://a.com/p?utm_source=openai") == "x https://a.com/p"
    assert to_whatsapp("x https://a.com/p?utm_source=openai&id=3") == "x https://a.com/p?id=3"
    assert to_whatsapp("x https://a.com/p?id=3&utm_source=openai") == "x https://a.com/p?id=3"
