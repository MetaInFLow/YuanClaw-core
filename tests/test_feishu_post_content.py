import threading

import pytest

from yuanclaw.bus.queue import MessageBus
from yuanclaw.channels.feishu import FeishuChannel, _extract_post_content
from yuanclaw.config.schema import FeishuConfig


def test_extract_post_content_supports_post_wrapper_shape() -> None:
    payload = {
        "post": {
            "zh_cn": {
                "title": "日报",
                "content": [
                    [
                        {"tag": "text", "text": "完成"},
                        {"tag": "img", "image_key": "img_1"},
                    ]
                ],
            }
        }
    }

    text, image_keys = _extract_post_content(payload)

    assert text == "日报 完成"
    assert image_keys == ["img_1"]


def test_extract_post_content_keeps_direct_shape_behavior() -> None:
    payload = {
        "title": "Daily",
        "content": [
            [
                {"tag": "text", "text": "report"},
                {"tag": "img", "image_key": "img_a"},
                {"tag": "img", "image_key": "img_b"},
            ]
        ],
    }

    text, image_keys = _extract_post_content(payload)

    assert text == "Daily report"
    assert image_keys == ["img_a", "img_b"]


def test_register_optional_event_keeps_builder_when_method_missing() -> None:
    class Builder:
        pass

    builder = Builder()
    same = FeishuChannel._register_optional_event(builder, "missing", object())
    assert same is builder


def test_register_optional_event_calls_supported_method() -> None:
    called = []

    class Builder:
        def register_event(self, handler):
            called.append(handler)
            return self

    builder = Builder()
    handler = object()
    same = FeishuChannel._register_optional_event(builder, "register_event", handler)

    assert same is builder
    assert called == [handler]


@pytest.mark.asyncio
async def test_feishu_stop_wakes_and_joins_reconnect_thread() -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._running = True
    thread = threading.Thread(target=channel._ws_stop_event.wait, args=(30,))
    channel._ws_thread = thread
    thread.start()

    await channel.stop()

    assert not thread.is_alive()
    assert channel._ws_thread is None
    assert channel._loop is None
