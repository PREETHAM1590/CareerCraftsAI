"""Supported product providers and their Nango integration keys."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDefinition:
    key: str
    nango_provider: str


# `nango_provider` is the documented Nango catalog provider.  A
# `provider_config_key` is different: it is an environment-specific Nango
# integration unique key and must be configured by the deployer.
SUPPORTED_PROVIDERS: dict[str, ProviderDefinition] = {
    "github": ProviderDefinition("github", "github"),
    "gmail": ProviderDefinition("gmail", "google-mail"),
    # Same Google provider, separate Nango integration whose OAuth scopes are
    # only gmail.send + email: outreach without inbox access.
    "gmail_send": ProviderDefinition("gmail_send", "google-mail"),
    "google_drive": ProviderDefinition("google_drive", "google-drive"),
    "google_calendar": ProviderDefinition("google_calendar", "google-calendar"),
    "outlook_mail": ProviderDefinition("outlook_mail", "outlook"),
    "outlook_calendar": ProviderDefinition("outlook_calendar", "outlook"),
}


def provider_definition(provider: str) -> ProviderDefinition:
    try:
        return SUPPORTED_PROVIDERS[provider]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(
            f"Unsupported integration provider: {provider}. Supported: {supported}"
        ) from exc


def provider_config_key(provider: str, configured_keys: dict[str, str]) -> str:
    provider_definition(provider)
    value = configured_keys.get(provider)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Nango provider config key is not configured for {provider}")
    return value.strip()


def provider_for_config_key(
    provider_config_key_value: str, configured_keys: dict[str, str]
) -> str | None:
    for provider in SUPPORTED_PROVIDERS:
        if configured_keys.get(provider) == provider_config_key_value:
            return provider
    return None
