"""Radio resolve routing (2026-09-25): the Spotify Web API is the PRIMARY
resolve route; the legacy MusicBrainz→embed chain only runs when the API
route is unavailable. The big-beat-bangers lesson: MB carries almost no
track links (0/24) and the embed scrape 403s — niche tracks must not depend
on it when a key exists. All routes mocked; no network in these tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SKILL = Path("/home/bob/workspace/skills/radio")


def _load_domain():
    name = "radio_domain_resolve_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SKILL / "radio_domain.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def domain():
    return _load_domain()


def test_api_hit_short_circuits_legacy(domain, monkeypatch):
    monkeypatch.setattr(
        domain, "_spotify_api_search",
        lambda q: {"uri": "spotify:track:api", "artist": "X", "title": "T"})
    def _boom(artist):
        raise AssertionError("legacy chain must not run when the API resolves")
    monkeypatch.setattr(domain, "_artist_top_tracks", _boom)
    assert domain._resolve_request("X - T", best=False) == {
        "uri": "spotify:track:api", "artist": "X", "title": "T"}


def test_fallback_when_api_unavailable(domain, monkeypatch):
    monkeypatch.setattr(domain, "_spotify_api_search", lambda q: None)
    monkeypatch.setattr(
        domain, "_artist_top_tracks",
        lambda artist: [("spotify:track:legacy", "T")])
    out = domain._resolve_request("X - T", best=False)
    assert out == {"uri": "spotify:track:legacy", "artist": "X", "title": "T"}


def test_creds_file_parse(domain, monkeypatch, tmp_path):
    key = tmp_path / "spotify_api_key"
    key.write_text("# comment\nclient_id=abc\nclient_secret=def\n")
    monkeypatch.setattr(domain, "_SPOTIFY_KEY_FILE", key)
    assert domain._spotify_creds() == ("abc", "def")
    monkeypatch.setattr(domain, "_SPOTIFY_KEY_FILE", tmp_path / "missing")
    assert domain._spotify_creds() is None
