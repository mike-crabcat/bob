"""Markdown → WhatsApp markup for outbound text (2026-10-14).

Models (gpt-6-luna with hosted web_search especially) write CommonMark:
``**bold**`` renders in WhatsApp as ``*bold*`` with the outer stars showing,
and ``([domain](url))`` citations show the raw brackets — WhatsApp has no
link anchors. Applied at the transport choke point (``_outbound_text``) so
every model and every send path gets it.

- ``**x**`` → ``*x*``, ``__x__`` → ``_x_``, ``~~x~~`` → ``~x~``,
  ``# Heading`` → ``*Heading*``
- citation-shaped links (link text is just the domain, usually wrapped in
  parens): two or more distinct URLs become ``[N]`` markers plus a
  ``Sources:`` footer (same shape as openai_service._render_citations);
  a lone one becomes the bare URL inline
- other links: ``[text](url)`` → ``text (url)``; ``[url](url)`` → ``url``
- ``**url**`` → bare url (stars break link detection); hosted web_search's
  ``utm_source=openai`` tag dropped

Code spans and fenced blocks pass through untouched. Plain text with no
markdown is returned unchanged. Kill switch: ``BOB_WHATSAPP_MARKDOWN=off``.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


def markdown_conversion_enabled() -> bool:
    return os.getenv("BOB_WHATSAPP_MARKDOWN", "on").strip().lower() not in (
        "0", "false", "no", "off")


# URL allows one level of balanced parens (wikipedia-style paths).
_URL = r"https?://(?:[^\s()]|\([^\s()]*\))+"
_LINK = rf"\[([^\[\]\n]+)\]\(({_URL})\)"
_LINK_RE = re.compile(_LINK)
# A parenthesised group holding only links: "([a](u))", "([a](u), [b](v))".
_CITE_GROUP_RE = re.compile(
    rf"[ \t]?\(\s*{_LINK}(?:\s*[,;]?\s*{_LINK})*\s*\)")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_UNDER_BOLD_RE = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
# OpenAI hosted web_search tags every result URL; pure noise on a phone.
_UTM_OPENAI_RE = re.compile(r"([?&])utm_source=openai(&|(?=[\s)\]]|$))")


def _strip_utm(text: str) -> str:
    def _sub(m: re.Match) -> str:
        sep, tail = m.group(1), m.group(2)
        return sep if tail == "&" else ""
    return _UTM_OPENAI_RE.sub(_sub, text)


def _emphasis(m: re.Match, mark: str) -> str:
    inner = m.group(1)
    # Stars around a bare URL break WhatsApp's link detection.
    return inner if re.fullmatch(_URL, inner) else f"{mark}{inner}{mark}"


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_citation_text(text: str, url: str) -> bool:
    """Link text that only names the source: the URL itself, its domain,
    or a bare domain-ish token."""
    t = text.strip().lower().rstrip("/")
    if t.startswith("www."):
        t = t[4:]
    if t in (url.lower().rstrip("/"), _host(url)):
        return True
    return " " not in t and "." in t and len(t) <= 40


def to_whatsapp(text: str) -> str:
    if not text or not markdown_conversion_enabled():
        return text

    # Shelter code spans/blocks behind placeholders that no rule can match.
    shelter: list[str] = []

    def _hide(m: re.Match) -> str:
        shelter.append(m.group(0))
        return f"\x00{len(shelter) - 1}\x00"

    body = _strip_utm(_CODE_RE.sub(_hide, text))

    # Citations: collect distinct URLs in order of first appearance.
    def _cite_links(chunk: str) -> list[str] | None:
        links = _LINK_RE.findall(chunk)
        if links and all(_is_citation_text(t, u) for t, u in links):
            return [u for _, u in links]
        return None

    groups = [g for m in _CITE_GROUP_RE.finditer(body)
              if (g := _cite_links(m.group(0)))]
    bare = [u for t, u in _LINK_RE.findall(body) if _is_citation_text(t, u)]
    distinct = list(dict.fromkeys([u for g in groups for u in g] + bare))
    numbered = len(distinct) >= 2
    index = {u: i + 1 for i, u in enumerate(distinct)}

    def _render(urls: list[str]) -> str:
        if numbered:
            return "".join(f"[{index[u]}]" for u in urls)
        return " ".join(urls)

    def _group(m: re.Match) -> str:
        urls = _cite_links(m.group(0))
        if urls is None:
            return m.group(0)  # mixed group: links handled below
        lead = " " if m.group(0)[:1] in (" ", "\t") else ""
        return lead + _render(urls)

    body = _CITE_GROUP_RE.sub(_group, body)

    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if _is_citation_text(label, url):
            return _render([url])
        return f"{label} ({url})"

    body = _LINK_RE.sub(_link, body)

    body = _BOLD_RE.sub(lambda m: _emphasis(m, "*"), body)
    body = _UNDER_BOLD_RE.sub(lambda m: _emphasis(m, "_"), body)
    body = _STRIKE_RE.sub(r"~\1~", body)
    body = _HEADING_RE.sub(
        lambda m: f"*{m.group(1).replace('*', '').strip()}*", body)

    if numbered:
        body = body.rstrip() + "\n\nSources:\n" + "\n".join(
            f"[{index[u]}] {u}" for u in distinct)

    return re.sub("\x00(\\d+)\x00", lambda m: shelter[int(m.group(1))], body)
