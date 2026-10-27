"""Optional Azure AI Content Safety check for generated narration."""

from __future__ import annotations

import os

from azure.identity import DefaultAzureCredential


_BLOCK_THRESHOLD = 4


def content_safety_enabled() -> bool:
    return os.getenv("AZURE_CONTENT_SAFETY_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def narrator_output_is_safe(text: str) -> bool:
    endpoint = os.getenv("AZURE_CONTENT_SAFETY_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError(
            "Azure Content Safety is enabled but AZURE_CONTENT_SAFETY_ENDPOINT is unset"
        )

    from azure.ai.contentsafety import ContentSafetyClient
    from azure.ai.contentsafety.models import AnalyzeTextOptions

    credential = DefaultAzureCredential()
    client = ContentSafetyClient(endpoint, credential)
    try:
        result = client.analyze_text(AnalyzeTextOptions(text=text))
        return all(
            (category.severity or 0) < _BLOCK_THRESHOLD
            for category in result.categories_analysis
        )
    finally:
        client.close()
        credential.close()
