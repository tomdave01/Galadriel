"""Shared Microsoft Foundry client and Agent Framework helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import os
from threading import Lock
from typing import Any

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from telemetry import traced


load_dotenv()

_client: FoundryChatClient | None = None
_client_lock = Lock()


def get_client() -> FoundryChatClient:
    """Lazily create the process-wide Foundry chat client."""
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        if _client is not None:
            return _client
        project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
        model = os.getenv("FOUNDRY_MODEL", "").strip()
        if not project_endpoint:
            raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT must be configured")
        if not project_endpoint.startswith("https://"):
            raise ValueError("FOUNDRY_PROJECT_ENDPOINT must start with 'https://'")
        if not model:
            raise RuntimeError(
                "FOUNDRY_MODEL must be configured with the Foundry deployment name"
            )

        _client = FoundryChatClient(
            project_endpoint=project_endpoint,
            model=model,
            credential=DefaultAzureCredential(),
        )
        return _client


def make_agent(
    name: str,
    instructions: str,
    tools: Sequence[Any] | None = None,
) -> Agent:
    """Create an Agent Framework agent backed by the shared Foundry client."""
    return Agent(
        client=get_client(),
        name=name,
        instructions=instructions,
        tools=tools or [],
    )


async def ask(
    agent: Any,
    prompt: str,
    timeout: float = 6.0,
    fallback: str = "",
    *,
    agent_name: str | None = None,
    fact_ids: Sequence[str] = (),
) -> str:
    """Run an agent with a deadline, returning its text or the fallback."""
    try:
        name = agent_name or str(getattr(agent, "name", "agent"))

        async def run_agent() -> Any:
            return await agent.run(prompt)

        result = await asyncio.wait_for(
            traced(name, fact_ids, run_agent()),
            timeout=timeout,
        )
        return str(result)
    except Exception as error:
        print(f"[foundry] fell back: {error.__class__.__name__}")
        return fallback
