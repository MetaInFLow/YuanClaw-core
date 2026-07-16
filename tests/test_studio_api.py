import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from yuanclaw.api.server import (
    CoreRuntime,
    RuntimeEventBroker,
    _compact_thread_summary,
    _has_summary_model_access,
    _normalize_thread_summary,
    _validate_public_bind_auth,
    create_app,
)
from yuanclaw.config.paths import get_media_dir
from yuanclaw.config.schema import Config
from yuanclaw.providers.openai_compatible_provider import OpenAICompatibleProvider
from yuanclaw.session.manager import SessionManager


class _RuntimeStub:
    def __init__(self, workspace):
        self.config = Config()
        self.config.agents.defaults.workspace = str(workspace)
        self.session_manager = SessionManager(workspace)
        self.channels = None
        self.events = RuntimeEventBroker()
        self.cron = SimpleNamespace(list_jobs=lambda include_disabled=True: [])
        self.host = "127.0.0.1"
        self.port = 18789
        self.started_at = 0.0
        self.provider = SimpleNamespace(chat=AsyncMock())
        self.agent = SimpleNamespace(
            process_direct=AsyncMock(return_value="stub reply"),
            cancel_session=AsyncMock(return_value=0),
        )
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


def test_delete_session_api_cancels_work_and_removes_persistence(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    session = runtime.session_manager.get_or_create("studio:thread-delete")
    session.add_message("user", "delete me")
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime=runtime)) as client:
        response = client.delete(f"/api/sessions/{quote(session.key, safe='')}")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "deleted": True,
        "cancelled_tasks": 0,
    }
    runtime.agent.cancel_session.assert_awaited_once_with(session.key)
    assert runtime.session_manager.read_session_file(session.key) is None


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


