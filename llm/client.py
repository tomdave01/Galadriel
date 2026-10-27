"""Small OpenAI-compatible chat client with a prompt-keyed disk cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        cache_dir: str | Path | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.model = model or os.getenv("LLM_MODEL", "")
        self.timeout = timeout
        configured_cache = cache_dir or os.getenv("LLM_CACHE_DIR")
        self.cache_dir = (
            Path(configured_cache)
            if configured_cache
            else Path.home() / ".cache" / "overseer" / "llm"
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        fallback_text: str,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """Return generated text, or a visibly marked fallback when unavailable."""
        cache_key = self._cache_key(prompt, system, json_mode)
        if os.getenv("LLM_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}:
            return {
                "text": fallback_text,
                "mock": True,
                "cached": False,
                "reason": "LLM_MOCK is enabled",
            }

        cached = self._read_cache(cache_key)
        if cached is not None:
            return {"text": cached, "mock": False, "cached": True, "reason": None}
        if not (self.base_url and self.api_key and self.model):
            return {
                "text": fallback_text,
                "mock": True,
                "cached": False,
                "reason": "LLM_BASE_URL, LLM_API_KEY, or LLM_MODEL is not configured",
            }

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = Request(
            self._completion_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            text = response_data["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("The LLM response did not contain message text")
            self._write_cache(cache_key, text)
            return {"text": text.strip(), "mock": False, "cached": False, "reason": None}
        except HTTPError as error:
            reason = f"LLM endpoint returned HTTP {error.code}"
        except (URLError, TimeoutError, OSError) as error:
            reason = f"LLM endpoint is unreachable: {error.__class__.__name__}"
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            reason = f"LLM response was invalid: {error.__class__.__name__}"
        return {"text": fallback_text, "mock": True, "cached": False, "reason": reason}

    def _completion_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _cache_key(self, prompt: str, system: str, json_mode: bool) -> str:
        content = json.dumps(
            [self.model, system, prompt, json_mode],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> str | None:
        path = self.cache_dir / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unable to read LLM cache entry {path}: {error}") from error
        text = data.get("text")
        if not isinstance(text, str):
            raise RuntimeError(f"Invalid LLM cache entry: {path}")
        return text

    def _write_cache(self, key: str, text: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{key}.", suffix=".tmp", dir=self.cache_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump({"text": text}, file, ensure_ascii=False)
            os.replace(temporary_name, self.cache_dir / f"{key}.json")
        except OSError as error:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise RuntimeError(f"Unable to write LLM cache: {error}") from error
