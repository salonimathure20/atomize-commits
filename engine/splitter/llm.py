"""Talk to a language model, whichever one the user has.

DESIGN NOTES
------------
1. No third-party HTTP library. urllib is in the standard library and this
   project stays installable with zero dependencies beyond networkx.

2. Bring your own key. The user supplies credentials through environment
   variables, so running this costs the project nothing. That is also how
   every serious editor extension handles it.

3. Local models are first class. Ollama runs on your laptop, costs nothing,
   and never sends your source code anywhere. It is the default.

4. Everything degrades. If no provider is configured, or the call fails, or
   the model returns nonsense, we fall back to the heuristic message. The
   tool must never break because a network call did.

Configure with:
    SPLITTER_LLM=ollama|anthropic|openai|gemini|none
    SPLITTER_MODEL=<model name>              (optional, sensible defaults)
    ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
    OLLAMA_HOST=http://localhost:11434       (optional)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

TIMEOUT = 60

# Anything matching these gets masked before it can reach a terminal, a log
# file, or a bug report. Credentials leak through error messages far more
# often than through code.
SECRET_QUERY_PARAMS = ("key", "api_key", "access_token")


class LLMError(RuntimeError):
    pass


class Provider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str:
        ...


def redact(text: str) -> str:
    """Mask credentials in a string before it is shown to anyone."""
    import re

    for param in SECRET_QUERY_PARAMS:
        text = re.sub(rf"([?&]{param}=)[^&\s]+", r"\1***", text)
    # Long opaque tokens that appear bare, e.g. pasted into a message.
    text = re.sub(r"\b(sk-|AIza|AQ\.)[A-Za-z0-9_\-]{12,}", r"\1***", text)
    return text


def _post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    safe_url = redact(url)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode(errors="replace")[:300])
        raise LLMError(f"{exc.code} from {safe_url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"could not reach {safe_url}: {redact(str(exc))}") from exc


class OllamaProvider:
    """A model running on your own machine. Free, private, no key needed."""

    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None):
        self.model = model or os.environ.get("SPLITTER_MODEL", "qwen2.5-coder:7b")
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")

    def complete(self, system: str, user: str) -> str:
        data = _post(
            f"{self.host}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "options": {"temperature": 0.2},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {"Content-Type": "application/json"},
        )
        return data.get("message", {}).get("content", "")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("SPLITTER_MODEL", "claude-haiku-4-5-20251001")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")

    def complete(self, system: str, user: str) -> str:
        data = _post(
            "https://api.anthropic.com/v1/messages",
            {
                "model": self.model,
                "max_tokens": 1500,
                "temperature": 0.2,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts)


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("SPLITTER_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set")

    def complete(self, system: str, user: str) -> str:
        data = _post(
            "https://api.openai.com/v1/chat/completions",
            {
                "model": self.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        return data["choices"][0]["message"]["content"]


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("SPLITTER_MODEL", "gemini-3.6-flash")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")

    def complete(self, system: str, user: str) -> str:
        # The key goes in a header, not the query string. Query strings end up
        # in shell history, proxy logs, and error messages.
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        data = _post(
            url,
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.2},
            },
            {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
        )
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)


class ScriptedProvider:
    """Returns canned replies. Used by the tests so they need no network."""

    name = "scripted"

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.replies.pop(0) if self.replies else ""


PROVIDERS = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def provider_from_env() -> Provider | None:
    """Build a provider from environment variables, or None if disabled."""
    choice = os.environ.get("SPLITTER_LLM", "").strip().lower()
    if choice in ("", "none", "off"):
        return None
    if choice not in PROVIDERS:
        raise LLMError(
            f"unknown SPLITTER_LLM={choice!r}; expected one of "
            + ", ".join(sorted(PROVIDERS)) + ", or none"
        )
    return PROVIDERS[choice]()


def extract_json(text: str):
    """Pull a JSON value out of a model reply.

    Models like to wrap JSON in prose or fenced code blocks even when told not
    to, so we scan for the outermost bracketed region rather than trusting the
    response to be clean.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]

    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("model did not return usable JSON")
