"""bg_jobs repository — SQL ownership, lifecycle transitions, and the
running-name uniqueness that bg_start relies on."""

from __future__ import annotations

import pytest

from server.repositories.bg_jobs import BgJobsRepository

from server.services.base import utcnow


def _now() -> str:
    return utcnow().isoformat()


async def _create(repo: BgJobsRepository, name: str = "radio", **overrides):
    kwargs = dict(
        name=name,
        command="python3 skills/radio/station.py",
        unit=f"bg-{name}.service",
        mechanism="systemd",
        pid=123,
        pid_start_time=None,
        description="",
        source="bg_start",
        wake_on_exit=False,
        parent_session_key="",
        log=f".bg/logs/{name}.log",
        now_iso=_now(),
    )
    kwargs.update(overrides)
    return await repo.create(**kwargs)


@pytest.mark.asyncio
async def test_create_and_read_running(db):
    repo = BgJobsRepository(db)
    job_id = await _create(repo, source="run_bg_process", wake_on_exit=True,
                           parent_session_key="agent:main:whatsapp:dm:1")
    assert job_id > 0
    row = await repo.get_running_by_name("radio")
    assert row["id"] == job_id
    assert row["status"] == "running"
    assert row["wake_on_exit"] == 1
    assert row["source"] == "run_bg_process"
    assert row["delivery"] == ""


@pytest.mark.asyncio
async def test_running_rows_and_count(db):
    repo = BgJobsRepository(db)
    await _create(repo, "a")
    await _create(repo, "b")
    assert len(await repo.running_rows()) == 2
    assert await repo.count_running() == 2


@pytest.mark.asyncio
async def test_terminal_transitions(db):
    repo = BgJobsRepository(db)
    job_id = await _create(repo)
    await repo.mark_terminal(job_id, status="failed", exit_code=3,
                             systemd_result="exited", now_iso=_now())
    row = await repo.get(job_id)
    assert row["status"] == "failed"
    assert row["exit_code"] == 3
    assert row["systemd_result"] == "exited"
    assert row["ended_at"]
    # terminal rows leave the running set
    assert await repo.get_running_by_name("radio") is None
    assert await repo.count_running() == 0


@pytest.mark.asyncio
async def test_replaced_only_from_running(db):
    repo = BgJobsRepository(db)
    job_id = await _create(repo)
    await repo.mark_terminal(job_id, status="exited", exit_code=0,
                             systemd_result="success", now_iso=_now())
    # a terminal row must not be 'replaced' by a later restart
    await repo.mark_replaced(job_id, _now())
    assert (await repo.get(job_id))["status"] == "exited"


@pytest.mark.asyncio
async def test_delivery_ledger(db):
    repo = BgJobsRepository(db)
    job_id = await _create(repo, wake_on_exit=True, parent_session_key="s")
    await repo.set_delivery(job_id, "pending", _now())
    assert len(await repo.pending_deliveries()) == 1
    await repo.set_delivery(job_id, "delivered", _now())
    assert await repo.pending_deliveries() == []
    # non-wake jobs never appear in the ledger even if mis-stamped
    other = await _create(repo, "plain")
    await repo.set_delivery(other, "pending", _now())
    assert await repo.pending_deliveries() == []


@pytest.mark.asyncio
async def test_one_running_row_per_name(db):
    repo = BgJobsRepository(db)
    await _create(repo, "radio")
    with pytest.raises(Exception):
        # partial unique index rejects a second running row for the name
        await _create(repo, "radio")
    # after terminal, the name is startable again — old row kept as history
    old_id = (await repo.get_running_by_name("radio"))["id"]
    await repo.mark_terminal(old_id, status="exited", exit_code=0,
                             systemd_result="success", now_iso=_now())
    new_id = await _create(repo, "radio")
    assert new_id != old_id
    rows = await repo.list(limit=10)
    assert [r["status"] for r in rows].count("running") == 1


@pytest.mark.asyncio
async def test_list_filters_by_status(db):
    repo = BgJobsRepository(db)
    a = await _create(repo, "a")
    await _create(repo, "b")
    await repo.mark_terminal(a, status="killed", exit_code=None,
                             systemd_result=None, now_iso=_now())
    running_names = {r["name"] for r in await repo.list(status="running")}
    assert running_names == {"b"}
