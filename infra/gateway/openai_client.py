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
    """Return the default deployment name."""
    return os.environ["AZURE_OPENAI_DEPLOYMENT"]


def get_connect_deployment() -> str:
    """Return the deployment name for Connect evaluations (GPT-5.3-chat).

    Falls back to the default deployment if not set.
    """
    return os.environ.get("AZURE_OPENAI_CONNECT_DEPLOYMENT", get_deployment())


def chat_completion(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    deployment: str | None = None,
    temperature: float | None = None,
) -> dict:
    """Make a chat completion call and return structured result.

    Args:
        system_prompt: The system message content.
        user_prompt: The user message content.
        max_tokens: Maximum tokens for the completion.
        deployment: Override deployment name (defaults to AZURE_OPENAI_DEPLOYMENT).
        temperature: Override temperature (omitted if None, letting API use default).

    Returns:
        dict with keys `text` (str) and `usage` (dict with model/token counts).
    """
    client = get_client()
    model = deployment or get_deployment()

    kwargs: dict = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "model": model,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = client.chat.completions.create(**kwargs)

    text = response.choices[0].message.content or ""
    usage = {
        "model": response.model or model,
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        "total_tokens": response.usage.total_tokens if response.usage else 0,
    }
    return {"text": text.strip(), "usage": usage}


def chat_completion_with_tools(
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 2000,
    deployment: str | None = None,
    temperature: float | None = None,
) -> dict:
    """Make a chat completion call with tool-calling support.

    Args:
        messages: Full messages array (system, user, assistant, tool).
        tools: OpenAI function-calling tool definitions.
        max_tokens: Maximum tokens for the completion.
        deployment: Override deployment name.
        temperature: Override temperature.

    Returns:
        dict with keys:
            - ``message``: The assistant's response message dict
              (content, tool_calls, role).
            - ``usage``: Token usage dict.
    """
    client = get_client()
    model = deployment or get_deployment()

    kwargs: dict = {
        "messages": messages,
        "max_tokens": max_tokens,
        "model": model,
    }
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = client.chat.completions.create(**kwargs)

    msg = response.choices[0].message
    result_message: dict = {"role": "assistant", "content": msg.content or ""}

    if msg.tool_calls:
        result_message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]

    usage = {
        "model": response.model or model,
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": (
            response.usage.completion_tokens if response.usage else 0
        ),
        "total_tokens": response.usage.total_tokens if response.usage else 0,
    }
    return {"message": result_message, "usage": usage}
