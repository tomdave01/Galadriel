"""Optional Azure AI Translator integration for narration text."""

from __future__ import annotations

import os
import re

from azure.identity import DefaultAzureCredential

_TARGET_LANGUAGES = {"Spanish": "es", "Arabic": "ar", "French": "fr"}


def translation_enabled() -> bool:
    return os.getenv("AZURE_TRANSLATOR_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def translate_narration(text: str, language: str, names: list[str]) -> str:
    """Translate a line while shielding known club/player names from translation."""
    target = _TARGET_LANGUAGES.get(language)
    if target is None:
        return text

    endpoint = os.getenv("AZURE_TEXT_TRANSLATION_ENDPOINT")
    region = os.getenv("AZURE_TEXT_TRANSLATION_REGION")
    if not endpoint or not region:
        raise RuntimeError(
            "Azure Translator is enabled but endpoint or region is missing"
        )

    from azure.ai.translation.text import TextTranslationClient

    placeholders: dict[str, str] = {}
    protected_text = text
    unique_names = sorted({name for name in names if name}, key=len, reverse=True)
    for index, name in enumerate(unique_names):
        token = f"OVERSEERNAME{index}TOKEN"
        protected_text = re.sub(re.escape(name), token, protected_text)
        placeholders[token] = name

    credential = DefaultAzureCredential()
    client = TextTranslationClient(
        endpoint=endpoint,
        credential=credential,
        region=region,
    )
    try:
        response = client.translate(
            body=[protected_text],
            to_language=[target],
            from_language="en",
        )
        translated = response[0].translations[0].text
        for token, name in placeholders.items():
            translated = translated.replace(token, name)
        return translated
    finally:
        client.close()
        credential.close()
