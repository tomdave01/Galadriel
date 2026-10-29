"""Compatibility import for the shared Microsoft Foundry client."""

from agent_framework.foundry import FoundryChatClient

from llm.foundry import get_client


def create_foundry_chat_client() -> FoundryChatClient:
    """Return the shared Entra-authenticated Foundry client."""
    return get_client()
