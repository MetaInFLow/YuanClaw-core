import json

from yuanclaw.agent.context import ContextBuilder
from yuanclaw.agent.loop import AgentLoop
from yuanclaw.session.manager import Session


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_BYTES = 500
    return loop


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_serializes_small_structured_tool_result() -> None:
    loop = _mk_loop()
    session = Session(key="test:structured-tool")

    loop._save_turn(
        session,
        [{"role": "tool", "content": {"ok": True, "items": [1, "two"]}}],
        skip=0,
    )

    assert json.loads(session.messages[0]["content"]) == {
        "ok": True,
        "items": [1, "two"],
    }


def test_save_turn_bounds_large_structured_tool_result_by_utf8_bytes() -> None:
    loop = _mk_loop()
    session = Session(key="test:large-structured-tool")

    loop._save_turn(
        session,
        [{"role": "tool", "content": {"rows": [{"text": "结果" * 200}]}}],
        skip=0,
    )

    persisted = session.messages[0]["content"]
    assert persisted.endswith(loop._TOOL_RESULT_TRUNCATION_SUFFIX)
    assert len(persisted.encode("utf-8")) <= loop._TOOL_RESULT_MAX_BYTES
    assert "\ufffd" not in persisted


def test_save_turn_bounds_large_string_tool_result_by_utf8_bytes() -> None:
    loop = _mk_loop()
    session = Session(key="test:large-string-tool")

    loop._save_turn(
        session,
        [{"role": "tool", "content": "中文" * 200}],
        skip=0,
    )

    persisted = session.messages[0]["content"]
    assert persisted.endswith(loop._TOOL_RESULT_TRUNCATION_SUFFIX)
    assert len(persisted.encode("utf-8")) <= loop._TOOL_RESULT_MAX_BYTES
    assert "\ufffd" not in persisted
