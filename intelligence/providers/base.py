"""LLM provider interface. Providers return raw text; the parser validates it."""
import os


class Provider:
    name = "base"

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class ProviderUnavailable(Exception):
    pass


def get_provider(name: str | None = None) -> Provider:
    name = (name or os.getenv("LLM_PROVIDER", "rules")).lower()
    if name == "bedrock":
        from intelligence.providers.bedrock import BedrockProvider
        return BedrockProvider()
    if name in ("nebius", "nemotron"):
        from intelligence.providers.nemotron_nebius import NebiusProvider
        return NebiusProvider()
    from intelligence.providers.rules import RulesProvider
    return RulesProvider()
