from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        self.applied_configs = []
        self.prepared_configs = []
        self.distill_payload = None
        self.distill_response = {
            "headline": "Workspace knowledge stays readable before opening the modal",
            "summaryPreview": "Show distilled summaries instead of session tail fragments.",
            "summaryMarkdown": "# Workspace knowledge stays readable before opening the modal",
            "insights": [
                {
                    "title": "Obsidian CLI missing from PATH",
                    "kind": "technical",
                    "summary": "Obsidian CLI cannot be invoked until PATH is fixed.",
                    "sourceSessionKeys": ["studio:test:1"],
                    "sourceExcerpt": "Obsidian CLI 未注册到 PATH",
                }
            ],
        }
        self.distill_error = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def running(self) -> bool:
        return True

    def status_payload(self) -> dict[str, object]:
        return {}

    def prepare_config(self, config: Config) -> dict[str, object]:
        self.prepared_configs.append(config)
        return {}

    async def apply_config(
        self, config: Config, prepared_components: dict[str, object] | None = None
    ) -> None:
        self.applied_configs.append((config, prepared_components))
        self.config = config

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

    async def distill_knowledge(self, payload: dict[str, object]) -> dict[str, object]:
        self.distill_payload = payload
        if self.distill_error:
            raise self.distill_error
        return self.distill_response


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


def test_internal_knowledge_distill_api_returns_structured_output(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    session = runtime.session_manager.get_or_create("studio:test:1")
    session.add_message("user", "Obsidian CLI 为什么还不能用？")
    session.add_message("assistant", "因为当前 PATH 里还没有注册 Obsidian CLI。")
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/internal/knowledge/distill",
            json={
                "runDate": "2026-04-22",
                "sessions": [
                    {
                        "sessionKey": "studio:test:1",
                        "updatedAt": "2026-04-22T08:00:00Z",
                        "threadSummary": "排查 Obsidian CLI",
                        "lastMessagePreview": "Obsidian CLI 未注册到 PATH",
                    }
                ],
                "existingInsights": [],
                "collectionPrompt": "Collect relevant chats.",
                "systemPrompt": "Summarize durable knowledge.",
                "archivePrompt": "Archive durable insights.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["headline"] == "Workspace knowledge stays readable before opening the modal"
    assert payload["insights"][0]["sourceSessionKeys"] == ["studio:test:1"]
    assert runtime.distill_payload is not None
    assert runtime.distill_payload["sessions"][0]["sessionKey"] == "studio:test:1"


def test_internal_knowledge_distill_api_surfaces_runtime_failures(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.distill_error = RuntimeError("summary model is unavailable")

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/internal/knowledge/distill",
            json={
                "runDate": "2026-04-22",
                "sessions": [
                    {
                        "sessionKey": "studio:test:1",
                        "updatedAt": "2026-04-22T08:00:00Z",
                    }
                ],
                "existingInsights": [],
                "collectionPrompt": "",
                "systemPrompt": "",
                "archivePrompt": "",
            },
        )

    assert response.status_code == 500
    assert "summary model is unavailable" in response.json()["detail"]


def test_write_config_api_rejects_incomplete_present_channel_payload(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    payload = runtime.config.model_dump(by_alias=True)
    payload["studio"] = {
        "channelRows": {
            "telegram": {
                "present": True,
            }
        },
        "channels": {
            "telegram": {
                "enabled": False,
                "token": "",
            }
        },
    }
    payload["channels"]["telegram"] = {
        "enabled": False,
        "token": "",
    }

    with patch("yuanclaw.api.server.save_config") as mock_save_config:
        with TestClient(create_app(runtime)) as client:
            response = client.put("/api/config", json=payload)

    assert response.status_code == 400
    assert "telegram" in response.json()["detail"]
    assert "token" in response.json()["detail"]
    mock_save_config.assert_not_called()
    assert runtime.prepared_configs == []
    assert runtime.applied_configs == []


def test_write_config_api_rejects_enabled_runtime_channel_without_required_fields(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    payload = runtime.config.model_dump(by_alias=True)
    payload["channels"]["telegram"]["enabled"] = True
    payload["channels"]["telegram"]["token"] = ""

    with patch("yuanclaw.api.server.save_config") as mock_save_config:
        with TestClient(create_app(runtime)) as client:
            response = client.put("/api/config", json=payload)

    assert response.status_code == 400
    assert "telegram" in response.json()["detail"]
    assert "token" in response.json()["detail"]
    mock_save_config.assert_not_called()
    assert runtime.prepared_configs == []
    assert runtime.applied_configs == []
def test_usage_api_aggregates_session_usage(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    first = runtime.session_manager.get_or_create("studio:cowboy-biaoge:thread-1")
    runtime.session_manager.record_usage(
        first,
        provider="openai",
        model="gpt-4o-mini",
        usage={
            "requests": 2,
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
        },
    )
    runtime.session_manager.save(first)

    second = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-2")
    runtime.session_manager.record_usage(
        second,
        provider="moonshot",
        model="moonshot/kimi-k2.5",
        usage={
            "requests": 1,
            "prompt_tokens": 60,
            "completion_tokens": 40,
            "total_tokens": 100,
        },
    )
    runtime.session_manager.save(second)

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["sessions"] == 2
    assert payload["totals"]["requests"] == 3
    assert payload["totals"]["prompt_tokens"] == 180
    assert payload["totals"]["completion_tokens"] == 120
    assert payload["totals"]["total_tokens"] == 300
    assert payload["providers"][0]["key"] == "openai"
    assert payload["providers"][0]["total_tokens"] == 200
    assert payload["models"][0]["key"] == "gpt-4o-mini"


def test_usage_api_counts_assistant_requests_without_metadata_usage(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    session = runtime.session_manager.get_or_create("studio:cowboy-biaoge:thread-usage-fallback")
    session.add_message("user", "hi")
    session.add_message(
        "assistant",
        "hello",
        model="openai-codex/gpt-5.1-codex",
        provider="openai_codex",
    )
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/usage")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["sessions"] == 1
    assert payload["totals"]["requests"] == 1
    assert payload["totals"]["total_tokens"] == 0
    assert payload["providers"][0]["key"] == "openai_codex"
    assert payload["providers"][0]["requests"] == 1
    assert payload["models"][0]["key"] == "openai-codex/gpt-5.1-codex"
    assert payload["models"][0]["requests"] == 1
