"""Image generation tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yuanclaw.agent.tools.base import Tool
from yuanclaw.config.paths import get_media_dir
from yuanclaw.config.schema import ImageGenerationToolConfig, ProviderConfig
from yuanclaw.providers.image_generation import (
    MAX_REFERENCE_IMAGE_BYTES,
    ImageGenerationError,
    get_image_gen_provider,
)
from yuanclaw.security.workspace_access import current_tool_workspace
from yuanclaw.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path
from yuanclaw.utils.artifacts import (
    ArtifactError,
    generated_image_tool_result,
    store_generated_image_artifact,
)
from yuanclaw.utils.helpers import detect_image_mime


class ImageGenerationTool(Tool):
    """Generate persistent image artifacts through the configured image provider."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: ImageGenerationToolConfig,
        provider_configs: dict[str, ProviderConfig] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.config = config
        self.provider_configs = dict(provider_configs or {})

    @property
    def name(self) -> str:
        return "generate_image"

    @property
    def description(self) -> str:
        return (
            "Generate or edit images and store them as persistent artifacts. "
            "Returns artifact ids and local paths. For edits, pass prior generated image paths "
            "or user image paths as reference_images."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Detailed image generation or edit prompt. Include style, subject, "
                        "composition, colors, and constraints."
                    ),
                },
                "reference_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional local image paths. Use generated artifact paths for iterative edits."
                    ),
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Optional output aspect ratio, e.g. 1:1, 16:9, 9:16, 4:3.",
                },
                "image_size": {
                    "type": "string",
                    "description": (
                        "Optional output size hint supported by the configured provider, "
                        "e.g. 1K, 2K, 4K, or 1024x1024."
                    ),
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Number of images to generate in this turn.",
                },
            },
            "required": ["prompt"],
        }

    def _provider_config(self) -> ProviderConfig | None:
        return self.provider_configs.get(self.config.provider)

    def _provider_client(self):
        provider_cls = get_image_gen_provider(self.config.provider)
        if provider_cls is None:
            return None
        provider_config = self._provider_config()
        return provider_cls(
            api_key=provider_config.api_key if provider_config else None,
            api_base=provider_config.api_base if provider_config else None,
            extra_headers=provider_config.extra_headers if provider_config else None,
            extra_body=provider_config.extra_body if provider_config else None,
        )

    def _resolve_reference_image(self, value: str) -> str:
        access = current_tool_workspace(self.workspace, restrict_to_workspace=True)
        workspace = access.project_path or self.workspace
        try:
            resolved = resolve_allowed_path(
                value,
                workspace=workspace,
                allowed_root=access.allowed_root,
                extra_allowed_roots=[get_media_dir()] if access.allowed_root is not None else None,
                strict=True,
            )
        except WorkspaceBoundaryError as exc:
            raise ImageGenerationError(
                "reference_images must be inside the workspace or YuanClaw media directory"
            ) from exc
        except OSError as exc:
            raise ImageGenerationError(f"reference image not found: {value}") from exc
        if not resolved.is_file():
            raise ImageGenerationError(f"reference image is not a file: {value}")
        if resolved.stat().st_size > MAX_REFERENCE_IMAGE_BYTES:
            raise ImageGenerationError(
                f"reference image exceeds {MAX_REFERENCE_IMAGE_BYTES} bytes: {value}"
            )
        with open(resolved, "rb") as handle:
            raw = handle.read(512)
        if detect_image_mime(raw) is None:
            raise ImageGenerationError(f"unsupported reference image: {value}")
        return str(resolved)

    def _resolve_reference_images(self, values: list[str] | None) -> list[str]:
        if not values:
            return []
        return [self._resolve_reference_image(value) for value in values if value]

    async def execute(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int | None = None,
        **_kwargs: Any,
    ) -> str:
        client = self._provider_client()
        if client is None:
            return f"Error: unsupported image generation provider '{self.config.provider}'"

        requested = count or 1
        if requested > self.config.max_images_per_turn:
            return (
                "Error: count exceeds tools.imageGeneration.maxImagesPerTurn "
                f"({self.config.max_images_per_turn})"
            )

        try:
            refs = self._resolve_reference_images(reference_images)
            artifacts: list[dict[str, Any]] = []
            while len(artifacts) < requested:
                response = await client.generate(
                    prompt=prompt,
                    model=self.config.model,
                    reference_images=refs,
                    aspect_ratio=aspect_ratio or self.config.default_aspect_ratio,
                    image_size=image_size or self.config.default_image_size,
                )
                if not response.images:
                    raise ImageGenerationError(
                        f"{self.config.provider} returned no images for this request"
                    )
                for image_data_url in response.images:
                    artifact = store_generated_image_artifact(
                        image_data_url,
                        prompt=prompt,
                        model=self.config.model,
                        source_images=refs,
                        save_dir=self.config.save_dir,
                        provider=self.config.provider,
                    )
                    artifacts.append(artifact)
                    if len(artifacts) >= requested:
                        break
            return generated_image_tool_result(artifacts)
        except (ArtifactError, ImageGenerationError, OSError) as exc:
            return f"Error: {exc}"
