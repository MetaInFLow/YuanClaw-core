"""Tests for workspace scope policy and file-tool enforcement."""

from __future__ import annotations

import pytest

from yuanclaw.agent.context import ContextBuilder
from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.filesystem import ReadFileTool
from yuanclaw.bus.events import InboundMessage
from yuanclaw.bus.queue import MessageBus
from yuanclaw.providers.base import LLMProvider, LLMResponse
from yuanclaw.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScopeResolver,
    bind_workspace_scope,
    build_workspace_scope,
    current_workspace_scope,
    reset_workspace_scope,
)
from yuanclaw.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path


def test_resolve_allowed_path_rejects_paths_outside_allowed_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspaceBoundaryError) as exc:
        resolve_allowed_path(outside, workspace=workspace, allowed_root=workspace)

    assert "outside allowed directory" in str(exc.value)
    assert "hard policy boundary" in str(exc.value)


@pytest.mark.asyncio
async def test_file_tool_uses_bound_restricted_workspace_scope(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    inside = project / "inside.txt"
    inside.write_text("ok", encoding="utf-8")

    tool = ReadFileTool(workspace=workspace)
    token = bind_workspace_scope(build_workspace_scope(project, "restricted"))
    try:
        assert await tool.execute(str(inside)) == "ok"
        denied = await tool.execute(str(outside))
    finally:
        reset_workspace_scope(token)

    assert "outside allowed directory" in denied


@pytest.mark.asyncio
async def test_file_tool_bound_full_scope_allows_absolute_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
    token = bind_workspace_scope(build_workspace_scope(workspace, "full"))
    try:
        result = await tool.execute(str(outside))
    finally:
        reset_workspace_scope(token)

    assert result == "secret"


def test_workspace_scope_resolver_persists_websocket_message_scope(tmp_path) -> None:
    default = tmp_path / "default"
    project = tmp_path / "project"
    default.mkdir()
    project.mkdir()
    resolver = WorkspaceScopeResolver(default, default_restrict_to_workspace=True)
    session = type("Session", (), {"metadata": {}})()
    msg = type(
        "Msg",
        (),
        {
            "channel": "websocket",
            "metadata": {
                WORKSPACE_SCOPE_METADATA_KEY: {
                    "project_path": str(project),
                    "access_mode": "full",
                }
            },
        },
    )()

    scope = resolver.for_message(msg, session.metadata)
    resolver.persist_message_scope(session, msg)

    assert scope.project_path == project.resolve()
    assert scope.access_mode == "full"
    assert session.metadata[WORKSPACE_SCOPE_METADATA_KEY]["project_path"] == str(project)


def test_workspace_scope_context_resets(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = bind_workspace_scope(build_workspace_scope(workspace, "restricted"))
    assert current_workspace_scope() is not None
    reset_workspace_scope(token)
    assert current_workspace_scope() is None


def test_context_builder_blocks_media_outside_restricted_scope(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"demo-bytes")

    builder = ContextBuilder(workspace)
    token = bind_workspace_scope(build_workspace_scope(project, "restricted"))
    try:
        messages = builder.build_messages(
            history=[],
            current_message="describe image",
            media=[str(outside)],
        )
    finally:
        reset_workspace_scope(token)

    assert isinstance(messages[-1]["content"], str)
    assert messages[-1]["content"].endswith("describe image")


def test_context_builder_allows_media_outside_full_scope(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"demo-bytes")

    builder = ContextBuilder(workspace)
    token = bind_workspace_scope(build_workspace_scope(workspace, "full"))
    try:
        messages = builder.build_messages(
            history=[],
            current_message="describe image",
            media=[str(outside)],
        )
    finally:
        reset_workspace_scope(token)

    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"


class _Provider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                   reasoning_effort=None, on_text_delta=None):
        return LLMResponse(content="done", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_agent_loop_binds_and_resets_workspace_scope_for_turn(tmp_path, monkeypatch) -> None:
    default = tmp_path / "default"
    project = tmp_path / "project"
    default.mkdir()
    project.mkdir()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_Provider(),
        workspace=default,
        restrict_to_workspace=True,
    )
    observed = {}

    async def fake_run(messages, **kwargs):
        scope = current_workspace_scope()
        observed["project_path"] = scope.project_path if scope else None
        observed["access_mode"] = scope.access_mode if scope else None
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop._process_message(
        InboundMessage(
            channel="websocket",
            chat_id="chat",
            sender_id="user",
            content="hello",
            metadata={
                WORKSPACE_SCOPE_METADATA_KEY: {
                    "project_path": str(project),
                    "access_mode": "full",
                }
            },
        )
    )

    session = loop.sessions.get_or_create("websocket:chat")
    assert observed == {"project_path": project.resolve(), "access_mode": "full"}
    assert session.metadata[WORKSPACE_SCOPE_METADATA_KEY]["project_path"] == str(project)
    assert current_workspace_scope() is None


@pytest.mark.asyncio
async def test_agent_loop_accepts_explicit_workspace_scope_from_studio_turn(tmp_path, monkeypatch) -> None:
    default = tmp_path / "default"
    project = tmp_path / "project"
    default.mkdir()
    project.mkdir()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_Provider(),
        workspace=default,
        restrict_to_workspace=True,
    )
    observed = {}

    async def fake_run(messages, **kwargs):
        scope = current_workspace_scope()
        observed["project_path"] = scope.project_path if scope else None
        observed["access_mode"] = scope.access_mode if scope else None
        return "done", [], messages, {}

    monkeypatch.setattr(loop, "_run_agent_loop", fake_run)

    await loop._process_message(
        InboundMessage(
            channel="studio",
            chat_id="thread",
            sender_id="user",
            content="hello",
            metadata={
                WORKSPACE_SCOPE_METADATA_KEY: {
                    "projectPath": str(project),
                    "accessMode": "restricted",
                }
            },
        )
    )

    session = loop.sessions.get_or_create("studio:thread")
    assert observed == {"project_path": project.resolve(), "access_mode": "restricted"}
    assert session.metadata[WORKSPACE_SCOPE_METADATA_KEY]["projectPath"] == str(project)
    assert current_workspace_scope() is None
