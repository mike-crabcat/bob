"""Radio control API (the single-owner refactor, 2026-08-30): auth,
hardening caps, confirm gates, and the fetch-queue single-writer logic.
station.py / radio_domain.py live in the workspace skill, loaded by path.
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
    return _load("radio_station_test", SKILL / "station.py")


@pytest.fixture(scope="module")
def domain_mod(station_mod):
    return sys.modules["radio_domain"]


class FakeStation:
    def __init__(self):
        self.source = "queue"

    def _status_dict(self):
        return {"now_playing": None, "source": self.source,
                "listeners": [], "station": "test"}

    def _render_now(self):
        return "ON AIR: (queue)\nqueue: 0 item(s)"


@pytest.fixture
def api(station_mod):
    return station_mod.ControlAPI(FakeStation(), "test-token")


def _dispatch(api, method, path, body=None, authorized=True):
    return asyncio.run(api.dispatch(method, path, body or {}, authorized))


def test_unauthorized_is_401(api):
    status, payload = _dispatch(api, "POST", "/skip", authorized=False)
    assert status == 401 and payload["ok"] is False
    assert "token" in payload["text"].lower()


def test_confirm_gates_on_destructive_ops(api, tmp_path, monkeypatch,
                                          domain_mod):
    # queue-clear and feature air refuse without confirm
    s, p = _dispatch(api, "POST", "/queue-clear", {"confirm": False})
    assert s == 400 and "confirm" in p["text"]
    s, p = _dispatch(api, "POST", "/feature",
                     {"sub": "air", "slug": "x", "confirm": False})
    assert s == 400 and "confirm" in p["text"]


def test_announce_length_cap(api):
    s, p = _dispatch(api, "POST", "/announce", {"text": "x" * 3000})
    assert s == 400 and "too long" in p["text"]


def test_announce_rate_limit(api, monkeypatch, domain_mod):
    monkeypatch.setattr(domain_mod, "queue_announcement",
                        lambda text, **k: (0, "queued"))
    for _ in range(api.ANNOUNCE_RATE_N):
        s, _p = _dispatch(api, "POST", "/announce", {"text": "hi"})
        assert s == 200
    s, p = _dispatch(api, "POST", "/announce", {"text": "hi"})
    assert s == 429 and "rate limit" in p["text"]


def test_fetch_rejects_non_spotify_uris(api):
    s, p = _dispatch(api, "POST", "/fetch", {"uris": ["-s", "search query"]})
    assert s == 400 and "spotify:" in p["text"]


def test_unknown_endpoint_404(api):
    s, _p = _dispatch(api, "GET", "/definitely-not")
    assert s == 404


def test_token_file_is_regenerated_not_static(station_mod, tmp_path,
                                              monkeypatch):
    """The token must be generated per start (secrets), not a fixed value.
    Writes go to a scratch file — the real run/api_token belongs to the
    LIVE station; rotating it from a test breaks the control API until the
    station restarts."""
    monkeypatch.setattr(station_mod, "API_TOKEN_FILE", tmp_path / "api_token")
    monkeypatch.setattr(station_mod, "RUN_DIR", tmp_path)
    tok = station_mod._new_api_token()
    tok2 = station_mod._new_api_token()
    assert tok != tok2 and len(tok) >= 32
    f = station_mod.API_TOKEN_FILE
    assert f.read_text().strip() == tok2
    assert (f.stat().st_mode & 0o777) == 0o600


# ---------------- fetch queue: single-writer logic ---------------- #

@pytest.fixture
def jobfile(domain_mod, tmp_path, monkeypatch):
    jf = tmp_path / "fetch_jobs.json"
    monkeypatch.setattr(domain_mod, "FETCH_JOBS_FILE", jf)
    return jf


def test_enqueue_dedup_and_positions(domain_mod, jobfile):
    r1 = domain_mod.fetch_enqueue(["spotify:track:a"], kind="request")
    r2 = domain_mod.fetch_enqueue(["spotify:track:b", "spotify:track:c"])
    r3 = domain_mod.fetch_enqueue(["spotify:track:a"])
    assert r1["position"] == 1 and r2["position"] == 2
    assert r3["dup_of"] == r1["job"]["id"]


def test_partial_dedup_enqueues_only_fresh(domain_mod, jobfile):
    domain_mod.fetch_enqueue(["spotify:track:a"])
    r = domain_mod.fetch_enqueue(["spotify:track:a", "spotify:track:z"])
    assert "job" in r and r["job"]["uris"] == ["spotify:track:z"]


def test_batch_cap_and_claiming(domain_mod, jobfile):
    for i in range(30):
        domain_mod.fetch_enqueue([f"spotify:track:{i}"])
    batch = domain_mod.fetch_take_batch()
    assert batch is not None
    assert sum(len(j["uris"]) for j in batch) <= domain_mod.FETCH_BATCH_CAP
    assert all(j["status"] == "running" for j in batch)


def test_requeue_stale_running(domain_mod, jobfile):
    domain_mod.fetch_enqueue(["spotify:track:a"])
    batch = domain_mod.fetch_take_batch()
    assert batch and batch[0]["status"] == "running"
    n = domain_mod.fetch_requeue_stale()
    assert n == 1
    jobs = json.loads(jobfile.read_text())
    assert jobs[0]["status"] == "queued"


def test_empty_queue_returns_none(domain_mod, jobfile):
    assert domain_mod.fetch_take_batch() is None


def test_fetch_batch_runs_zotify_and_stamps(domain_mod, jobfile,
                                            monkeypatch, tmp_path):
    calls = []

    def fake_zotify(uris):
        calls.append(list(uris))
        return True, [{"type": "song", "path": "x.mp3",
                       "title": "T", "artist": "A", "origin": "fetch"}]

    monkeypatch.setattr(domain_mod, "_zotify_run", fake_zotify)
    monkeypatch.setattr(domain_mod, "append_queue", lambda items: 5)
    monkeypatch.setattr(domain_mod, "_attach_pending_dedications",
                        lambda items, fetched_uris=None: items)
    monkeypatch.setattr(domain_mod, "_prune_library", lambda: None)
    r = domain_mod.fetch_enqueue(["spotify:track:a", "spotify:track:b"])
    batch = domain_mod.fetch_take_batch()
    ok, queued = domain_mod.fetch_run_batch(batch)
    assert ok and queued == 1
    assert calls == [["spotify:track:a", "spotify:track:b"]]  # ONE zotify run
    jobs = json.loads(jobfile.read_text())
    assert jobs[0]["status"] == "done" and jobs[0]["batch_tracks"] == 1


# ---------------- embargo lane: feature fetches debut on air ---------------- #

@pytest.fixture
def embargo_dirs(domain_mod, tmp_path, monkeypatch):
    lib, emb, feats = (tmp_path / d for d in
                       ("library", "library_embargo", "features"))
    for d in (lib, emb, feats):
        d.mkdir()
    monkeypatch.setattr(domain_mod, "LIBRARY_DIR", lib)
    monkeypatch.setattr(domain_mod, "EMBARGO_DIR", emb)
    monkeypatch.setattr(domain_mod, "FEATURES_DIR", feats)
    return lib, emb, feats


def _make_set(feats: Path, slug: str, status: str = "draft",
              tracks: list | None = None) -> dict:
    d = feats / slug
    d.mkdir(parents=True, exist_ok=True)
    m = {"slug": slug, "name": slug, "status": status,
         "tracks": tracks or [], "scripts": {}, "created_at": time.time()}
    (d / "set.json").write_text(json.dumps(m))
    return m


def test_feature_fetch_embargoes_instead_of_queueing(
        domain_mod, jobfile, embargo_dirs, monkeypatch, tmp_path):
    # a landed --for-set track must reach neither the queue nor rotation
    lib, emb, feats = embargo_dirs
    _make_set(feats, "seansrave")
    landed = tmp_path / "Daft Punk - Alive.mp3"
    landed.write_bytes(b"x")
    monkeypatch.setattr(domain_mod, "_zotify_run", lambda uris: (True, [
        {"type": "song", "path": str(landed), "title": "Alive",
         "artist": "Daft Punk", "origin": "fetch"}]))
    queued: list = []
    monkeypatch.setattr(domain_mod, "append_queue",
                        lambda items: queued.append(items) or 5)
    monkeypatch.setattr(domain_mod, "_attach_pending_dedications",
                        lambda items, fetched_uris=None: items)
    domain_mod.fetch_enqueue(["spotify:track:a"], kind="feature",
                             note="seansrave")
    ok, n = domain_mod.fetch_run_batch(domain_mod.fetch_take_batch())
    assert ok and n == 1
    assert not queued                        # live queue untouched
    assert not list(lib.rglob("*.mp3"))      # rotation can't see it
    assert list((emb / "seansrave").glob("*.mp3"))  # parked in embargo


def test_embargo_falls_back_when_set_is_dead(
        domain_mod, jobfile, embargo_dirs, monkeypatch, tmp_path):
    # cancelled/airless sets must not strand downloads in limbo
    _make_set(embargo_dirs[2], "dead", status="cancelled")
    landed = tmp_path / "A - B.mp3"
    landed.write_bytes(b"x")
    monkeypatch.setattr(domain_mod, "_zotify_run", lambda uris: (True, [
        {"type": "song", "path": str(landed), "title": "B",
         "artist": "A", "origin": "fetch"}]))
    queued: list = []
    monkeypatch.setattr(domain_mod, "append_queue",
                        lambda items: queued.append(items) or 5)
    monkeypatch.setattr(domain_mod, "_attach_pending_dedications",
                        lambda items, fetched_uris=None: items)
    monkeypatch.setattr(domain_mod, "_prune_library", lambda: None)
    domain_mod.fetch_enqueue(["spotify:track:a"], kind="feature", note="dead")
    ok, n = domain_mod.fetch_run_batch(domain_mod.fetch_take_batch())
    assert ok and n == 1 and queued           # queued plainly instead
    assert not list(embargo_dirs[1].rglob("*.mp3"))


def test_embargo_batches_never_mix(domain_mod, jobfile, embargo_dirs):
    # landed files attribute per batch, so a feature job can't share a
    # zotify run with a plain one
    _make_set(embargo_dirs[2], "s1")
    domain_mod.fetch_enqueue(["spotify:track:a"], kind="feature", note="s1")
    domain_mod.fetch_enqueue(["spotify:track:b"], kind="fetch")
    b1 = domain_mod.fetch_take_batch()
    b2 = domain_mod.fetch_take_batch()
    assert len(b1) == 1 and len(b2) == 1
    assert {b1[0]["kind"], b2[0]["kind"]} == {"feature", "fetch"}


def test_embargo_release_moves_and_repoints(domain_mod, embargo_dirs):
    lib, emb, feats = embargo_dirs
    held = emb / "rastahour"
    held.mkdir(parents=True)
    (held / "Bob Marley - Exodus.mp3").write_bytes(b"x")
    m = _make_set(feats, "rastahour", tracks=[
        {"artist": "Bob Marley", "title": "Exodus",
         "path": "library_embargo/rastahour/Bob Marley - Exodus.mp3"}])
    m, n = domain_mod._embargo_release("rastahour", m)
    assert n == 1
    assert (lib / "Bob Marley - Exodus.mp3").is_file()
    assert not held.exists()
    assert m["tracks"][0]["path"] == "library/Bob Marley - Exodus.mp3"


def test_library_match_sees_embargo_lane(domain_mod, embargo_dirs):
    lib, emb, _ = embargo_dirs
    (lib / "A - Rotate.mp3").write_bytes(b"x")
    e = emb / "s1"
    e.mkdir()
    (e / "B - Debut.mp3").write_bytes(b"x")
    assert domain_mod._library_match("B", "Debut") == e / "B - Debut.mp3"
    assert domain_mod._library_match("A", "Rotate") == lib / "A - Rotate.mp3"
    assert domain_mod._library_match("C", "Nope") is None


def test_fetch_endpoint_carries_for_set(api, domain_mod, jobfile,
                                        embargo_dirs):
    _make_set(embargo_dirs[2], "seansrave")
    s, p = _dispatch(api, "POST", "/fetch",
                     {"uris": ["spotify:track:a"], "for_set": "seansrave"})
    assert s == 200 and "embargo" in p["text"]
    jobs = json.loads(jobfile.read_text())
    assert jobs[0]["kind"] == "feature" and jobs[0]["note"] == "seansrave"
    # a slug with no set bails honestly instead of embargoing into a void
    s, p = _dispatch(api, "POST", "/fetch",
                     {"uris": ["spotify:track:a"], "for_set": "ghost"})
    assert s == 422 and "no such feature set" in p["text"]
