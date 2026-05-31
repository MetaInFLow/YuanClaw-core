import base64
import json
from pathlib import Path
from typing import Any

import pytest

from yuanclaw.agent.loop import AgentLoop
from yuanclaw.agent.tools.image_generation import ImageGenerationTool
from yuanclaw.bus.queue import MessageBus
from yuanclaw.config.loader import set_config_path
from yuanclaw.config.schema import Config
from yuanclaw.providers.base import LLMProvider, LLMResponse
from yuanclaw.providers.image_generation import (
    ImageGenerationResponse,
    image_gen_provider_configs,
    register_image_gen_provider,
)
from yuanclaw.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lwqV9QAAAABJRU5ErkJggg=="
)
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode("ascii")


class _FakeLLMProvider(LLMProvider):
    async def chat(self, **_kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "fake-model"


class _MockImageProvider:
    calls: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def generate(self, **kwargs: Any) -> ImageGenerationResponse:
        self.calls.append({"init": self.kwargs, "generate": kwargs})
        return ImageGenerationResponse(images=[_PNG_DATA_URL])


def test_image_generation_config_accepts_camel_case_payload() -> None:
    config = Config.model_validate(
        {
            "tools": {
                "imageGeneration": {
                    "enabled": True,
                    "provider": "mock",
                    "model": "mock-image",
                    "defaultAspectRatio": "16:9",
                    "defaultImageSize": "2K",
                    "maxImagesPerTurn": 2,
                    "saveDir": "generated",
                }
            }
        }
    )

    assert config.tools.image_generation.enabled is True
    assert config.tools.image_generation.provider == "mock"
    assert config.tools.image_generation.default_aspect_ratio == "16:9"
    assert config.tools.image_generation.max_images_per_turn == 2


def test_agent_loop_registers_image_tool_only_when_enabled(tmp_path: Path) -> None:
    disabled = Config()
    disabled.tools.image_generation.enabled = False
    disabled_loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeLLMProvider(),
        workspace=tmp_path,
        image_generation_config=disabled.tools.image_generation,
    )
    assert disabled_loop.tools.get("generate_image") is None

    enabled = Config()
    enabled.tools.image_generation.enabled = True
    enabled_loop = AgentLoop(
        bus=MessageBus(),
        provider=_FakeLLMProvider(),
        workspace=tmp_path,
        image_generation_config=enabled.tools.image_generation,
    )
    tool = enabled_loop.tools.get("generate_image")
    assert tool is not None
    schema = tool.to_schema()["function"]["parameters"]
    assert {"prompt", "aspect_ratio", "reference_images", "count"}.issubset(
        schema["properties"]
    )


@pytest.mark.asyncio
async def test_generate_image_writes_artifact_with_mock_provider(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir()
    set_config_path(config_file)
    _MockImageProvider.calls = []
    register_image_gen_provider("mock", _MockImageProvider)

    config = Config()
    config.tools.image_generation.enabled = True
    config.tools.image_generation.provider = "mock"
    config.tools.image_generation.model = "mock-image"
    config.providers.custom.api_key = "mock-key"
    config.providers.custom.api_base = "https://mock.example/v1"

    tool = ImageGenerationTool(
        workspace=tmp_path / "workspace",
        config=config.tools.image_generation,
        provider_configs=image_gen_provider_configs(config),
    )

    result = json.loads(
        await tool.execute(prompt="a tiny red square", aspect_ratio="1:1", count=1)
    )

    artifact = result["artifacts"][0]
    artifact_path = Path(artifact["path"])
    metadata_path = artifact_path.with_suffix(".json")
    assert artifact["id"].startswith("img_")
    assert artifact["mime"] == "image/png"
    assert artifact["prompt"] == "a tiny red square"
    assert artifact["model"] == "mock-image"
    assert artifact["provider"] == "mock"
    assert artifact_path.exists()
    assert artifact_path.read_bytes() == _PNG_BYTES
    assert metadata_path.exists()
    assert "reference_images" in result["next_step"]
    assert _MockImageProvider.calls[0]["generate"]["aspect_ratio"] == "1:1"


@pytest.mark.asyncio
async def test_generate_image_rejects_reference_images_outside_workspace_and_media(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    scope = build_workspace_scope(workspace, "restricted")
    token = bind_workspace_scope(scope)
    try:
        config = Config()
        config.tools.image_generation.enabled = True
        config.tools.image_generation.provider = "mock"
        tool = ImageGenerationTool(
            workspace=workspace,
            config=config.tools.image_generation,
            provider_configs={},
        )

        result = await tool.execute(prompt="edit", reference_images=[str(outside)])
    finally:
        reset_workspace_scope(token)

    assert result.startswith("Error:")
    assert "reference_images must be inside the workspace or YuanClaw media directory" in result
