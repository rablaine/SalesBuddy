"""
Azure OpenAI client for the NoteHelper AI Gateway.

Uses DefaultAzureCredential (→ system-assigned Managed Identity in Azure,
falls back to az-login / VS Code creds locally).
"""
import os
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

_client: AzureOpenAI | None = None


def get_client() -> AzureOpenAI:
    """Return a cached Azure OpenAI client authenticated via Managed Identity."""
    global _client
    if _client is None:
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        _client = AzureOpenAI(
            api_version=os.environ.get(
                "AZURE_OPENAI_API_VERSION", "2025-01-01-preview"
            ),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_ad_token_provider=token_provider,
        )
    return _client


def get_deployment() -> str:
    """Return the configured deployment name."""
    return os.environ["AZURE_OPENAI_DEPLOYMENT"]


def chat_completion(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
) -> dict:
    """Make a chat completion call and return structured result.

    Returns:
        dict with keys `text` (str) and `usage` (dict with model/token counts).
    """
    client = get_client()
    deployment = get_deployment()

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        model=deployment,
    )

    text = response.choices[0].message.content or ""
    usage = {
        "model": response.model or deployment,
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        "total_tokens": response.usage.total_tokens if response.usage else 0,
    }
    return {"text": text.strip(), "usage": usage}
