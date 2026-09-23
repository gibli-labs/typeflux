from typeflux.providers.anthropic import AnthropicProvider
from typeflux.providers.base import (
    AsyncModelProvider,
    ModelProvider,
    ProviderUsage,
    provider_default_params,
    register_provider_artifact_support,
    register_provider_param_support,
    supported_artifact_kinds,
    supported_provider_params,
    validate_artifact_kinds_supported,
    validate_provider_params_supported,
)
from typeflux.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderPolicyError,
    ProviderRateLimitError,
    ProviderTransientError,
)
from typeflux.providers.gemini import GeminiProvider
from typeflux.providers.openai import OpenAIProvider

register_provider_param_support(
    AnthropicProvider.provider_name,
    AnthropicProvider.supported_provider_params,
)
register_provider_param_support(
    OpenAIProvider.provider_name,
    OpenAIProvider.supported_provider_params,
)
register_provider_param_support(
    GeminiProvider.provider_name,
    GeminiProvider.supported_provider_params,
)
register_provider_artifact_support(
    AnthropicProvider.provider_name,
    AnthropicProvider.supported_artifact_kinds,
)
register_provider_artifact_support(
    OpenAIProvider.provider_name,
    OpenAIProvider.supported_artifact_kinds,
)
register_provider_artifact_support(
    GeminiProvider.provider_name,
    GeminiProvider.supported_artifact_kinds,
)

__all__ = [
    "AnthropicProvider",
    "AsyncModelProvider",
    "GeminiProvider",
    "ModelProvider",
    "OpenAIProvider",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderPolicyError",
    "ProviderRateLimitError",
    "ProviderTransientError",
    "ProviderUsage",
    "provider_default_params",
    "register_provider_artifact_support",
    "register_provider_param_support",
    "supported_artifact_kinds",
    "supported_provider_params",
    "validate_artifact_kinds_supported",
    "validate_provider_params_supported",
]
