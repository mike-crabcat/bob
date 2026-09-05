"""Radio 2.0 rotation programming (2026-09-03): block rotation off
library_tags.json + dayparts.json — daypart bucket choice, adjacency,
night rules (the generalised Blair decree), recency guard with
relaxation, sweeper injection, block persistence. The builder is pure
(items/tags/cfg in, block out) so these are all direct-function tests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

SKILL = Path("/home/bob/workspace/skills/radio")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def station_mod():
    return _load("radio_station_rot_test", SKILL / "station.py")


@pytest.fixture(scope="module")
def domain_mod(station_mod):
    return sys.modules["radio_domain"]


def _song(artist: str, title: str = "T") -> dict:
    return {"type": "song", "path": f"library/{artist} - {title}.mp3",
            "artist": artist, "title": title, "origin": "library"}


TAGS = {"artists": {
    a: {"genre": g, "energy": e}
    for a, g, e in [
        ("TechnoMan", "techno", 8), ("TechnoWoman", "techno", 7),
        ("TranceAct", "trance", 7), ("HouseBand", "house", 6),
        ("ChillDuo", "downtempo", 3), ("AmbientOne", "ambient", 2),
        ("RockBand", "rock", 7), ("RockSolo", "rock", 5),
        ("OldAct", "classics", 4), ("NightAct", "downtempo", 3),
    ]
}}


def _tagged_items():
    return [_song(a) for a in TAGS["artists"]]


# ---------------- tag lookup ---------------- #

def test_tag_for_exact_primary_and_casefold(domain_mod):
    tags = {"artists": {"Fred again..": {"genre": "house", "energy": 7}}}
    assert domain_mod.tag_for("Fred again..", tags)["genre"] == "house"
    # comma-primary: featured artists resolve to the headliner's tag
    assert domain_mod.tag_for("Fred again.., Skrillex", tags)["genre"] \
        == "house"
    # case/whitespace insensitive
    assert domain_mod.tag_for("  fred   AGAIN.. ", tags)["genre"] == "house"
    assert domain_mod.tag_for("Nobody", tags) is None


def test_live_tags_file_is_wellformed(domain_mod):
    tags = domain_mod.load_tags()
    artists = tags.get("artists", {})
    assert artists, "library_tags.json shipped empty/corrupt"
    for name, t in artists.items():
        assert t.get("genre"), f"{name} has no genre"
        assert isinstance(t.get("energy"), int) and 1 <= t["energy"] <= 10, \
            f"{name} energy out of range"


# ---------------- dayparts ---------------- #

def test_dayparts_cover_24h_and_mirror_adjacency(domain_mod, tmp_path,
                                                 monkeypatch):
    doc = domain_mod.load_dayparts()
    hours = [h for dp in doc["dayparts"] for h in dp["hours"]]
    assert sorted(hours) == list(range(24))
    # mirrored: a→b implies b→a
    for a, nbrs in doc["adjacency"].items():
        for b in nbrs:
            assert a in doc["adjacency"].get(b, [])
    assert domain_mod.daypart_for_hour(doc, 16)["name"]


def test_invalid_dayparts_file_falls_back(domain_mod, tmp_path,
                                          monkeypatch):
    bad = {"dayparts": [{"name": "all", "hours": [1, 2, 3],
                         "buckets": {"rock": 1}}]}   # 21 hours missing
    f = tmp_path / "dayparts.json"
    f.write_text(json.dumps(bad))
    monkeypatch.setattr(domain_mod, "DAYPARTS_FILE", f)
    doc = domain_mod.load_dayparts()
    names = [dp["name"] for dp in doc["dayparts"]]
    assert "night" in names and "arvo-drive" in names  # built-in defaults


# ---------------- night rules (the Blair decree, generalised) ---------------- #

def test_night_only_exclusive_window(station_mod):
    tags = {"artists": {"NightAct": {"genre": "downtempo", "energy": 3,
                                     "night_only": True},
                        "RockBand": {"genre": "rock", "energy": 7}}}
    items = [_song("NightAct"), _song("RockBand")]
    dpcfg = station_mod.load_dayparts()
    # 03:00 with a night_only act alive: it owns the window exclusively
    b = station_mod._build_block(items, tags, dpcfg, 3, None,
                                 recent=set(), last_path=None)
    assert b["bucket"] == "night-exclusive"
    assert all(it["artist"] == "NightAct" for it in b["items"])
    # midday: night_only excluded
    b = station_mod._build_block(items, tags, dpcfg, 12, None,
                                 recent=set(), last_path=None)
    assert all(it["artist"] != "NightAct" for it in b["items"])
    # 22:00-24:00: both allowed at the rule level (a block is single-bucket,
    # so the daypart — not the rule — decides what actually airs)
    aa = station_mod._allowed_at_hour
    night_tag = tags["artists"]["NightAct"]
    rock_tag = tags["artists"]["RockBand"]
    assert aa(night_tag, 23, True) and aa(rock_tag, 23, True)


def test_no_night_only_acts_degrades_instead_of_silence(station_mod):
    """The decree window with zero night_only tracks must not go silent
    (NoW was LRU-pruned; dead air would be a regression)."""
    dpcfg = station_mod.load_dayparts()
    b = station_mod._build_block(_tagged_items(), TAGS, dpcfg, 3, None,
                                 recent=set(), last_path=None)
    assert b and b["items"]
    assert b["daypart"] == "night"


# ---------------- block construction ---------------- #

def test_block_bucket_from_daypart(station_mod):
    dpcfg = station_mod.load_dayparts()
    for hour in range(24):
        b = station_mod._build_block(_tagged_items(), TAGS, dpcfg, hour,
                                     None, recent=set(), last_path=None)
        allowed = set(domain_bucket_names(station_mod)) | {"eclectic"}
        assert b and b["bucket"] in allowed, f"hour {hour}: {b['bucket']}"


def domain_bucket_names(station_mod):
    import radio_domain  # noqa: F401 — via sys.modules from station import
    dpcfg = station_mod.load_dayparts()
    names = set()
    for dp in dpcfg["dayparts"]:
        names |= set(dp["buckets"])
    return names


def test_block_energy_ramp_and_distinct_artists(station_mod):
    dpcfg = station_mod.load_dayparts()
    # a bucket with 2 artists × 3 tracks each: one artist per slot
    tags = {"artists": {
        f"Rock{i}": {"genre": "rock", "energy": e}
        for i, e in enumerate([7, 5, 6, 8, 4, 6])}}
    items = [_song(a, f"T{n}") for a in tags["artists"] for n in (1, 2, 3)]
    b = station_mod._build_block(items, tags, dpcfg, 19, None,
                                 recent=set(), last_path=None)
    energies = [tags["artists"][it["artist"]]["energy"] for it in b["items"]]
    assert energies == sorted(energies), "block must ramp energy upward"
    assert len({it["artist"] for it in b["items"]}) == len(b["items"])


def test_recency_guard_and_relaxation(station_mod):
    dpcfg = station_mod.load_dayparts()
    items = _tagged_items()
    # everything recent → still builds (relaxed), never silent
    all_recent = {station_mod.item_label(it) for it in items}
    b = station_mod._build_block(items, TAGS, dpcfg, 16, None,
                                 recent=all_recent, last_path=None)
    assert b and b["items"]
    # one recent track → must not appear
    victim = items[0]
    b = station_mod._build_block(items, TAGS, dpcfg, 16, None,
                                 recent={station_mod.item_label(victim)},
                                 last_path=None)
    assert all(it["path"] != victim["path"] for it in b["items"])


def test_sweeper_only_on_bucket_change(station_mod, tmp_path):
    dpcfg = station_mod.load_dayparts()
    sw = tmp_path / "sweeper-1.mp3"
    sw.write_bytes(b"x")
    prev_same = "rock"
    b = station_mod._build_block(_tagged_items(), TAGS, dpcfg, 19,
                                 prev_bucket=prev_same, recent=set(),
                                 last_path=None, sweeper_files=[sw])
    if b["bucket"] == prev_same:
        assert b["items"][0].get("type") != "sweeper"
    else:
        assert b["items"][0].get("type") == "sweeper"
        assert b["items"][0]["path"] == str(sw)
    # no sweeper files → no sweeper, whatever the change
    b = station_mod._build_block(_tagged_items(), TAGS, dpcfg, 19,
                                 prev_bucket="trance", recent=set(),
                                 last_path=None, sweeper_files=[])
    assert b["items"][0].get("type") != "sweeper"


def test_empty_library_returns_none(station_mod):
    b = station_mod._build_block([], TAGS, station_mod.load_dayparts(), 12,
                                 None, recent=set(), last_path=None)
    assert b is None


# ---------------- persistence + resume ---------------- #

def test_block_save_load_roundtrip(station_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(station_mod, "BLOCK_FILE",
                        tmp_path / "block.json")
    block = {"bucket": "rock", "daypart": "evening",
             "built_at": time.time(),
             "items": [_song("RockBand"), _song("RockSolo")]}
    station_mod._save_block(block)
    loaded = station_mod._load_block()
    assert loaded == block


def test_consume_rotation_pops_and_persists(station_mod, tmp_path,
                                            monkeypatch, domain_mod):
    monkeypatch.setattr(station_mod, "BLOCK_FILE",
                        tmp_path / "block.json")
    cfg = dict(domain_mod.DEFAULTS)
    cfg["ffmpeg"] = None
    st = station_mod.Station(cfg, mode="queue")
    st._block = {"bucket": "rock", "daypart": "evening",
                 "built_at": time.time(),
                 "items": [_song("RockBand"), _song("RockSolo")]}
    st._consume_rotation(st._block["items"][0])
    assert len(st._block["items"]) == 1
    assert station_mod._load_block()["items"][0]["artist"] == "RockSolo"
    # consuming something that isn't head is a no-op
    st._consume_rotation({"path": "library/other.mp3"})
    assert len(st._block["items"]) == 1


def test_recent_labels_reads_history(station_mod, tmp_path):
    hf = tmp_path / "history.jsonl"
    now = time.time()
    rows = [
        {"ts": now - 60, "type": "song", "title": "A — recent"},
        {"ts": now - 60, "type": "sweeper", "title": "527 ID"},
        {"ts": now - 3 * 3600, "type": "song", "title": "B — old"},
        {"ts": now - 60, "type": "announcement", "title": "news"},
    ]
    hf.write_text("\n".join(json.dumps(r) for r in rows))
    labels = station_mod._recent_labels(1.0, history_file=hf)
    assert labels == {"A — recent"}
    assert station_mod._recent_labels(4.0, history_file=hf) \
        == {"A — recent", "B — old"}


def test_tags_report_counts(domain_mod, tmp_path, monkeypatch):
    lib = tmp_path / "library"
    lib.mkdir()
    for name in ("Tagged Act - X.mp3", "Tagged Act - Y.mp3",
                 "Mystery Act - Z.mp3"):
        (lib / name).write_bytes(b"x")
    monkeypatch.setattr(domain_mod, "LIBRARY_DIR", lib)
    monkeypatch.setattr(domain_mod, "TAGS_FILE", tmp_path / "nope.json")
    # no tags file → everything untagged, report still works
    rep = domain_mod.tags_report()
    assert rep["tracks"] == 3 and rep["tagged_tracks"] == 0
    assert dict(rep["missing"])["Tagged Act"] == 2
