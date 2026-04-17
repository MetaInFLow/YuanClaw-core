import asyncio
import json

import pytest

from yuanclaw.cron.service import CronService
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
