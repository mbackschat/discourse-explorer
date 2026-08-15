"""Tests for discourse_explorer.auth.

Run via:
    uv run python -m unittest tests.test_auth
"""

import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _rc(**overrides):
    """Build a RuntimeConfig with sensible empty defaults; override per test."""
    from discourse_explorer.config import RuntimeConfig
    base = dict(
        discourse_url="",
        discourse_api_key="",
        discourse_api_username="",
        discourse_cookie="",
        discourse_username="",
        discourse_password="",
        data_dir=Path("/tmp/unused"),
        extraction_model="qwen2.5:14b",
        query_model="",
        openai_api_key="",
        ollama_host="http://localhost:11434",
        embed_model="nomic-embed-text",
        ollama_embed_dim=768,
        openai_embed_model="text-embedding-3-large",
        openai_embed_dim=3072,
        gleaning=1,
        llm_model_max_async=0,
        max_parallel_insert=0,
        rerank_provider="",
        rerank_model="",
        rerank_api_key="",
        rerank_base_url="",
    )
    base.update(overrides)
    return RuntimeConfig(**base)


class CookieDomainTests(unittest.TestCase):
    """The cookie-domain helper must strip the port — `requests` matches the
    cookie jar against host without port, so a domain like 'localhost:3000'
    silently fails to attach."""

    def test_strips_default_port(self) -> None:
        from discourse_explorer.auth import _cookie_domain
        self.assertEqual(_cookie_domain("https://discourse.example.com"), "discourse.example.com")

    def test_strips_explicit_port(self) -> None:
        from discourse_explorer.auth import _cookie_domain
        self.assertEqual(_cookie_domain("http://localhost:3000"), "localhost")
        self.assertEqual(_cookie_domain("https://example.com:8443/forum"), "example.com")

    def test_raises_on_unparseable_url(self) -> None:
        from discourse_explorer.auth import _cookie_domain
        from discourse_explorer.config import ConfigError
        with self.assertRaises(ConfigError):
            _cookie_domain("not-a-url")


class GetSessionConfigErrors(unittest.TestCase):
    """`get_session` raises ConfigError on missing inputs — never sys.exit."""

    def test_empty_base_url_raises(self) -> None:
        from discourse_explorer.auth import get_session
        from discourse_explorer.config import ConfigError
        with self.assertRaises(ConfigError):
            get_session("", _rc())

    def test_no_credentials_raises(self) -> None:
        from discourse_explorer.auth import get_session
        from discourse_explorer.config import ConfigError
        with self.assertRaises(ConfigError):
            get_session("https://discourse.example.com", _rc())

    def test_api_key_without_username_raises(self) -> None:
        from discourse_explorer.auth import get_session
        from discourse_explorer.config import ConfigError
        with self.assertRaises(ConfigError):
            get_session(
                "https://discourse.example.com",
                _rc(discourse_api_key="sk-test"),
            )


class HttpCleartextWarning(unittest.TestCase):
    """Plain `http://` triggers a warning before sending credentials."""

    def test_http_warns(self) -> None:
        from discourse_explorer.auth import get_session
        from discourse_explorer.config import ConfigError
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # No creds means we'll raise after warning — that's fine, we only
            # care about the warning side-effect.
            with self.assertRaises(ConfigError):
                get_session("http://discourse.example.com", _rc())
        self.assertTrue(
            any("cleartext" in str(w.message).lower() or "http" in str(w.message).lower()
                for w in caught),
            f"expected cleartext warning, got: {[str(w.message) for w in caught]}",
        )

    def test_https_does_not_warn(self) -> None:
        from discourse_explorer.auth import get_session
        from discourse_explorer.config import ConfigError
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(ConfigError):
                get_session("https://discourse.example.com", _rc())
        cleartext = [w for w in caught if "cleartext" in str(w.message).lower()]
        self.assertEqual(cleartext, [])


class CookieAttachesAtNonStandardPort(unittest.TestCase):
    """End-to-end: cookie set by _session_from_cookie must attach to a
    request when the URL carries a non-default port. The pre-fix behavior
    embedded the port in the cookie domain ('host:port'), which `requests`
    silently dropped at request-prep time, leaving the request anonymous."""

    def test_cookie_attaches_when_url_has_explicit_port(self) -> None:
        import requests

        from discourse_explorer.auth import _session_from_cookie
        rc = _rc(discourse_cookie="abc123")

        with mock.patch("discourse_explorer.auth._verify_session", side_effect=lambda s, *a, **k: s):
            session = _session_from_cookie("https://discourse.example.com:8443", rc)

        req = requests.Request("GET", "https://discourse.example.com:8443/latest.json")
        prepared = session.prepare_request(req)
        self.assertIn(
            "_t", prepared.headers.get("Cookie", ""),
            f"cookie header was: {prepared.headers.get('Cookie')!r}",
        )


if __name__ == "__main__":
    unittest.main()
