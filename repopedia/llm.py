"""Optional LLM client for synthesis (ask_codebase answers, wiki prose).

Portfolio-wide convention: any OpenAI-compatible endpoint, configured
exclusively via environment — no keys in code, configs, or the graph DB:

    REPOPEDIA_LLM_BASE_URL   e.g. https://api.openai.com/v1
    REPOPEDIA_LLM_API_KEY
    REPOPEDIA_LLM_MODEL      e.g. gpt-4o-mini

When unset, repopedia still works: ``ask_codebase`` returns structured
evidence without a synthesized answer, and the wiki generator emits a
deterministic structural wiki (tables + diagrams, no prose claims).
Zero new dependencies — plain urllib.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class LLMConfig:
    """Connection details for an OpenAI-compatible chat endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        base_url = os.environ.get("REPOPEDIA_LLM_BASE_URL", "").strip()
        api_key = os.environ.get("REPOPEDIA_LLM_API_KEY", "").strip()
        model = os.environ.get("REPOPEDIA_LLM_MODEL", "").strip()
        if not (base_url and api_key and model):
            return None
        return cls(base_url, api_key, model)


class LLMClient:
    """Minimal chat-completions client. ``complete`` returns message text."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @property
    def model(self) -> str:
        return self.config.model

    def complete(self, system: str, user: str, max_tokens: int = 1000,
                 temperature: float = 0.2) -> str:
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        req = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(
                f"LLM request failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
        try:
            return payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"unexpected LLM response shape: {str(payload)[:300]}") from exc


def llm_from_env() -> LLMClient | None:
    """LLMClient from environment, or None when not configured."""
    config = LLMConfig.from_env()
    return LLMClient(config) if config else None


class FakeLLM:
    """Test double with the LLMClient interface. Records prompts."""

    def __init__(self, reply: str = "synthesized answer") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    @property
    def model(self) -> str:
        return "fake-llm"

    def complete(self, system: str, user: str, **kwargs) -> str:
        self.calls.append((system, user))
        return self.reply
