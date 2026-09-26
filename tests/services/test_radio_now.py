"""`radio.py now` extras (2026-09-26): on-air questions must be answerable
by ONE call. The grand-final morning turns port-probed and grep-ed the
workspace for state the station already owns — now_extras() folds the
feature gate + booked sets into `now`, rendered client-side (works even
with the station down)."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

SKILL = Path("/home/bob/workspace/skills/radio")


def _load_domain():
    name = "radio_domain_now_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SKILL / "radio_domain.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _seed(mod, tmp_path, *, gate=None, bookings=()):
    """Point the module's dirs at a tmp skill tree. GATE_FILE is a
    precomputed module constant (not derived from RUN_DIR at call time),
    so it must be reassigned too."""
    feats = tmp_path / "features"
    feats.mkdir(parents=True)
    for slug, sched in bookings:
        d = feats / slug
        d.mkdir()
        (d / "set.json").write_text(json.dumps(
            {"slug": slug, "status": "ready", "scheduled_for": sched}))
    run = tmp_path / "run"
    run.mkdir()
    mod.FEATURES_DIR = feats
    mod.RUN_DIR = run
    mod.GATE_FILE = run / "feature.json"
    if gate:
        mod.GATE_FILE.write_text(json.dumps(gate))


def test_gate_and_bookings_render(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    mod = _load_domain()
    soon = (datetime.now().astimezone()
            + timedelta(hours=1)).isoformat()
    _seed(mod, tmp_path,
          gate={"slug": "nuffy-talkback", "created_at": time.time() - 600},
          bookings=[("dockers-gf-pregame", soon)])
    lines = mod.now_extras()
    assert any("feature on air: nuffy-talkback" in l for l in lines)
    assert any("upcoming sets: dockers-gf-pregame airs" in l for l in lines)


def test_no_gate_no_bookings_is_quiet(monkeypatch, tmp_path):
    mod = _load_domain()
    _seed(mod, tmp_path)
    assert mod.now_extras() == []


def test_overdue_booking_flags(monkeypatch, tmp_path):
    mod = _load_domain()
    past = "2020-01-01T08:00:00+08:00"
    _seed(mod, tmp_path, bookings=[("stale-set", past)])
    lines = mod.now_extras()
    assert any("OVERDUE" in l and "stale-set" in l for l in lines)
