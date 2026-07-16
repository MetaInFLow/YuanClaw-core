import asyncio
import json
import os

import pytest

from yuanclaw.cron.service import CronService, CronStoreCorruptError
from yuanclaw.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"


@pytest.mark.asyncio
async def test_running_service_honors_external_disable(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    called: list[str] = []

    async def on_job(job) -> None:
        called.append(job.id)

    service = CronService(store_path, on_job=on_job)
    job = service.add_job(
        name="external-disable",
        schedule=CronSchedule(kind="every", every_ms=200),
        message="hello",
    )
    await service.start()
    try:
        # Wait slightly to ensure file mtime is definitively different
        await asyncio.sleep(0.05)
        external = CronService(store_path)
        updated = external.enable_job(job.id, enabled=False)
        assert updated is not None
        assert updated.enabled is False

        await asyncio.sleep(0.35)
        assert called == []
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_run_history_is_recorded_and_persisted(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    calls: list[str] = []

    async def on_job(job) -> None:
        calls.append(job.id)

    service = CronService(store_path, on_job=on_job)
    job = service.add_job(
        name="history",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="hello",
    )

    for _ in range(25):
        assert await service.run_job(job.id, force=True) is True

    live_job = service.list_jobs(include_disabled=True)[0]
    assert live_job is not None
    assert len(live_job.state.run_history) == 20
    assert all(record.status == "ok" for record in live_job.state.run_history)
    assert all(record.duration_ms >= 0 for record in live_job.state.run_history)
    assert calls == [job.id] * 25

    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(persisted["jobs"][0]["state"]["runHistory"]) == 20

    reloaded = CronService(store_path)
    reloaded_job = reloaded.list_jobs(include_disabled=True)[0]
    assert reloaded_job is not None
    assert len(reloaded_job.state.run_history) == 20
    assert reloaded_job.state.run_history[-1].status == "ok"


def test_add_job_rejects_invalid_cron_expression(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="does not produce a future run time"):
        service.add_job(
            name="invalid",
            schedule=CronSchedule(kind="cron", expr="not a cron"),
            message="hello",
        )


@pytest.mark.asyncio
async def test_corrupt_store_without_backup_is_not_overwritten(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{broken", encoding="utf-8")
    service = CronService(store_path)

    with pytest.raises(CronStoreCorruptError, match="Invalid cron store"):
        await service.start()

    assert store_path.read_text(encoding="utf-8") == "{broken"


def test_corrupt_store_recovers_previous_version(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    first = service.add_job(
        name="first",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="first",
    )
    service.add_job(
        name="second",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="second",
    )
    store_path.write_text("{broken", encoding="utf-8")

    recovered = CronService(store_path).list_jobs(include_disabled=True)

    assert [job.id for job in recovered] == [first.id]
    assert list(store_path.parent.glob("jobs.json.corrupt-*"))


@pytest.mark.asyncio
async def test_due_jobs_execute_concurrently(tmp_path) -> None:
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_finished = asyncio.Event()

    async def on_job(job) -> None:
        if job.name == "slow":
            slow_started.set()
            await release_slow.wait()
        else:
            fast_finished.set()

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    slow = service.add_job(
        name="slow",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="slow",
    )
    fast = service.add_job(
        name="fast",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="fast",
    )
    slow.state.next_run_at_ms = 1
    fast.state.next_run_at_ms = 1
    timer = asyncio.create_task(service._on_timer())

    await asyncio.wait_for(slow_started.wait(), timeout=1.0)
    await asyncio.wait_for(fast_finished.wait(), timeout=0.2)
    release_slow.set()
    await timer


@pytest.mark.asyncio
async def test_job_timeout_is_recorded(tmp_path) -> None:
    async def on_job(_job) -> None:
        await asyncio.Event().wait()

    service = CronService(
        tmp_path / "cron" / "jobs.json",
        on_job=on_job,
        job_timeout_s=0.01,
    )
    job = service.add_job(
        name="slow",
        schedule=CronSchedule(kind="every", every_ms=1000),
        message="slow",
    )

    assert await service.run_job(job.id, force=True) is True
    assert job.state.last_status == "error"
    assert job.state.last_error == "timed out after 0.01s"


@pytest.mark.asyncio
async def test_idle_service_discovers_external_job(tmp_path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    called = asyncio.Event()

    async def on_job(_job) -> None:
        called.set()

    service = CronService(store_path, on_job=on_job)
    service._STORE_POLL_INTERVAL_S = 0.05
    await service.start()
    try:
        original_stat = store_path.stat()
        external = CronService(store_path)
        external.add_job(
            name="external",
            schedule=CronSchedule(kind="every", every_ms=50),
            message="hello",
        )
        os.utime(
            store_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        await asyncio.wait_for(called.wait(), timeout=1.0)
    finally:
        service.stop()
