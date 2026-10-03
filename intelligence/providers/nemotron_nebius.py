"""Nebius Token Factory provider (OpenAI-compatible chat completions) for Nemotron models."""
import os

import httpx

from intelligence.providers.base import Provider, ProviderUnavailable


class NebiusProvider(Provider):
    name = "nebius"

    def __init__(self):
        self.key = os.getenv("NEBIUS_API_KEY")
        if not self.key:
            raise ProviderUnavailable("NEBIUS_API_KEY is not set")
        self.base = os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.com/v1")
        self.model = os.getenv("NEBIUS_PARSER_MODEL", "nvidia/Llama-3_3-Nemotron-Super-49B-v1")

    def complete(self, system: str, user: str) -> str:
        try:
            r = httpx.post(f"{self.base}/chat/completions", timeout=20,
                           headers={"Authorization": f"Bearer {self.key}"},
                           json={"model": self.model, "temperature": 0,
                                 "response_format": {"type": "json_object"},
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError) as e:
            raise ProviderUnavailable(str(e)) from e
