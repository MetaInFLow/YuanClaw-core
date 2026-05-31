"""Create LLM providers from YuanClaw config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from yuanclaw.config.schema import Config, InlineFallbackConfig, ModelPresetConfig
from yuanclaw.providers.base import LLMProvider
from yuanclaw.providers.fallback_provider import FallbackProvider
from yuanclaw.providers.registry import find_by_name


@dataclass(frozen=True)
class ProviderSnapshot:
    provider: LLMProvider
    model: str
    context_window_tokens: int
    signature: tuple[object, ...]


_NATIVE_OPENAI_COMPATIBLE_PROVIDERS = {
    "openrouter",
    "huggingface",
    "skywork",
    "aihubmix",
    "siliconflow",
    "novita",
    "volcengine",
    "volcengine_coding_plan",
    "byteplus",
    "byteplus_coding_plan",
    "stepfun",
    "xiaomi_mimo",
    "longcat",
    "ant_ling",
    "lm_studio",
    "atomic_chat",
    "nvidia",
    "qianfan",
}


def _resolve_model_preset(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ModelPresetConfig:
    return preset if preset is not None else config.resolve_preset(preset_name)


def _make_provider_core(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Create a plain provider without failover wrapping."""
    from yuanclaw.providers.anthropic_provider import AnthropicProvider
    from yuanclaw.providers.azure_openai_provider import AzureOpenAIProvider
    from yuanclaw.providers.bedrock_provider import BedrockProvider
    from yuanclaw.providers.custom_provider import CustomProvider
    from yuanclaw.providers.litellm_provider import LiteLLMProvider
    from yuanclaw.providers.openai_codex_provider import OpenAICodexProvider
    from yuanclaw.providers.openai_compatible_provider import OpenAICompatibleProvider

    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    model = model or resolved.model
    allow_model_prefix_routing = resolved.provider == "auto"
    provider_name = config.get_provider_name(model, preset=resolved)
    p = config.get_provider(model, preset=resolved)

    if provider_name == "openai_codex" or (
        allow_model_prefix_routing and model.startswith("openai-codex/")
    ):
        provider = OpenAICodexProvider(default_model=model)
    elif (
        provider_name == "anthropic"
        or (allow_model_prefix_routing and model.startswith("anthropic/"))
    ) and p and p.api_key:
        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model, preset=resolved),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    elif provider_name == "custom":
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model, preset=resolved) or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    elif provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            logger.warning("Azure OpenAI config incomplete, falling back to local custom provider")
            provider = CustomProvider(
                api_key="no-key",
                api_base="http://localhost:8000/v1",
                default_model=model,
            )
        else:
            provider = AzureOpenAIProvider(
                api_key=p.api_key,
                api_base=p.api_base,
                default_model=model,
            )
    elif provider_name == "bedrock" or (
        allow_model_prefix_routing and model.startswith("bedrock/")
    ):
        provider = BedrockProvider(
            api_key=p.api_key if p and p.api_key else None,
            api_base=config.get_api_base(model, preset=resolved),
            default_model=model,
            region=p.region if p else None,
            profile=p.profile if p else None,
            extra_body=p.extra_body if p else None,
        )
    elif provider_name == "ovms":
        provider = CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model, preset=resolved) or "http://localhost:8000/v3",
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        spec = find_by_name(provider_name) if provider_name else None
        if not (p and p.api_key) and not (
            spec and (spec.is_oauth or spec.is_local)
        ):
            logger.warning("No API key configured, chat responses may fail until provider is configured")
            provider = CustomProvider(
                api_key="no-key",
                api_base=config.get_api_base(model, preset=resolved) or "http://localhost:8000/v1",
                default_model=model,
            )
        elif (
            spec
            and provider_name in _NATIVE_OPENAI_COMPATIBLE_PROVIDERS
            and config.get_api_base(model, preset=resolved)
        ):
            provider = OpenAICompatibleProvider(
                api_key=p.api_key if p else "no-key",
                api_base=config.get_api_base(model, preset=resolved) or "http://localhost:8000/v1",
                default_model=model,
                provider_name=provider_name,
                extra_headers=p.extra_headers if p else None,
                extra_body=p.extra_body if p else None,
                strip_model_prefix=spec.strip_model_prefix,
            )
        else:
            provider = LiteLLMProvider(
                api_key=p.api_key if p else None,
                api_base=config.get_api_base(model, preset=resolved),
                default_model=model,
                extra_headers=p.extra_headers if p else None,
                provider_name=provider_name,
            )

    provider.generation = resolved.to_generation_settings()
    return provider


