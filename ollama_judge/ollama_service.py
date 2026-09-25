"""
Client for a locally running Ollama server.

Talks to the HTTP API directly rather than through the `ollama` pip package, so
the project has one fewer dependency and the base URL stays configurable.

Everything here degrades gracefully: if Ollama is not running, calls return
None quickly rather than raising, and the caller falls back to deterministic
templates. A local LLM being down must never take the phone system down.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from config import (
    OLLAMA_BASE_URL,
    OLLAMA_ENABLED,
    OLLAMA_MAX_TOKENS,
    OLLAMA_MODEL,
    OLLAMA_TEMPERATURE,
    OLLAMA_TIMEOUT,
)

logger = logging.getLogger(__name__)


class OllamaService:
    """Minimal, timeout-bounded wrapper over the Ollama chat API."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL,
                 model: str = OLLAMA_MODEL,
                 timeout: float = OLLAMA_TIMEOUT,
                 temperature: float = OLLAMA_TEMPERATURE,
                 enabled: bool = OLLAMA_ENABLED,
                 max_tokens: int = OLLAMA_MAX_TOKENS):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.enabled = enabled
        self.max_tokens = max_tokens
        # Cached health so one outage does not add a timeout to every turn.
        self._available: bool | None = None
        self._checked_at: float = 0.0
        self._recheck_after = 30.0

    # ------------------------------------------------------------------
    def is_available(self, force: bool = False) -> bool:
        """Is the server up and does it have the configured model?"""
        if not self.enabled:
            return False
        now = time.time()
        if (not force and self._available is not None
                and now - self._checked_at < self._recheck_after):
            return self._available

        self._checked_at = now
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=3.0)
            response.raise_for_status()
            names = [m.get("name", "") for m in response.json().get("models", [])]
            # "llama3.2" should match the installed "llama3.2:latest".
            self._available = any(
                n == self.model or n.split(":")[0] == self.model.split(":")[0]
                for n in names)
            if not self._available:
                logger.warning("Ollama is running but model %r is not pulled. "
                               "Available: %s. Run: ollama pull %s",
                               self.model, names, self.model)
        except Exception as exc:
            logger.info("Ollama unavailable (%s); using deterministic responses",
                        type(exc).__name__)
            self._available = False
        return self._available

    def list_models(self) -> list[str]:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=3.0)
            response.raise_for_status()
            return [m.get("name", "") for m in response.json().get("models", [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    def chat(self, system: str, user: str, examples: list[dict] | None = None,
             json_mode: bool = True) -> str | None:
        """
        One chat completion. Returns the raw text, or None on any failure.

        `json_mode` asks Ollama to constrain output to valid JSON, which small
        models otherwise struggle with.
        """
        if not self.is_available():
            return None

        messages = [{"role": "system", "content": system}]
        if examples:
            messages.extend(examples)
        messages.append({"role": "user", "content": user})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                # Cap the generation. A voice reply is one or two sentences;
                # without this a small model will happily ramble in JSON mode
                # until the request times out.
                "num_predict": self.max_tokens,
            },
        }
        if json_mode:
            payload["format"] = "json"

        try:
            response = httpx.post(f"{self.base_url}/api/chat", json=payload,
                                  timeout=self.timeout)
            response.raise_for_status()
            return response.json().get("message", {}).get("content", "")
        except httpx.TimeoutException:
            logger.warning("Ollama timed out after %ss", self.timeout)
            self._available = None          # re-check next call
            return None
        except Exception as exc:
            logger.error("Ollama call failed: %s", exc)
            self._available = None
            return None

    # ------------------------------------------------------------------
    def chat_json(self, system: str, user: str,
                  examples: list[dict] | None = None) -> dict | None:
        """Chat and parse the result as JSON, or None."""
        raw = self.chat(system, user, examples, json_mode=True)
        if not raw:
            return None
        return parse_json(raw)


# --------------------------------------------------------------------------
def parse_json(raw: str) -> dict | None:
    """
    Pull a JSON object out of a model response.

    Small models wrap JSON in prose or markdown fences even when asked not to,
    so we try the whole string first and then the outermost {...} block.
    """
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    logger.warning("could not parse model output as JSON: %.120s", text)
    return None
