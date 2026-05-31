"""Image generation provider abstraction and registry."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from yuanclaw.config.schema import Config, ProviderConfig
from yuanclaw.utils.helpers import detect_image_mime


class ImageGenerationError(RuntimeError):
    """Raised for image generation configuration or provider failures."""


@dataclass
class ImageGenerationResponse:
    """Image generation result."""

    images: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


class ImageGenerationProvider(Protocol):
    """Protocol implemented by image generation providers."""

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> ImageGenerationResponse:
        ...


_IMAGE_GEN_PROVIDERS: dict[str, type] = {}


def image_path_to_data_url(path: str | Path) -> str:
    """Encode a local image file as a data URL."""
    raw = Path(path).expanduser().read_bytes()
    mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
    if not mime or not mime.startswith("image/"):
        raise ImageGenerationError(f"unsupported reference image: {path}")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class OpenRouterImageGenerationClient:
    """Small async client for OpenRouter Chat Completions image generation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.api_base = (api_base or "https://openrouter.ai/api/v1").rstrip("/")
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.timeout = timeout

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> ImageGenerationResponse:
        if not self.api_key:
            raise ImageGenerationError(
                "OpenRouter API key is not configured. Set providers.openrouter.apiKey."
            )

        content: str | list[dict[str, Any]]
        references = list(reference_images or [])
        if references:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            blocks.extend(
                {"type": "image_url", "image_url": {"url": image_path_to_data_url(path)}}
                for path in references
            )
            content = blocks
        else:
            content = prompt

        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "stream": False,
        }
        image_config: dict[str, str] = {}
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        if image_size:
            image_config["image_size"] = image_size
        if image_config:
            body["image_config"] = image_config
        body.update(self.extra_body)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=body,
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:500]
            raise ImageGenerationError(f"OpenRouter image generation failed: {detail}") from exc

        data = response.json()
        images: list[str] = []
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            for image in message.get("images") or []:
                if not isinstance(image, dict):
                    continue
                image_url = image.get("image_url") or image.get("imageUrl") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if isinstance(url, str) and url.startswith("data:image/"):
                    images.append(url)

        if not images:
            raise ImageGenerationError("OpenRouter returned no images for this request")
        return ImageGenerationResponse(images=images, metadata={"raw": data})


def register_image_gen_provider(name: str, provider_cls: type) -> None:
    """Register an image generation provider class."""
    _IMAGE_GEN_PROVIDERS[name.strip().lower().replace("-", "_")] = provider_cls


def get_image_gen_provider(name: str) -> type | None:
    """Return a registered image generation provider class."""
    return _IMAGE_GEN_PROVIDERS.get(name.strip().lower().replace("-", "_"))


def image_gen_provider_configs(config: Config) -> dict[str, ProviderConfig]:
    """Return provider configs usable by image generation tools."""
    return {
        name: value
        for name, value in vars(config.providers).items()
        if isinstance(value, ProviderConfig)
    }


register_image_gen_provider("openrouter", OpenRouterImageGenerationClient)
