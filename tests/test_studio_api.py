from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from yuanclaw.api.server import (
    _compact_thread_summary,
    _has_summary_model_access,
    _normalize_thread_summary,
    create_app,
)
from yuanclaw.config.schema import Config
from yuanclaw.session.manager import SessionManager


class _RuntimeStub:
    def __init__(self, workspace):
        self.config = Config()
        self.config.agents.defaults.workspace = str(workspace)
        self.session_manager = SessionManager(workspace)
        self.channels = None
        self.cron = SimpleNamespace(list_jobs=lambda include_disabled=True: [])
        self.host = "127.0.0.1"
        self.port = 18789
        self.started_at = 0.0
        self.provider = SimpleNamespace(chat=AsyncMock())

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def running(self) -> bool:
        return True

    def status_payload(self) -> dict[str, object]:
        return {}

    async def generate_thread_summary(
        self,
        session_key: str,
        content: str,
        cowboy_name: str | None = None,
    ) -> dict[str, str]:
        fallback = _compact_thread_summary(content)
        if not _has_summary_model_access(self.config):
            self.session_manager.set_thread_summary(session_key, fallback)
            return {"summary": fallback, "mode": "input"}

        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": "stub"},
                {"role": "user", "content": f"{cowboy_name}:{content}"},
            ]
        )
        summary = _normalize_thread_summary(response.content, fallback)
        self.session_manager.set_thread_summary(session_key, summary)
        return {"summary": summary, "mode": "llm"}


def test_sessions_api_includes_message_summary_fields(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    session = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-1")
    session.add_message("user", "Please review the desktop wiring plan.")
    session.add_message(
        "assistant",
        "I wired the runtime adapter and updated the desktop startup flow.",
    )
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/sessions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1

    item = payload["items"][0]
    assert item["key"] == "studio:cowboy-manong:thread-1"
    assert item["message_count"] == 2
    assert item["last_role"] == "assistant"
    assert item["last_message_preview"] == (
        "I wired the runtime adapter and updated the desktop startup flow."
    )


def test_sessions_api_uses_null_preview_for_empty_sessions(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    session = runtime.session_manager.get_or_create("studio:cowboy-biaoge:thread-empty")
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/sessions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["message_count"] == 0
    assert item["last_role"] is None
    assert item["last_message_preview"] is None


def test_session_summary_api_falls_back_to_input_without_api_key(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/sessions/studio%3Acowboy-biaoge%3Athread-1/summary",
            json={"content": "帮我梳理飞书表格字段设计", "cowboyName": "牛表哥"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "input"
    assert payload["summary"] == "帮我梳理飞书表格字段设计"

    sessions = runtime.session_manager.list_sessions()
    assert sessions[0]["thread_summary"] == "帮我梳理飞书表格字段设计"


def test_session_summary_api_uses_model_output_when_provider_is_configured(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.agents.defaults.provider = "moonshot"
    runtime.config.providers.moonshot.api_key = "test-key"
    runtime.provider.chat = AsyncMock(return_value=SimpleNamespace(content="飞书表格结构梳理"))

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/sessions/studio%3Acowboy-biaoge%3Athread-2/summary",
            json={"content": "帮我梳理飞书表格字段设计", "cowboyName": "牛表哥"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "llm"
    assert payload["summary"] == "飞书表格结构梳理"

    sessions = runtime.session_manager.list_sessions()
    assert sessions[0]["thread_summary"] == "飞书表格结构梳理"
