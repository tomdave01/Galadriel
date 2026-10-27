"""Microsoft Foundry chat client construction."""

from __future__ import annotations

import logging
import os

from agent_framework.foundry import FoundryChatClient
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential, CredentialUnavailableError


logger = logging.getLogger(__name__)
# UNVERIFIED: confirm this token scope against the deployed Foundry project.
_FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def create_foundry_chat_client() -> FoundryChatClient:
    """Create the Foundry project client using Entra ID, with a key fallback."""
    project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    model = os.getenv("FOUNDRY_MODEL", "").strip()
    if not project_endpoint or not model:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL must be configured"
        )

    credential = DefaultAzureCredential()
    try:
        credential.get_token(_FOUNDRY_SCOPE)
    except (CredentialUnavailableError, ClientAuthenticationError) as error:
        api_key = os.getenv("FOUNDRY_API_KEY", "").strip()
        credential.close()
        if not api_key:
            raise RuntimeError(
                "Foundry authentication is unavailable and FOUNDRY_API_KEY is unset"
            ) from error
        logger.warning(
            "DefaultAzureCredential could not authenticate to Foundry; using the "
            "configured Foundry-only API-key fallback."
        )
        # UNVERIFIED: current FoundryChatClient docs expose default_headers but do
        # not document project-endpoint API-key authentication. Confirm the
        # `api-key` header is enabled for the target Foundry project before use.
        return FoundryChatClient(
            project_endpoint=project_endpoint,
            model=model,
            default_headers={"api-key": api_key},
        )

    return FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=credential,
    )
