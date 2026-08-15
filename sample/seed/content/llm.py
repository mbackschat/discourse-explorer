"""Provider abstraction + selection for LLM-backed body generation.

Phase 2 (Sits 8-9). The seeder reuses the project-wide convention from
`discourse_explorer/config.py`: provider is inferred from which credentials
are set, not chosen by a separate flag. If `OPENAI_API_KEY` is present,
post-body generation goes through OpenAI; otherwise it falls back to Ollama
via `OLLAMA_HOST`. `EXTRACTION_MODEL` pins the model name for either
provider. Keeping the env-var vocabulary aligned with the parent project
means `sample/.env` and `<data-dir>/config/.env` stay interchangeable.

Sit 8 shipped the Protocol + the Ollama implementation + selection scaffold.
Sit 9 fills in the OpenAI branch.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

# Mirror `discourse_explorer.config._OLLAMA_EXTRACTION_DEFAULT` so the seeder
# falls back to the same default model when `EXTRACTION_MODEL` is unset.
_OLLAMA_EXTRACTION_DEFAULT = "qwen2.5:14b"
_OLLAMA_HOST_DEFAULT = "http://localhost:11434"
# Mirror `discourse_explorer.config.OPENAI_EXTRACTION_MODEL` — same default
# model so an unset `EXTRACTION_MODEL` produces identical behavior in both
# the seeder and the parent project's extraction pipeline.
_OPENAI_EXTRACTION_DEFAULT = "gpt-4.1-mini"


@runtime_checkable
class Provider(Protocol):
    """Minimal LLM-provider interface — one prompt in, one string out."""

    def generate(self, prompt: str) -> str:  # pragma: no cover - protocol stub
        ...


def select_provider() -> Provider:
    """Infer the active provider from environment, matching the parent project.

    `OPENAI_API_KEY` set and non-empty → `OpenAIProvider`, model from
    `EXTRACTION_MODEL` (default `gpt-4.1-mini`).
    Otherwise → `OllamaProvider` configured from `OLLAMA_HOST` /
    `EXTRACTION_MODEL` with the same defaults as `discourse_explorer/config.py`.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        # Local-import to avoid pulling the `openai` SDK at module import time
        # for callers that ultimately route through Ollama.
        from sample.seed.content.providers.openai import OpenAIProvider

        model = os.environ.get("EXTRACTION_MODEL", _OPENAI_EXTRACTION_DEFAULT)
        return OpenAIProvider(api_key=api_key, model=model)

    # Local-import to avoid pulling `requests` for callers that only need the
    # Protocol type at import time.
    from sample.seed.content.providers.ollama import OllamaProvider

    host = os.environ.get("OLLAMA_HOST", _OLLAMA_HOST_DEFAULT)
    model = os.environ.get("EXTRACTION_MODEL", _OLLAMA_EXTRACTION_DEFAULT)
    return OllamaProvider(host=host, model=model)
