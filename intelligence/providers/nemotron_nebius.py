"""Nebius Token Factory provider (OpenAI-compatible chat completions) for NVIDIA Nemotron models."""
import os

import httpx

from intelligence.providers.base import Provider, ProviderUnavailable
from observability import tracing

DEFAULT_BASE = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"


class NebiusProvider(Provider):
    name = "nebius"

    def __init__(self):
        self.key = os.getenv("NEBIUS_API_KEY")
        if not self.key:
            raise ProviderUnavailable("NEBIUS_API_KEY is not set")
        self.base = (os.getenv("NEBIUS_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.model = os.getenv("NEBIUS_PARSER_MODEL") or DEFAULT_MODEL
        self.json_mode = True  # dropped once if the model rejects response_format

    def _post(self, body: dict) -> httpx.Response:
        return httpx.post(f"{self.base}/chat/completions", timeout=30,
                          headers={"Authorization": f"Bearer {self.key}"}, json=body)

    def complete(self, system: str, user: str) -> str:
        body = {"model": self.model, "temperature": 0, "max_tokens": 1200,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = self._post({**body, "response_format": {"type": "json_object"}} if self.json_mode else body)
            if r.status_code == 400 and self.json_mode and "response_format" in r.text:
                self.json_mode = False
                r = self._post(body)
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage") or {}
            tracing.generation_usage(self.model, {"input": usage.get("prompt_tokens", 0),
                                                  "output": usage.get("completion_tokens", 0)},
                                     {"provider": "nebius-token-factory", "json_mode": self.json_mode})
            return data["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as e:
            raise ProviderUnavailable(f"HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise ProviderUnavailable(f"{type(e).__name__}: {e}") from e
