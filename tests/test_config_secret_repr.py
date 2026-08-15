"""`repr(RuntimeConfig)` must never carry a secret.

A frozen dataclass gets a generated `__repr__` that prints every field. That
puts the OpenAI key, the Discourse session cookie and the account password one
stray `print(rc)` away from a terminal, a log file, or an exception traceback —
and tracebacks are the dangerous one, because nobody chooses to emit those.

This is not hypothetical. On 2026-08-15 a `RuntimeConfig` repr reached a
terminal through a single missing pair of parentheses: `default_extraction_model`
is a method, not a property, so `rc.default_extraction_model` rendered the bound
method, and a bound method's repr embeds the repr of `self`. One typo, full key
disclosure, and the key had to be rotated.

Masking at the field level is the fix that covers the class: it holds for
`print`, for `logging`, for f-strings, for tracebacks, and for any future field
someone adds to the wrong list — because the test below enumerates by name
rather than trusting a hand-kept list.
"""
import unittest
from dataclasses import fields

from discourse_explorer.config import RuntimeConfig

# Any field whose name matches is a credential and must be masked in repr.
SECRET_HINTS = ("key", "cookie", "password", "token", "secret")

SENTINEL = "SENTINEL-DO-NOT-LEAK-{}"


def _is_secret(name: str) -> bool:
    return any(hint in name.lower() for hint in SECRET_HINTS)


def _config_with_sentinels() -> tuple[RuntimeConfig, dict]:
    """A config whose every string field holds a uniquely findable value."""
    from pathlib import Path

    kwargs, secrets = {}, {}
    for f in fields(RuntimeConfig):
        if f.type is int or f.name in ("ollama_embed_dim", "openai_embed_dim",
                                       "gleaning", "llm_model_max_async",
                                       "max_parallel_insert",
                                       "force_llm_summary_on_merge"):
            kwargs[f.name] = 1
        elif f.name == "data_dir":
            kwargs[f.name] = Path("/tmp/x")
        else:
            value = SENTINEL.format(f.name)
            kwargs[f.name] = value
            if _is_secret(f.name):
                secrets[f.name] = value
    return RuntimeConfig(**kwargs), secrets


class SecretReprTests(unittest.TestCase):
    def test_no_credential_field_appears_in_repr(self):
        rc, secrets = _config_with_sentinels()

        self.assertTrue(secrets, "no credential fields found — check SECRET_HINTS")
        rendered = repr(rc)
        for name, value in secrets.items():
            with self.subTest(field=name):
                self.assertNotIn(value, rendered)

    def test_non_secret_fields_are_still_visible(self):
        """Masking must not blind the repr; it is a debugging tool."""
        rc, _ = _config_with_sentinels()

        rendered = repr(rc)
        self.assertIn(SENTINEL.format("discourse_url"), rendered)
        self.assertIn(SENTINEL.format("query_model"), rendered)

    def test_the_values_are_still_readable_on_the_instance(self):
        """Masking is presentational only; the code still needs the secret."""
        rc, secrets = _config_with_sentinels()

        for name, value in secrets.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(rc, name), value)

    def test_a_bound_method_repr_cannot_leak_either(self):
        """The exact 2026-08-15 path: `rc.method` without the parentheses."""
        rc, secrets = _config_with_sentinels()

        rendered = repr(rc.default_extraction_model)
        for name, value in secrets.items():
            with self.subTest(field=name):
                self.assertNotIn(value, rendered)


if __name__ == "__main__":
    unittest.main()
