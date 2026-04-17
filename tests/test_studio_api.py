from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from yuanclaw.api.server import (
    _build_request_runtime_override,
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


def test_session_summary_api_falls_back_when_model_returns_error_text(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.agents.defaults.provider = "moonshot"
    runtime.config.providers.moonshot.api_key = "test-key"
    runtime.provider.chat = AsyncMock(
        return_value=SimpleNamespace(
            content="Error: Error code: 429 - {'error': {'code': '1113', 'message': '余额不足或无可用资源包,请充值。'}}"
        )
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/sessions/studio%3Acowboy-biaoge%3Athread-3/summary",
            json={"content": "hi", "cowboyName": "牛表哥"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "llm"
    assert payload["summary"] == "hi"

    sessions = runtime.session_manager.list_sessions()
    assert sessions[0]["thread_summary"] == "hi"


def test_session_summary_api_falls_back_when_model_returns_timeout_text(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.agents.defaults.provider = "moonshot"
    runtime.config.providers.moonshot.api_key = "test-key"
    runtime.provider.chat = AsyncMock(return_value=SimpleNamespace(content="Error: Request timed out."))

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/sessions/studio%3Acowboy-biaoge%3Athread-4/summary",
            json={"content": "帮我看飞书登录状态", "cowboyName": "牛表哥"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "llm"
    assert payload["summary"] == "帮我看飞书登录状态"

    sessions = runtime.session_manager.list_sessions()
    assert sessions[0]["thread_summary"] == "帮我看飞书登录状态"


def test_build_request_runtime_override_uses_per_request_model_pool_config() -> None:
    config = Config()
    config.agents.defaults.model = "glm-5-turbo"
    config.agents.defaults.provider = "custom"
    config.providers.custom.api_key = "old-key"
    config.providers.custom.api_base = "https://old.example.com/v1"

    provider, model, provider_name = _build_request_runtime_override(
        config,
        {
            "runtimeConfig": {
                "provider": "custom",
                "adapter": "openai_chat_stream_aggregate",
                "model": "gpt-4.1",
                "apiKey": "new-key",
                "apiBase": "https://override.example.com/v1",
            }
        },
    )

    assert provider is not None
    assert model == "gpt-4.1"
    assert provider_name == "custom"
    assert provider.api_key == "new-key"
    assert provider.api_base == "https://override.example.com/v1"
    assert provider.adapter == "openai_chat_stream_aggregate"


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
