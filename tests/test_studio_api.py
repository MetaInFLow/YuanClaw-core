from types import SimpleNamespace

from fastapi.testclient import TestClient

from yuanclaw.api.server import create_app
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

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def running(self) -> bool:
        return True

    def status_payload(self) -> dict[str, object]:
        return {}


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
