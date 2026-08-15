"""OpenAI provider — single chat-completion call via the official SDK.

Mirrors `discourse_explorer/config.py` env conventions: `OPENAI_API_KEY` is
the credential and `EXTRACTION_MODEL` (with default `gpt-4.1-mini`) pins the
model. The provider is intentionally thin — caching, retries, and prompt
shape live one layer up in the body generator (Sit 10), so this class just
wraps a single `chat.completions.create` round-trip and returns the message
content.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpenAIProvider:
    """Concrete `Provider` for the OpenAI Chat Completions API.

    `api_key` is the OpenAI credential — passed through to the SDK client
    rather than read from env at call time so a caller can construct
    multiple providers from different keys in the same process. `model`
    is whatever the parent project's `EXTRACTION_MODEL` resolves to;
    `gpt-4.1-mini` is the project-wide default (see
    `discourse_explorer.config.OPENAI_EXTRACTION_MODEL`).
    """

    api_key: str
    model: str

    def generate(self, prompt: str) -> str:
        # Local import keeps `openai` off the import path for pure-construction
        # unit tests and for callers that select Ollama instead.
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content or ""
        return content.strip()