def _inline_fallback_preset(
    primary: ModelPresetConfig,
    fallback: InlineFallbackConfig,
) -> ModelPresetConfig:
    return ModelPresetConfig(
        model=fallback.model,
        provider=fallback.provider,
        max_tokens=fallback.max_tokens if fallback.max_tokens is not None else primary.max_tokens,
        context_window_tokens=(
            fallback.context_window_tokens
            if fallback.context_window_tokens is not None
            else primary.context_window_tokens
        ),
        temperature=(
            fallback.temperature if fallback.temperature is not None else primary.temperature
        ),
        reasoning_effort=fallback.reasoning_effort,
    )


def _resolve_fallback_presets(config: Config, primary: ModelPresetConfig) -> list[ModelPresetConfig]:
    presets: list[ModelPresetConfig] = []
    for fallback in config.agents.defaults.fallback_models:
        if isinstance(fallback, str):
            presets.append(config.model_presets[fallback])
        else:
            if isinstance(fallback, dict):
                fallback = InlineFallbackConfig.model_validate(fallback)
            presets.append(_inline_fallback_preset(primary, fallback))
    return presets


def make_provider(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Create the configured provider, wrapping it with fallback support when configured."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    provider = _make_provider_core(config, preset_name=preset_name, preset=resolved, model=model)
    fallback_presets = _resolve_fallback_presets(config, resolved)
    if not fallback_presets:
        return provider
    return FallbackProvider(
        primary=provider,
        fallback_presets=fallback_presets,
        provider_factory=lambda fb: _make_provider_core(config, preset_name=preset_name, preset=fb),
    )


def provider_signature(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> tuple[object, ...]:
    """Return config fields that affect the active provider chain."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    p = config.get_provider(resolved.model, preset=resolved)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    def _fallback_signature(fallback: ModelPresetConfig) -> tuple[object, ...]:
        fp = config.get_provider(fallback.model, preset=fallback)
        return (
            fallback.model,
            fallback.provider,
            config.get_provider_name(fallback.model, preset=fallback),
            config.get_api_key(fallback.model, preset=fallback),
            config.get_api_base(fallback.model, preset=fallback),
            fp.extra_headers if fp else None,
            fp.extra_body if fp else None,
            fp.api_type if fp else "auto",
            getattr(fp, "region", None) if fp else None,
            getattr(fp, "profile", None) if fp else None,
            fallback.max_tokens,
            fallback.temperature,
            fallback.reasoning_effort,
            fallback.context_window_tokens,
        )

    return (
        resolved.model,
        resolved.provider,
        config.get_provider_name(resolved.model, preset=resolved),
        config.get_api_key(resolved.model, preset=resolved),
        config.get_api_base(resolved.model, preset=resolved),
        p.extra_headers if p else None,
        p.extra_body if p else None,
        p.api_type if p else "auto",
        getattr(p, "region", None) if p else None,
        getattr(p, "profile", None) if p else None,
        resolved.max_tokens,
        resolved.temperature,
        resolved.reasoning_effort,
        resolved.context_window_tokens,
        tuple(_fallback_signature(fallback) for fallback in fallback_presets),
    )


def build_provider_snapshot(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ProviderSnapshot:
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    fallback_windows = [
        fallback.context_window_tokens
        for fallback in _resolve_fallback_presets(config, resolved)
    ]
    return ProviderSnapshot(
        provider=make_provider(config, preset=resolved),
        model=resolved.model,
        context_window_tokens=min([resolved.context_window_tokens, *fallback_windows]),
        signature=provider_signature(config, preset=resolved),
    )


def load_provider_snapshot(
    config_path: Path | None = None,
    *,
    preset_name: str | None = None,
) -> ProviderSnapshot:
    from yuanclaw.config.loader import load_config

    return build_provider_snapshot(load_config(config_path), preset_name=preset_name)
