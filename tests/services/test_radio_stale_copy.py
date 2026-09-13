"""Stale-copy guard (2026-09-13): rendered copy (intro/infills/back/
promo + advert text composed off the ad pack) is stamped with a setlist
hash; a rebuilt setlist can't ship with words naming cut tracks after a
Friday promo + ad re-posted Sunday named three tracks cut Saturday
night. feature_ready refuses, feature_promote refuses on the promo
itself, feature_advertise carries a warning banner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SKILL = Path("/home/bob/workspace/skills/radio")


def _load_domain():
    spec = importlib.util.spec_from_file_location(
        "radio_domain_stale_test", SKILL / "radio_domain.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["radio_domain_stale_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def domain(tmp_path, monkeypatch):
    mod = _load_domain()
    feat = tmp_path / "features"
    run = tmp_path / "run"
    feat.mkdir()
    run.mkdir()
    monkeypatch.setattr(mod, "FEATURES_DIR", feat)
    monkeypatch.setattr(mod, "RUN_DIR", run)
    monkeypatch.setattr(mod, "QUEUE_FILE", run / "queue.json")
    monkeypatch.setattr(mod, "GATE_FILE", run / "feature.json")
    monkeypatch.setattr(mod, "DEFERRED_FILE", run / "deferred.json")
    return mod


@pytest.fixture()
def made_set(domain):
    d = domain.FEATURES_DIR / "zz-test"
    d.mkdir()
    for f in ("intro.mp3", "back.mp3", "promo.mp3", "art.png"):
        (d / f).write_bytes(b"x")
    m = {"slug": "zz-test", "name": "Test Set", "description": "",
         "status": "draft", "created_at": 0, "updated_at": 0,
         "scheduled_for": None, "aired_at": None,
         "tracks": [
             {"artist": "AC/DC", "title": "Thunderstruck",
              "path": "library/AC_DC - Thunderstruck.mp3"},
             {"artist": "8 Bit Universe",
              "title": "Star vs The Forces of Evil Theme",
              "path": "library/8 Bit Universe - Star vs The Forces of "
                      "Evil Theme (8 Bit Version).mp3"}],
         "scripts": {"intro": {"audio": "intro.mp3"},
                     "back": {"audio": "back.mp3"},
                     "promo": {"audio": "promo.mp3"}},
         "art": {"path": "art.png"}}
    (d / "set.json").write_text(json.dumps(m))
    return m


def test_legacy_scripts_refuse_ready(domain, made_set):
    rc, out = domain.feature_ready("zz-test")
    assert rc == 1
    assert "stale copy" in out
    assert "intro" in out and "promo" in out


def test_restamped_scripts_pass_ready(domain, made_set):
    cur = domain._setlist_hash(made_set)
    for s in made_set["scripts"].values():
        s["setlist_hash"] = cur
    ((domain.FEATURES_DIR / "zz-test" / "set.json")
     .write_text(json.dumps(made_set)))
    rc, out = domain.feature_ready("zz-test")
    assert rc == 0
    assert "READY" in out


def test_promote_refuses_stale_promo_without_queue_write(domain, made_set):
    made_set["tracks"].pop(0)  # rebuild the setlist — copy goes stale
    domain._feat_save(made_set)
    rc, out = domain.feature_promote("zz-test")
    assert rc == 1
    assert "STALE" in out
    assert not (domain.RUN_DIR / "queue.json").exists()


def test_advertise_carries_stale_banner(domain, made_set):
    made_set["tracks"].pop(0)
    domain._feat_save(made_set)
    rc, out = domain.feature_advertise("zz-test")
    assert rc == 0
    assert out.splitlines()[0].startswith("⚠ STALE COPY")


def test_status_detail_marks_stale(domain, made_set):
    made_set["tracks"].pop(0)
    domain._feat_save(made_set)
    rc, out = domain.feature_status("zz-test")
    assert rc == 0
    assert "STALE COPY" in out


def test_feat_save_derives_setlist_hash(domain, made_set):
    made_set["tracks"].pop(0)
    domain._feat_save(made_set)
    on_disk = json.loads(
        (domain.FEATURES_DIR / "zz-test" / "set.json").read_text())
    assert on_disk["setlist_hash"] == domain._setlist_hash(on_disk)


def _stamped(domain, made_set):
    cur = domain._setlist_hash(made_set)
    for s in made_set["scripts"].values():
        s["setlist_hash"] = cur
    made_set["status"] = "aired"
    made_set["aired_at"] = 1.0
    return made_set


def test_bare_ready_clears_stale_past_schedule(domain, made_set):
    # 2026-09-13 incident: 'feature ready <slug>' (no --at) flipped an
    # aired set back to ready leaving its PAST scheduled_for armed — the
    # station re-aired the set 33 min after it completed (reached via a
    # 'feature ready <slug> --help' that executed instead of helping).
    _stamped(domain, made_set)
    made_set["scheduled_for"] = "2026-09-13T08:00:00+08:00"
    domain._feat_save(made_set)
    rc, out = domain.feature_ready("zz-test")
    assert rc == 0
    after = json.loads(
        (domain.FEATURES_DIR / "zz-test" / "set.json").read_text())
    assert after["status"] == "ready"
    assert after.get("scheduled_for") is None
    assert "CLEARED" in out


def test_bare_ready_keeps_future_schedule(domain, made_set):
    _stamped(domain, made_set)
    made_set["scheduled_for"] = "2026-09-13T23:00:00+08:00"
    domain._feat_save(made_set)
    rc, out = domain.feature_ready("zz-test")
    assert rc == 0
    after = json.loads(
        (domain.FEATURES_DIR / "zz-test" / "set.json").read_text())
    assert after["scheduled_for"] == "2026-09-13T23:00:00+08:00"
