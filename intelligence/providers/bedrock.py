"""Amazon Bedrock provider (Converse API). Requires boto3 and AWS credentials."""
import os

from intelligence.providers.base import Provider, ProviderUnavailable


class BedrockProvider(Provider):
    name = "bedrock"

    def __init__(self):
        try:
            import boto3
        except ImportError as e:  # optional dependency
            raise ProviderUnavailable("pip install boto3 to use LLM_PROVIDER=bedrock") from e
        self.model = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0")
        self.client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self.client.converse(
                modelId=self.model, system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": 600, "temperature": 0})
            return resp["output"]["message"]["content"][0]["text"]
        except Exception as e:
            raise ProviderUnavailable(str(e)) from e
