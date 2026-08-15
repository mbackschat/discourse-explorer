"""Tests for the layered dotenv resolution in discourse_explorer.config.bootstrap().

Run via:
    uv run python -m tests.test_config_bootstrap

Or as a pytest file if pytest is added later — the assertions work both ways.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# Make imports resolve when run as a script from project root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# Env vars that each test scrubs so one test's leftovers don't pollute the next.
_ENV_KEYS = (
    "DISCOURSE_DATA_DIR",
    "DISCOURSE_URL",
    "DISCOURSE_API_KEY",
    "DISCOURSE_API_USERNAME",
    "DISCOURSE_COOKIE",
    "DISCOURSE_USERNAME",
    "DISCOURSE_PASSWORD",
    "EXTRACTION_MODEL",
    "QUERY_MODEL",
    "OPENAI_API_KEY",
    "OLLAMA_HOST",
    "EMBED_MODEL",
    "OLLAMA_EMBED_DIM",
    "OPENAI_EMBED_MODEL",
    "OPENAI_EMBED_DIM",
    "GLEANING",
    "FORCE_LLM_SUMMARY_ON_MERGE",
)


class _BootstrapHarness:
    """Temp data dir, env isolation, and a freshly-reloaded config module.

    Shared so every test that needs to reach `bootstrap()` gets the same
    isolation, rather than asserting on `os.environ` because wiring the harness
    up was inconvenient.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="discourse-test-"))
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        (self.scratch / "config").mkdir()
        # Snapshot + clear relevant env vars.
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- Helpers ----------------------------------------------------------

    def _import_config_fresh(self):
        """Reload config.py with PROJECT_ROOT_ENV pointed at a test file.

        Each test constructs its own root env. We patch the module constant
        after import so the real project .env is never consulted.
        """
        import importlib

        import discourse_explorer.config as mod
        return importlib.reload(mod)

    def _write_env(self, path: Path, pairs: dict) -> None:
        path.write_text("\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n")


class BootstrapLayeringTests(_BootstrapHarness, unittest.TestCase):
    """The layering order is load-bearing — data-dir .env must override root
    .env, and explicit CLI args must override both for dir resolution."""

    # -- Tests ------------------------------------------------------------

    def test_data_dir_env_wins_over_root_env(self) -> None:
        """Data-dir .env overrides root .env for overlapping keys."""
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {
            "DISCOURSE_DATA_DIR": str(self.scratch),
            "EXTRACTION_MODEL": "root-model",
        })
        self._write_env(self.scratch / "config" / ".env", {
            "EXTRACTION_MODEL": "scratch-model",
        })

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(rc.extraction_model, "scratch-model")
        self.assertEqual(rc.data_dir, self.scratch.resolve())

    def test_root_env_used_when_data_dir_env_missing(self) -> None:
        """Root .env provides the value when data-dir .env is absent."""
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {
            "DISCOURSE_DATA_DIR": str(self.scratch),
            "EXTRACTION_MODEL": "root-model",
        })
        # No <scratch>/config/.env created.

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(rc.extraction_model, "root-model")

    def test_cli_path_overrides_root_data_dir(self) -> None:
        """Explicit CLI path wins over DISCOURSE_DATA_DIR from root .env."""
        other = self.tmp / "other"
        other.mkdir()
        (other / "config").mkdir()

        root_env = self.tmp / "root.env"
        self._write_env(root_env, {"DISCOURSE_DATA_DIR": str(self.scratch)})
        self._write_env(other / "config" / ".env", {
            "EXTRACTION_MODEL": "other-model",
        })

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(other)

        self.assertEqual(rc.data_dir, other.resolve())
        self.assertEqual(rc.extraction_model, "other-model")

    def test_missing_data_dir_raises(self) -> None:
        """No CLI arg and no env var → clear error."""
        missing_root = self.tmp / "nonexistent.env"
        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", missing_root):
            with self.assertRaises(cfg.ConfigError):
                cfg.bootstrap(None)

    def test_auto_seeds_entity_types_json(self) -> None:
        """Migration: missing config/entity_types.json gets the embedded default."""
        # Use a different scratch dir without pre-created config/.
        target = self.tmp / "needs-migration"
        target.mkdir()
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {"DISCOURSE_DATA_DIR": str(target)})

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertTrue(rc.paths().entity_types_file.exists())
        vocab = json.loads(rc.paths().entity_types_file.read_text())
        names = {t["name"] for t in vocab["types"]}
        # Structural types must be present.
        for required in cfg.STRUCTURAL_TYPE_NAMES:
            self.assertIn(required, names)

    def test_openai_embed_dim_auto_resolves(self) -> None:
        """_OPENAI_EMBED_DIMS lookup sets dim when OPENAI_EMBED_DIM is unset."""
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {
            "DISCOURSE_DATA_DIR": str(self.scratch),
            "OPENAI_EMBED_MODEL": "text-embedding-3-large",
        })

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(rc.openai_embed_dim, 3072)

    def test_default_extraction_model_prefers_openai(self) -> None:
        """When OPENAI_API_KEY is set and EXTRACTION_MODEL is at the Ollama
        default, we fall through to the OpenAI extraction model."""
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {
            "DISCOURSE_DATA_DIR": str(self.scratch),
            "OPENAI_API_KEY": "sk-test",
        })

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(rc.default_extraction_model(), cfg.OPENAI_EXTRACTION_MODEL)

    def test_default_extraction_model_respects_override(self) -> None:
        """Explicit EXTRACTION_MODEL wins even with OPENAI_API_KEY set."""
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {
            "DISCOURSE_DATA_DIR": str(self.scratch),
            "OPENAI_API_KEY": "sk-test",
            "EXTRACTION_MODEL": "gpt-5-mini",
        })

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(rc.default_extraction_model(), "gpt-5-mini")


class EntityVocabValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="discourse-vocab-test-"))
        (self.tmp / "config").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_vocab(self, vocab: dict) -> None:
        (self.tmp / "config" / "entity_types.json").write_text(json.dumps(vocab))

    def test_load_entity_types_round_trip(self) -> None:
        import discourse_explorer.config as cfg
        self._write_vocab(cfg._DEFAULT_ENTITY_TYPES)
        vocab = cfg.load_entity_types(self.tmp)
        self.assertEqual(
            cfg.structural_type_names(vocab),
            list(cfg.STRUCTURAL_TYPE_NAMES),
        )
        self.assertGreater(len(cfg.content_type_names(vocab)), 0)

    def test_missing_structural_raises(self) -> None:
        import discourse_explorer.config as cfg
        self._write_vocab({"version": 1, "types": [
            {"name": "Issue", "color": "#000", "structural": False},
        ]})
        with self.assertRaises(cfg.ConfigError):
            cfg.load_entity_types(self.tmp)

    def test_structural_without_flag_raises(self) -> None:
        import discourse_explorer.config as cfg
        types = [dict(t) for t in cfg._DEFAULT_ENTITY_TYPES["types"]]
        # Break the flag on one structural type.
        for t in types:
            if t["name"] == "User":
                t["structural"] = False
        self._write_vocab({"version": 1, "types": types})
        with self.assertRaises(cfg.ConfigError):
            cfg.load_entity_types(self.tmp)


class TopicToDocumentEncodedBlobTests(unittest.TestCase):
    """Verified against the full 1331-topic corpus: the only 200+ char runs of
    [A-Za-z0-9+/=] are pasted binary payloads (base64-encoded PDFs, hex-encoded
    Java serialization dumps). They waste LLM calls and produce format errors
    during extraction with no semantic benefit. Eliding them at document-build
    time keeps scraped JSON intact while skipping the noise on the index side.
    """

    def _build_topic(self, post_text: str) -> dict:
        return {
            "id": 1,
            "title": "t",
            "category_name": "General",
            "created_at": "2026-01-01T00:00:00Z",
            "tags": [],
            "posts": [{"username": "u", "plain_text": post_text}],
        }

    def test_long_base64_run_is_elided(self) -> None:
        from discourse_explorer.query import topic_to_document
        # 300-char base64-alphabet run (above the 200-char threshold)
        blob = "JVBERi0xLjQKJfbk/N8K" * 15  # 300 chars, valid base64 alphabet
        self.assertGreaterEqual(len(blob), 200)
        topic = self._build_topic(f"Hi, the payload is {blob} and that's all.")
        doc = topic_to_document(topic)
        self.assertNotIn(blob, doc)
        self.assertIn("Hi, the payload is", doc)
        self.assertIn("and that's all.", doc)
        self.assertIn("elided", doc.lower())

    def test_short_base64_like_string_preserved(self) -> None:
        from discourse_explorer.query import topic_to_document
        short_hash = "a" * 199  # 199 chars — just under the threshold
        topic = self._build_topic(f"commit {short_hash} is the one")
        doc = topic_to_document(topic)
        self.assertIn(short_hash, doc)

    def test_normal_prose_preserved(self) -> None:
        from discourse_explorer.query import topic_to_document
        prose = (
            "We want to increase the font size of the main menu in the "
            "application frame. Setting theme.components.menu.mainMenu.item."
            "fontSize doesn't seem to work. The git commit is a1b2c3d4 and "
            "the image tag is v1.2.3 — any ideas?"
        )
        topic = self._build_topic(prose)
        doc = topic_to_document(topic)
        self.assertIn(prose, doc)
        self.assertNotIn("elided", doc.lower())

    def test_pem_style_newline_separated_base64_preserved(self) -> None:
        """PEM certificates are 64-char lines separated by newlines — the
        regex requires contiguous runs, so PEM is safe."""
        from discourse_explorer.query import topic_to_document
        pem_body = "\n".join("A" * 64 for _ in range(20))  # 20 lines
        topic = self._build_topic(f"Here's the cert:\n{pem_body}\nThanks.")
        doc = topic_to_document(topic)
        self.assertIn(pem_body, doc)
        self.assertNotIn("elided", doc.lower())


