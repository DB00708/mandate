"""Speech -> schema-bound Proposal. Strict validation; retry once; then fail closed."""
import json
from datetime import date

from pydantic import ValidationError

from intelligence.parser.schema import SCHEMA_PROMPT, Proposal
from intelligence.providers.base import Provider, ProviderUnavailable, get_provider


class ParseFailed(Exception):
    """The model could not produce a valid proposal; the user is asked to repeat."""


class FaultyProvider(Provider):
    """Test double: returns malformed JSON for the first `bad` calls, then delegates."""
    name = "faulty"

    def __init__(self, inner: Provider, bad: int):
        self.inner, self.bad = inner, bad

    def complete(self, system, user):
        if self.bad > 0:
            self.bad -= 1
            return '{"intent": "purchase", "items": [{"category": "milk", "qty": "lots"'  # truncated / wrong type
        return self.inner.complete(system, user)


def _extract_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end != -1 else raw


def parse(text: str, today: date, members: list[str], provider: Provider | None = None) -> tuple[Proposal, int]:
    """Return (proposal, attempts). Raises ParseFailed after two invalid outputs."""
    provider = provider or get_provider()
    user = json.dumps({"text": text, "today": today.isoformat(), "members": members})
    last_err = None
    for attempt in (1, 2):
        try:
            raw = provider.complete(SCHEMA_PROMPT, user)
            return Proposal.model_validate_json(_extract_json(raw)), attempt
        except (ValidationError, ValueError, ProviderUnavailable) as e:
            last_err = e
    raise ParseFailed(str(last_err)[:300])
