"""LLM provider abstraction module."""

from yuanclaw.providers.base import LLMProvider, LLMResponse

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "FallbackProvider",
    "LiteLLMProvider",
    "OpenAICompatibleProvider",
    "OpenAICodexProvider",
    "AzureOpenAIProvider",
    "BedrockProvider",
]

_EXPORTS = {
    "AnthropicProvider": ("yuanclaw.providers.anthropic_provider", "AnthropicProvider"),
    "AzureOpenAIProvider": ("yuanclaw.providers.azure_openai_provider", "AzureOpenAIProvider"),
    "BedrockProvider": ("yuanclaw.providers.bedrock_provider", "BedrockProvider"),
    "FallbackProvider": ("yuanclaw.providers.fallback_provider", "FallbackProvider"),
    "LiteLLMProvider": ("yuanclaw.providers.litellm_provider", "LiteLLMProvider"),
    "OpenAICodexProvider": ("yuanclaw.providers.openai_codex_provider", "OpenAICodexProvider"),
    "OpenAICompatibleProvider": (
        "yuanclaw.providers.openai_compatible_provider",
        "OpenAICompatibleProvider",
    ),
}


def __getattr__(name: str) -> object:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'yuanclaw.providers' has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    from importlib import import_module

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
