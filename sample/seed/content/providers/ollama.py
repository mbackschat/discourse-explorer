"""Ollama provider — POSTs to `/api/generate` with `stream=false`.

Uses `requests` (already a project dependency) to match the rest of the
codebase's HTTP patterns. The provider is intentionally thin — caching,
retries, and prompt-shape concerns live one layer up in the body generator
(Sit 10).
"""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class OllamaProvider:
    """Concrete `Provider` for a local (or remote) Ollama daemon.

    `host` is the base URL (`http://localhost:11434` is the project default,
    matching `discourse_explorer/config.py`). `model` is passed straight
    through — caller is responsible for ensuring it's been pulled.
    """

    host: str
    model: str
    timeout: float = 60.0

    def generate(self, prompt: str) -> str:
        url = f"{self.host.rstrip('/')}/api/generate"
        response = requests.post(
            url,
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["response"].strip()