def test_session_messages_api_replays_metadata_and_signed_media_urls(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    runtime = _RuntimeStub(tmp_path / "workspace")
    media_dir = get_media_dir("api")
    media_path = media_dir / "sample.png"
    media_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    session = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-media")
    session.metadata["goal_state"] = {
        "status": "active",
        "objective": "finish the replay route",
        "ui_summary": "replay route",
    }
    session.add_message("user", "see image", media=[str(media_path)])
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.get(
            f"/api/sessions/{quote(session.key, safe='')}/messages",
        )

        assert response.status_code == 200
        payload = response.json()
        media_url = payload["messages"][0]["media_urls"][0]["url"]
        media_response = client.get(media_url)

    assert payload["key"] == session.key
    assert payload["metadata"]["goal_state"]["objective"] == "finish the replay route"
    assert payload["messages"][0]["content"] == "see image"
    assert "media" not in payload["messages"][0]
    assert payload["messages"][0]["media_urls"][0]["name"] == "sample.png"
    assert media_url.startswith("/api/media/")
    assert media_response.status_code == 200
    assert media_response.content == b"\x89PNG\r\n\x1a\nfake"
    assert media_response.headers["x-content-type-options"] == "nosniff"


def test_session_messages_api_strips_unservable_media_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    runtime = _RuntimeStub(tmp_path / "workspace")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    session = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-outside-media")
    session.add_message("user", "outside", media=[str(outside)])
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.get(f"/api/sessions/{quote(session.key, safe='')}/messages")

    assert response.status_code == 200
    message = response.json()["messages"][0]
    assert "media" not in message
    assert "media_urls" not in message


def test_signed_media_route_rejects_tampered_signature(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    runtime = _RuntimeStub(tmp_path / "workspace")
    media_path = get_media_dir("api") / "tamper.png"
    media_path.write_bytes(b"image")
    session = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-tamper")
    session.add_message("user", "tamper", media=[str(media_path)])
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        response = client.get(f"/api/sessions/{quote(session.key, safe='')}/messages")
        media_url = response.json()["messages"][0]["media_urls"][0]["url"]
        tampered = media_url.replace("/api/media/", "/api/media/invalid", 1)
        tampered_response = client.get(tampered)

    assert tampered_response.status_code == 401


def test_signed_media_route_requires_gateway_token_when_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.gateway.token = "static-token"
    media_path = get_media_dir("api") / "protected.png"
    media_path.write_bytes(b"image")
    session = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-protected")
    session.add_message("user", "protected", media=[str(media_path)])
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        replay = client.get(
            f"/api/sessions/{quote(session.key, safe='')}/messages",
            headers={"Authorization": "Bearer static-token"},
        )
        media_url = replay.json()["messages"][0]["media_urls"][0]["url"]
        unauthorized = client.get(media_url)
        authorized = client.get(
            media_url,
            headers={"Authorization": "Bearer static-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_signed_media_route_supports_single_byte_range(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    runtime = _RuntimeStub(tmp_path / "workspace")
    media_path = get_media_dir("api") / "clip.mp4"
    media_path.write_bytes(b"0123456789")
    session = runtime.session_manager.get_or_create("studio:cowboy-manong:thread-range")
    session.add_message("user", "clip", media=[str(media_path)])
    runtime.session_manager.save(session)

    with TestClient(create_app(runtime)) as client:
        replay = client.get(f"/api/sessions/{quote(session.key, safe='')}/messages")
        media_url = replay.json()["messages"][0]["media_urls"][0]["url"]
        partial = client.get(media_url, headers={"Range": "bytes=2-5"})
        invalid = client.get(media_url, headers={"Range": "bytes=20-30"})

    assert partial.status_code == 206
    assert partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    assert partial.headers["accept-ranges"] == "bytes"
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */10"


def test_session_messages_api_does_not_create_missing_session(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/sessions/studio%3Amissing/messages")
        listed = client.get("/api/sessions")

    assert response.status_code == 404
    assert listed.json()["total"] == 0


def test_ws_chat_done_frame_and_runtime_event_include_goal_state(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")

    async def _process_direct(**kwargs):
        session = runtime.session_manager.get_or_create(kwargs["session_key"])
        session.metadata["goal_state"] = {
            "status": "active",
            "objective": "continue core API work",
            "ui_summary": "core API",
        }
        runtime.session_manager.save(session)
        return "working"

    runtime.agent.process_direct = AsyncMock(side_effect=_process_direct)
    event_queue = runtime.events.subscribe()

    async def _noop_progress(*args, **kwargs):
        return None

    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "continue",
                    "sessionKey": "studio:cowboy-manong:thread-goal",
                }
            )
            running = websocket.receive_json()
            done = websocket.receive_json()
            turn_end = websocket.receive_json()

    assert running["type"] == "goal_status"
    assert running["status"] == "running"
    assert done["type"] == "done"
    assert done["goalState"]["active"] is True
    assert done["goalState"]["objective"] == "continue core API work"
    assert turn_end["type"] == "turn_end"
    assert turn_end["sessionKey"] == "studio:cowboy-manong:thread-goal"
    assert turn_end["goalState"]["active"] is True

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    reply_events = [event for event in events if event.get("type") == "agent.reply_done"]
    assert reply_events
    assert reply_events[-1]["goal_state"]["active"] is True
    assert any(event.get("type") == "agent.goal_status" and event.get("status") == "running" for event in events)
    assert any(event.get("type") == "agent.turn_end" for event in events)


def test_ws_chat_forwards_structured_progress_fields(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")

    async def _process_direct(**kwargs):
        await kwargs["on_progress"](
            "edited files",
            tool_hint=True,
            tool_events=[{"name": "apply_patch", "status": "ok"}],
            file_edit_events=[{"path": "demo.txt", "action": "update"}],
        )
        return "done"

    runtime.agent.process_direct = AsyncMock(side_effect=_process_direct)

    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "edit",
                    "sessionKey": "studio:cowboy-manong:thread-progress",
                }
            )
            assert websocket.receive_json()["type"] == "goal_status"
            progress = websocket.receive_json()

    assert progress["type"] == "progress"
    assert progress["toolHint"] is True
    assert progress["toolEvents"] == [{"name": "apply_patch", "status": "ok"}]
    assert progress["fileEditEvents"] == [{"path": "demo.txt", "action": "update"}]


def test_ws_chat_forwards_cli_apps_and_mcp_presets_to_agent(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")

    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "use apps",
                    "sessionKey": "studio:cowboy-manong:thread-apps",
                    "cliApps": [{"name": "Obsidian", "entryPoint": "obsidian-cli"}],
                    "mcpPresets": [{"name": "GitHub", "transport": "stdio"}],
                }
            )
            assert websocket.receive_json()["type"] == "goal_status"
            assert websocket.receive_json()["type"] == "done"

    kwargs = runtime.agent.process_direct.await_args.kwargs
    assert kwargs["metadata"]["cliApps"] == [
        {"name": "Obsidian", "entryPoint": "obsidian-cli"}
    ]
    assert kwargs["metadata"]["mcpPresets"] == [{"name": "GitHub", "transport": "stdio"}]


def test_ws_chat_forwards_workspace_scope_to_agent_metadata(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    project = tmp_path / "project"
    project.mkdir()

    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "use project scope",
                    "sessionKey": "studio:cowboy-manong:thread-workspace",
                    "workspaceScope": {
                        "projectPath": str(project),
                        "accessMode": "restricted",
                    },
                }
            )
            assert websocket.receive_json()["type"] == "goal_status"
            assert websocket.receive_json()["type"] == "done"

    kwargs = runtime.agent.process_direct.await_args.kwargs
    assert kwargs["metadata"]["workspace_scope"] == {
        "project_path": str(project),
        "access_mode": "restricted",
    }


def test_ws_chat_stop_frame_cancels_running_session(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    started = asyncio.Event()

    async def _slow_process(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    runtime.agent.process_direct = AsyncMock(side_effect=_slow_process)
    runtime.agent.cancel_session = AsyncMock(return_value=1)

    with TestClient(create_app(runtime)) as client:
        with client.websocket_connect("/ws/chat") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "long task",
                    "sessionKey": "studio:thread-stop",
                }
            )
            assert websocket.receive_json()["type"] == "goal_status"
            websocket.send_json(
                {
                    "type": "stop",
                    "sessionKey": "studio:thread-stop",
                }
            )
            stopped = websocket.receive_json()

    assert stopped == {
        "type": "stopped",
        "sessionKey": "studio:thread-stop",
        "cancelled": 1,
    }
    runtime.agent.cancel_session.assert_awaited_once_with("studio:thread-stop")


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


def test_write_config_api_restores_disk_config_when_runtime_apply_fails(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    previous_model = runtime.config.agents.defaults.model
    payload = runtime.config.model_dump(by_alias=True)
    payload["agents"]["defaults"]["model"] = "openai/gpt-test"
    runtime.apply_config = AsyncMock(side_effect=RuntimeError("simulated start failure"))

    with patch("yuanclaw.api.server.save_config") as mock_save_config:
        with TestClient(create_app(runtime)) as client:
            response = client.put("/api/config", json=payload)

    assert response.status_code == 500
    assert "simulated start failure" in response.json()["detail"]
    assert mock_save_config.call_count == 2
    assert mock_save_config.call_args_list[0].args[0].agents.defaults.model == "openai/gpt-test"
    assert mock_save_config.call_args_list[1].args[0].agents.defaults.model == previous_model


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


def test_public_bind_requires_gateway_token_or_issue_secret() -> None:
    config = Config()

    for host in ("0.0.0.0", "::"):
        try:
            _validate_public_bind_auth(host, config.gateway)
        except RuntimeError as exc:
            assert "neither token nor token_issue_secret is set" in str(exc)
        else:
            raise AssertionError(f"expected public bind guard for {host}")


def test_local_bind_allows_empty_gateway_auth() -> None:
    config = Config()

    _validate_public_bind_auth("127.0.0.1", config.gateway)
    _validate_public_bind_auth("localhost", config.gateway)


def test_public_bind_allows_static_token_or_issue_secret() -> None:
    config = Config()
    config.gateway.token = "static-token"
    _validate_public_bind_auth("0.0.0.0", config.gateway)

    config.gateway.token = ""
    config.gateway.token_issue_secret = "issue-secret"
    _validate_public_bind_auth("::", config.gateway)


def test_config_accepts_gateway_auth_camel_case() -> None:
    config = Config.model_validate(
        {
            "gateway": {
                "token": "static-token",
                "tokenIssueSecret": "issue-secret",
                "tokenIssuePath": "/api/token",
                "tokenTtlS": 60,
            }
        }
    )

    assert config.gateway.token == "static-token"
    assert config.gateway.token_issue_secret == "issue-secret"
    assert config.gateway.token_issue_path == "/api/token"
    assert config.gateway.token_ttl_s == 60


@pytest.mark.asyncio
async def test_core_runtime_apply_config_refreshes_provider_and_tool_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    initial = Config()
    initial.gateway.host = "127.0.0.1"
    initial.agents.defaults.workspace = str(tmp_path / "workspace-a")
    runtime = CoreRuntime(initial, host="127.0.0.1", port=18789, with_channels=False)

    updated = Config.model_validate(initial.model_dump(by_alias=True))
    updated.agents.defaults.workspace = str(tmp_path / "workspace-b")
    updated.agents.defaults.provider = "longcat"
    updated.agents.defaults.model = "longcat/LongCat-Flash"
    updated.providers.longcat.api_key = "longcat-key"
    updated.tools.cli_apps.enabled = True
    updated.tools.image_generation.enabled = True

    old_agent = runtime.agent
    old_session_manager = runtime.session_manager
    await runtime.apply_config(updated)

    assert runtime.agent is not old_agent
    assert runtime.session_manager is not old_session_manager
    assert runtime.config.agents.defaults.model == "longcat/LongCat-Flash"
    assert runtime.agent.model == "longcat/LongCat-Flash"
    assert isinstance(runtime.provider, OpenAICompatibleProvider)
    assert isinstance(runtime.agent.provider, OpenAICompatibleProvider)
    assert runtime.agent.workspace == tmp_path / "workspace-b"
    assert runtime.agent.tools.get("run_cli_app") is not None
    assert runtime.agent.tools.get("generate_image") is not None
    assert runtime.agent.tools.get("run_cli_app") is not old_agent.tools.get("run_cli_app")


@pytest.mark.asyncio
async def test_core_runtime_apply_config_restores_running_components_on_start_failure(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    initial = Config()
    initial.agents.defaults.workspace = str(tmp_path / "workspace-a")
    runtime = CoreRuntime(initial, host="127.0.0.1", port=18789, with_channels=False)
    await runtime.start()
    previous_agent = runtime.agent
    previous_provider = runtime.provider
    original_start_locked = runtime._start_locked
    start_attempts = 0

    async def fail_new_runtime_once() -> None:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 1:
            raise RuntimeError("simulated start failure")
        await original_start_locked()

    monkeypatch.setattr(runtime, "_start_locked", fail_new_runtime_once)
    updated = Config.model_validate(initial.model_dump(by_alias=True))
    updated.agents.defaults.workspace = str(tmp_path / "workspace-b")

    with pytest.raises(RuntimeError, match="simulated start failure"):
        await runtime.apply_config(updated)

    assert runtime.running is True
    assert runtime.config is initial
    assert runtime.agent is previous_agent
    assert runtime.provider is previous_provider
    await runtime.stop()


def test_core_runtime_running_reflects_background_task_liveness(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_config_path",
        lambda: tmp_path / "instance" / "config.json",
    )
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    runtime = CoreRuntime(config, host="127.0.0.1", port=18789, with_channels=True)
    runtime._started = True
    runtime._agent_task = SimpleNamespace(done=lambda: False)
    runtime._channels_task = SimpleNamespace(done=lambda: False)
    assert runtime.running is True

    runtime._channels_task = SimpleNamespace(done=lambda: True)
    assert runtime.running is False


def test_api_requires_gateway_token_when_configured(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.gateway.token = "static-token"

    with TestClient(create_app(runtime)) as client:
        unauthorized = client.get("/api/status")
        authorized = client.get(
            "/api/status",
            headers={"Authorization": "Bearer static-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_gateway_token_issue_endpoint_mints_short_lived_api_token(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.gateway.token_issue_secret = "issue-secret"
    runtime.config.gateway.token_ttl_s = 60

    with TestClient(create_app(runtime)) as client:
        denied = client.get(runtime.config.gateway.token_issue_path)
        issued = client.get(
            runtime.config.gateway.token_issue_path,
            headers={"Authorization": "Bearer issue-secret"},
        )
        token = issued.json()["token"]
        authorized = client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert denied.status_code == 401
    assert issued.status_code == 200
    assert issued.json()["expires_in"] == 60
    assert isinstance(token, str) and len(token) >= 24
    assert authorized.status_code == 200


def test_websocket_requires_gateway_token_when_configured(tmp_path) -> None:
    runtime = _RuntimeStub(tmp_path / "workspace")
    runtime.config.gateway.token = "static-token"

    with TestClient(create_app(runtime)) as client:
        missing_token_rejected = False
        try:
            with client.websocket_connect("/ws/events"):
                pass
        except Exception:
            missing_token_rejected = True
        assert missing_token_rejected is True

        with client.websocket_connect("/ws/events?token=static-token") as websocket:
            assert websocket.receive_json()["type"] == "ready"