class SummaryOnMergeTests(_BootstrapHarness, unittest.TestCase):
    """The summarization threshold must not depend on how a run was launched.

    LightRAG reads FORCE_LLM_SUMMARY_ON_MERGE from the process environment and
    defaults it to 8. Only `scripts/index.sh` used to set it (999), so the
    same nominal `--index --clear` cost 3-5x more when typed by hand, and the
    script's choice could not be overridden. It is now a RuntimeConfig field
    passed explicitly to the LightRAG constructor.
    """

    def test_default_skips_the_summary_cascade(self):
        from discourse_explorer.config import SUMMARY_ON_MERGE_DEFAULT
        self.assertEqual(999, SUMMARY_ON_MERGE_DEFAULT)
        self.assertGreater(SUMMARY_ON_MERGE_DEFAULT, 8,
                           "must exceed LightRAG's default or the cascade fires")

    def test_data_dir_env_override_reaches_the_runtime_config(self):
        """The knob has to survive the whole dotenv chain into RuntimeConfig.

        Asserting on `os.environ` instead — as an earlier version of this test
        did — proves only that `int()` works. It passed with the field deleted
        from `config.py` entirely.
        """
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {"DISCOURSE_DATA_DIR": str(self.scratch)})
        self._write_env(self.scratch / "config" / ".env", {
            "FORCE_LLM_SUMMARY_ON_MERGE": "8",
        })

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(8, rc.force_llm_summary_on_merge)

    def test_default_reaches_the_runtime_config_when_unset(self):
        root_env = self.tmp / "root.env"
        self._write_env(root_env, {"DISCOURSE_DATA_DIR": str(self.scratch)})

        cfg = self._import_config_fresh()
        with mock.patch.object(cfg, "PROJECT_ROOT_ENV", root_env):
            rc = cfg.bootstrap(None)

        self.assertEqual(cfg.SUMMARY_ON_MERGE_DEFAULT,
                         rc.force_llm_summary_on_merge)

    def _script(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "scripts" / "index.sh").read_text()

    def _script_code(self):
        """Script text with comment lines and the heredoc usage block removed.

        Every guard this class checks for is also *described* in the script's
        comments, so a plain substring search over the whole file stays green
        after the actual code is deleted. Search executable lines only.
        """
        out, in_heredoc = [], False
        for line in self._script().splitlines():
            stripped = line.strip()
            if in_heredoc:
                # Closing delimiter of `<<'USAGE'`, on its own line.
                if stripped == "USAGE":
                    in_heredoc = False
                continue
            if "<<'USAGE'" in stripped or '<<"USAGE"' in stripped:
                in_heredoc = True
                continue
            if stripped.startswith("#") or not stripped:
                continue
            out.append(line.split(" #", 1)[0])
        return "\n".join(out)

    def test_script_code_strips_comments(self):
        """Guard for the guard: if this stops removing comments, every
        assertion below silently reverts to matching prose."""
        self.assertIn("nohup", self._script(),
                      "fixture assumption: 'nohup' appears in the comments")
        self.assertNotIn("nohup", self._script_code())

    def test_script_sets_no_indexing_knobs_of_its_own(self):
        """A knob set only in the script's launch env is invisible to anyone
        reading config, and un-overridable by the user."""
        self.assertNotIn("FORCE_LLM_SUMMARY_ON_MERGE=", self._script_code())

    def test_script_detaches_into_its_own_session(self):
        """nohup alone leaves the child in the launching shell's process group,
        so a process-group SIGKILL still reaches it."""
        self.assertIn("start_new_session=True", self._script_code())

    def test_script_requires_an_explicit_mode(self):
        """A bare invocation must NOT default to the destructive --clear path."""
        s = self._script_code()
        self.assertIn("--full)", s)
        self.assertIn("--resume)", s)
        self.assertIn("64", s, "bare invocation must exit 64 (EX_USAGE)")

    def test_script_checks_both_process_name_spellings(self):
        """`discourse_explorer.query` alone cannot match the console entry
        point (`discourse-explorer query`) — the 2026-08-14 corruption.

        Asserts against the **pgrep line specifically**, not the whole script.
        Searching the whole text passed even with the hyphen spelling deleted
        from the guard, because the launch command line contains
        `discourse-explorer query` too — so the test claimed to cover the exact
        regression it did not cover.
        """
        pgrep_lines = [l for l in self._script_code().splitlines()
                       if "pgrep" in l]
        self.assertTrue(pgrep_lines, "no executable pgrep guard in the script")
        for line in pgrep_lines:
            self.assertIn("discourse-explorer query", line)
            self.assertIn("discourse_explorer.query", line)


if __name__ == "__main__":
    unittest.main()
