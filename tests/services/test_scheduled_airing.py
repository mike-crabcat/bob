"""Station-side scheduled airing: a 'ready' feature set with a past
scheduled_for must be selected for airing by the station itself — planned
start times must not depend on an LLM routine turn (2026-08-29: the wakeup
pump stalled 28 min behind a gate-blocked heartbeat task and the 17:00 set
aired late).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def station_mod():
    # the radio skill lives in the workspace, outside the package tree —
    # load station.py by path (registered in sys.modules so its dataclasses
    # can resolve their module)
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "radio_station", Path("/home/bob/workspace/skills/radio/station.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["radio_station"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_set(d: Path, slug: str, *, status: str, scheduled_for) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "set.json").write_text(json.dumps(
        {"slug": slug, "status": status,
         "scheduled_for": scheduled_for}))


def test_due_set_with_past_time_is_selected(station_mod, tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    _write_set(tmp_path / "a", "a", status="ready", scheduled_for=past)
    assert station_mod._due_feature_set(tmp_path) == "a"


def test_future_and_unscheduled_sets_ignored(station_mod, tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _write_set(tmp_path / "a", "a", status="ready", scheduled_for=future)
    _write_set(tmp_path / "b", "b", status="ready", scheduled_for=None)
    _write_set(tmp_path / "c", "c", status="aired",
               scheduled_for="2020-01-01T00:00:00+00:00")
    assert station_mod._due_feature_set(tmp_path) is None


def test_naive_timestamp_reads_as_local(station_mod, tmp_path):
    naive = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    _write_set(tmp_path / "a", "a", status="ready", scheduled_for=naive)
    assert station_mod._due_feature_set(tmp_path) == "a"


def test_unparseable_timestamp_is_ignored(station_mod, tmp_path):
    _write_set(tmp_path / "a", "a", status="ready", scheduled_for="at five")
    assert station_mod._due_feature_set(tmp_path) is None


def test_oldest_due_set_wins(station_mod, tmp_path):
    older = "2026-01-01T00:00:00+00:00"
    newer = "2026-06-01T00:00:00+00:00"
    _write_set(tmp_path / "b", "b", status="ready", scheduled_for=newer)
    _write_set(tmp_path / "a", "a", status="ready", scheduled_for=older)
    assert station_mod._due_feature_set(tmp_path) == "a"
