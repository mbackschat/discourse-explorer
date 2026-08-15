"""Tests for `sample.seed.content.llm` + the Ollama and OpenAI providers.

Covers the Sit-8 + Sit-9 invariants:
  * Provider selection mirrors `discourse_explorer/config.py` — `OPENAI_API_KEY`
    routes to `OpenAIProvider` (Sit 9); an unset key falls through to Ollama.
  * Constructors wire fields straight through (no env reads at construction).
  * `EXTRACTION_MODEL` overrides the default OpenAI model; default is
    `gpt-4.1-mini`, matching `discourse_explorer.config.OPENAI_EXTRACTION_MODEL`.
  * Live round-trips (Ollama + OpenAI) are gated behind
    `SAMPLE_LLM_INTEGRATION` so CI without credentials doesn't fail; opting in
    asserts the backend answers a tiny prompt with a non-empty string.
"""

from __future__ import annotations

import os
import unittest

from sample.seed.content.llm import Provider, select_provider
from sample.seed.content.providers.base import Provider as BaseProvider
from sample.seed.content.providers.ollama import OllamaProvider
from sample.seed.content.providers.openai import OpenAIProvider


class SelectProviderTests(unittest.TestCase):
    """`select_provider()` infers the backend from env, like the parent project."""

    def setUp(self) -> None:
        # Snapshot + clear the relevant env vars so each test starts from a
        # known baseline. `tearDown` restores the original mapping verbatim.
        self._saved = {
            key: os.environ.get(key)
            for key in ("OPENAI_API_KEY", "OLLAMA_HOST", "EXTRACTION_MODEL")
        }
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_openai_key_routes_to_openai_provider(self) -> None:
        """`OPENAI_API_KEY` set → `OpenAIProvider` (no construction-time HTTP)."""
        os.environ["OPENAI_API_KEY"] = "sk-fake-key"
        provider = select_provider()
        self.assertIsInstance(provider, OpenAIProvider)
        # Credentials flow through verbatim; no env-time mutation.
        self.assertEqual(provider.api_key, "sk-fake-key")

    def test_openai_default_model_is_gpt_4_1_mini(self) -> None:
        """Unset `EXTRACTION_MODEL` → default mirrors the parent project."""
        os.environ["OPENAI_API_KEY"] = "sk-fake-key"
        provider = select_provider()
        assert isinstance(provider, OpenAIProvider)
        self.assertEqual(provider.model, "gpt-4.1-mini")

    def test_openai_extraction_model_override(self) -> None:
        """`EXTRACTION_MODEL` set → wins over the default for the OpenAI branch."""
        os.environ["OPENAI_API_KEY"] = "sk-fake-key"
        os.environ["EXTRACTION_MODEL"] = "foo-model"
        provider = select_provider()
        assert isinstance(provider, OpenAIProvider)
        self.assertEqual(provider.model, "foo-model")

    def test_no_openai_key_returns_ollama(self) -> None:
        """No key → `OllamaProvider` configured from defaults."""
        provider = select_provider()
        self.assertIsInstance(provider, OllamaProvider)
        # Default host + model match `discourse_explorer/config.py`.
        self.assertEqual(provider.host, "http://localhost:11434")
        self.assertEqual(provider.model, "qwen2.5:14b")

    def test_ollama_host_and_extraction_model_are_honoured(self) -> None:
        """Env overrides flow into the constructed provider."""
        os.environ["OLLAMA_HOST"] = "http://ollama.internal:9000"
        os.environ["EXTRACTION_MODEL"] = "llama3.1:70b"
        provider = select_provider()
        assert isinstance(provider, OllamaProvider)
        self.assertEqual(provider.host, "http://ollama.internal:9000")
        self.assertEqual(provider.model, "llama3.1:70b")

    def test_protocol_runtime_check(self) -> None:
        """`OllamaProvider` satisfies both the canonical and re-exported Protocol."""
        provider = OllamaProvider(host="http://x:1", model="m")
        self.assertIsInstance(provider, Provider)
        self.assertIsInstance(provider, BaseProvider)


class OllamaProviderConstructionTests(unittest.TestCase):
    """Pure-construction unit checks — no HTTP."""

    def test_fields_are_passed_through(self) -> None:
        provider = OllamaProvider(host="http://x:1", model="m")
        self.assertEqual(provider.host, "http://x:1")
        self.assertEqual(provider.model, "m")

    def test_timeout_defaults_are_finite(self) -> None:
        """A finite default timeout protects callers from hanging on a bad host."""
        provider = OllamaProvider(host="http://x:1", model="m")
        self.assertGreater(provider.timeout, 0)
        self.assertLess(provider.timeout, 10_000)


class OpenAIProviderConstructionTests(unittest.TestCase):
    """Pure-construction unit checks — no network, no env reads."""

    def test_fields_are_passed_through(self) -> None:
        provider = OpenAIProvider(api_key="sk-x", model="m")
        self.assertEqual(provider.api_key, "sk-x")
        self.assertEqual(provider.model, "m")


@unittest.skipUnless(
    os.environ.get("SAMPLE_LLM_INTEGRATION"),
    "Ollama integration test — set SAMPLE_LLM_INTEGRATION=1 to opt in",
)
class OllamaIntegrationTests(unittest.TestCase):
    """Live round-trip against a running Ollama daemon. Skipped by default."""

    def test_tiny_prompt_returns_non_empty_string(self) -> None:
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("EXTRACTION_MODEL", "qwen2.5:14b")
        # Tight timeout so a misconfigured run fails fast rather than hanging
        # the suite. The daemon should respond well within five seconds for
        # a one-token prompt; bump if you legitimately hit it on a cold model.
        provider = OllamaProvider(host=host, model=model, timeout=5.0)
        out = provider.generate("Say the single word: hello")
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)


@unittest.skipUnless(
    os.environ.get("SAMPLE_LLM_INTEGRATION") and os.environ.get("OPENAI_API_KEY"),
    "OpenAI integration test — set SAMPLE_LLM_INTEGRATION=1 and OPENAI_API_KEY to opt in",
)
class OpenAIIntegrationTests(unittest.TestCase):
    """Live round-trip against the OpenAI API. Skipped by default.

    Doubly gated: requires both the opt-in flag AND a real `OPENAI_API_KEY`.
    Uses a 5-second per-call timeout (via `socket.setdefaulttimeout` for the
    duration of the call) so a misconfigured run fails fast rather than
    hanging the suite.
    """

    def test_tiny_prompt_returns_non_empty_string(self) -> None:
        import socket

        api_key = os.environ["OPENAI_API_KEY"]
        model = os.environ.get("EXTRACTION_MODEL", "gpt-4.1-mini")
        provider = OpenAIProvider(api_key=api_key, model=model)
        prior = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5.0)
        try:
            out = provider.generate("Say the single word: hello")
        finally:
            socket.setdefaulttimeout(prior)
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)


if __name__ == "__main__":
    unittest.main()
