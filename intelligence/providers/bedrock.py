"""Amazon Bedrock provider (Converse API). Requires AWS credentials with Bedrock access.

Newer models are only invokable on demand through an inference profile, so the model id
carries a geo prefix (us./eu./apac.). The default is Amazon's own Nova 2 Lite; set
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 (for example) to swap."""
import os

from intelligence.providers.base import Provider, ProviderUnavailable
from observability import tracing

DEFAULT_MODEL = "us.amazon.nova-2-lite-v1:0"


class BedrockProvider(Provider):
    name = "bedrock"

    def __init__(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:  # optional dependency
            raise ProviderUnavailable("pip install boto3 to use LLM_PROVIDER=bedrock") from e
        self.model = os.getenv("BEDROCK_MODEL_ID") or DEFAULT_MODEL
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.client = boto3.client("bedrock-runtime", region_name=self.region,
                                   config=Config(read_timeout=20, retries={"max_attempts": 2, "mode": "standard"}))

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self.client.converse(
                modelId=self.model, system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": 600, "temperature": 0})
        except Exception as e:
            raise ProviderUnavailable(f"{type(e).__name__}: {e}") from e
        usage = resp.get("usage", {})
        tracing.generation_usage(self.model, {"input": usage.get("inputTokens", 0),
                                              "output": usage.get("outputTokens", 0)},
                                 {"provider": "bedrock", "region": self.region,
                                  "latency_ms": resp.get("metrics", {}).get("latencyMs"),
                                  "stop_reason": resp.get("stopReason")})
        return "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
