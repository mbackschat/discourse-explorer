"""Configuration for the Discourse explorer toolchain.

Two tiers of config, resolved at CLI startup by `bootstrap()`:

1. **Codebase-level constants** (module-level, never change per-run):
   rate-limit knobs, OpenAI embedding-dim lookup, the structural type
   vocabulary, and the default fallback entity-type vocabulary used to
   migrate pre-JSON data dirs.

2. **Per-run configuration** (`RuntimeConfig`): auth, URL, model choices,
   embedding model/dim, gleaning. Loaded from a layered dotenv chain:
   `<project-root>/.env` (for `DISCOURSE_DATA_DIR` and shell convenience)
   then `<data-dir>/config/.env` (authoritative per-run overrides).

A scrape run is 1:1 with a data directory, so per-run state belongs next to
its data, not at the project root. `bootstrap()` returns a frozen
`RuntimeConfig` that CLIs thread into their helpers.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Codebase-level constants (stable across all data dirs)
# ---------------------------------------------------------------------------

REQUEST_DELAY = 1.0  # seconds between HTTP requests
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # exponential backoff multiplier

_OPENAI_EMBED_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# Structural types are owned by `_topic_to_custom_kg` in query.py: they're
# authored deterministically from JSON fields and must remain exactly these
# names for the custom-KG + visualize lookups to line up. They are NOT passed
# to the LLM's `addon_params["entity_types"]` so Pass 2 can't re-extract them
# and overwrite Pass 1's typing on name merge.
STRUCTURAL_TYPE_NAMES: tuple[str, ...] = ("User", "Topic", "Category", "Tag")

# Discourse-universal relationship pins. Authored by `_topic_to_custom_kg`
# in `query.py` (Pass 1) and recognized by `rel_clusters.py` as fixed
# buckets that bypass the embedding-cluster pipeline. Each pin carries:
#  - `id`: internal key used by `_topic_to_custom_kg` to look up the
#     keyword CSV at author time (must stay stable — it's a code contract
#     between Pass 1 and the pin classifier).
#  - `display_name`: shown in the visualizer legend. Past-tense Discourse
#     UI verbs (see docs/discourse/DISCOURSE_TERMINOLOGY.md).
#  - `keywords_csv`: comma-separated keyword strings emitted verbatim on
#     every Pass 1 edge of that kind.
# Colors are NOT stored here — visualize.py assigns them from a palette in
# pin-order at render time, so pin colors stay consistent across all
# Discourse corpora without needing to be managed per-pin here.


class StructuralRelPin(NamedTuple):
    id: str
    display_name: str
    keywords_csv: str


STRUCTURAL_REL_PINS: tuple[StructuralRelPin, ...] = (
    StructuralRelPin("user_posted",       "Posted",      "posted, authored, participated"),
    StructuralRelPin("topic_tagged",      "Tagged",      "tagged, tag, labeled"),
    StructuralRelPin("topic_in_category", "Categorized", "posted in, category, section"),
)

# Back-compat dict for `query.py::_topic_to_custom_kg` — same
# `STRUCTURAL_REL_KEYWORDS[pin_id]` lookup shape as before. Derived, not
# duplicated, so drift between the two is impossible.
STRUCTURAL_REL_KEYWORDS: dict[str, str] = {
    p.id: p.keywords_csv for p in STRUCTURAL_REL_PINS
}


# Discourse mints a `<id>-tag` slug when a tag name won't slugify (e.g. an
# all-numeric name, rejected to avoid colliding with tag IDs). That slug is an
# opaque placeholder with no merge potential, so it must never become an entity
# name — fall back to the display name instead.
_PLACEHOLDER_SLUG_RE = re.compile(r"^\d+-tag$")


# Per-entity description-summarization threshold handed to LightRAG.
#
# LightRAG reads `FORCE_LLM_SUMMARY_ON_MERGE` from the *process environment*
# itself (`lightrag.py:307`) and defaults it to 8, which fires an extra LLM call
# per entity once 8 descriptions accumulate. On a multi-thousand-topic corpus
# that dominates the bill for no measurable retrieval gain — descriptions get
# concatenated verbatim instead, which is what this project's published
# benchmark in docs/MANUAL.md was measured with.
#
# It lives here, and is passed explicitly to the LightRAG constructor, so the
# value cannot depend on *how* indexing was launched. Before this, only
# `scripts/index.sh` set it (to 999) via the launch environment, so a raw
# CLI invocation silently got LightRAG's 8 and ran 3-5x slower and dearer for
# the same nominal command — and a user could not override the script's choice.
# Override via `FORCE_LLM_SUMMARY_ON_MERGE` in `<data-dir>/config/.env`.
SUMMARY_ON_MERGE_DEFAULT = 999


def tag_display(tag) -> str:
    """Human-facing tag text for the LLM-visible document header.

    The mirror of `tag_label`: prefers `name`, falls back to `slug`. Deliberately
    NOT the slug-first identity, because this string lands in the document text
    that LightRAG hashes for doc-level dedupe. Changing it rewrites every
    affected document's id and turns an incremental update into a full
    re-extraction — measured at 1,018 of 1,399 topics and ~13x the cost on the
    production corpus, 2026-08-14. Tag *identity* normalization belongs in the graph
    node names (`tag_label`), which are not part of the hashed text.
    """
    # Same `null`-is-not-a-tag guard as `tag_label`, and it matters more here:
    # this string is hashed for doc-level dedupe, so a literal "None" in the
    # header would be baked into a document id. Safe to add now precisely
    # because no topic carries a null tag (verified across all 1,399 production
    # topics and the 33-topic sample fixture, 2026-08-15), so no existing
    # document's text — and therefore no existing hash — changes.
    if tag is None:
        return ""
    if not isinstance(tag, dict):
        return str(tag).strip()
    return (str(tag.get("name") or "").strip()
            or str(tag.get("slug") or "").strip())


def tag_label(tag) -> str:
    """Canonical label for a scraped Discourse tag, derived from `slug`.

    A tag's `name` is not stable across scrapes. Verified on the production corpus
    2026-08-14: tag id=144 (slug `2025-06`) appears as name `2025․06` with
    U+2024 ONE DOT LEADER in topics fetched in April, and as name `2025-06`
    with a plain hyphen in topics fetched in August. Same tag, same slug, two
    names. Anything keyed on `name` therefore splits one tag by *when* the
    topic was scraped — as graph nodes, as stats rows, and as version entries
    in QUERY-GUIDE.md.

    The `slug` is stable across both eras, so it is the identity. `name` is the
    fallback for tags with no usable slug. Legacy plain-string tags (older
    scrapes stored bare strings) pass through unchanged.

    Lives here rather than in a consumer because `query.py` (graph nodes +
    LLM-visible document header), `derive_query_guide.py` (version coverage)
    and `stats.py` (DuckDB views) must all agree on one vocabulary.
    """
    # A JSON `null` in the tags array is the absence of a tag, not a tag named
    # "None" — which is what `str(None).strip()` hands back, and which would
    # then become a graph node, a `topic_tags` row and a version-table entry.
    # SQL spells this NULL; `""` is this side's spelling of the same nothing.
    if tag is None:
        return ""
    if not isinstance(tag, dict):
        return str(tag).strip()
    slug = str(tag.get("slug") or "").strip()
    if slug and not _PLACEHOLDER_SLUG_RE.match(slug):
        return slug
    return str(tag.get("name") or "").strip()

# Embedded default vocabulary. Used by `_maybe_migrate()` to seed an
# entity_types.json in pre-JSON data dirs so upgrades are seamless. After
# migration the JSON file is authoritative; this constant is only a one-shot
# fallback for historical data dirs. Colors are omitted on purpose — the
# visualizer paints names from a palette at render time; entity_types.json
# holds vocabulary only (name + structural flag).
_DEFAULT_ENTITY_TYPES: dict = {
    "version": 2,
    "types": [
        {"name": "User",      "structural": True},
        {"name": "Topic",     "structural": True},
        {"name": "Category",  "structural": True},
        {"name": "Tag",       "structural": True},
        {"name": "Model",     "structural": False},
        {"name": "Issue",     "structural": False},
        {"name": "Document",  "structural": False},
        {"name": "Component", "structural": False},
        {"name": "Version",   "structural": False},
        {"name": "Api",       "structural": False},
        {"name": "Rule",      "structural": False},
    ],
}


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Per-site paths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SitePaths:
    """All filesystem paths rooted at a single data directory."""
    data_dir: Path
    topics_dir: Path
    sync_state_file: Path
    index_file: Path
    categories_file: Path
    config_dir: Path
    env_file: Path
    entity_types_file: Path
    graphrag_dir: Path
    graphml_file: Path


def site_paths_from_dir(data_dir: Path) -> SitePaths:
    """Construct a SitePaths rooted at `data_dir`."""
    config_dir = data_dir / "config"
    graphrag_dir = data_dir / "graphrag"
    return SitePaths(
        data_dir=data_dir,
        topics_dir=data_dir / "topics",
        sync_state_file=data_dir / "sync_state.json",
        index_file=data_dir / "index.json",
        categories_file=data_dir / "categories.json",
        config_dir=config_dir,
        env_file=config_dir / ".env",
        entity_types_file=config_dir / "entity_types.json",
        graphrag_dir=graphrag_dir,
        graphml_file=graphrag_dir / "graph_chunk_entity_relation.graphml",
    )


# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------

# OpenAI extraction default (used when no EXTRACTION_MODEL env override).
# Why gpt-4.1-mini (not gpt-5-mini or gpt-4o-mini) for extraction: entity
# extraction with a constrained vocabulary punishes format slippage, and
# gpt-4.1-mini has measurably better instruction-following than gpt-4o-mini on
# structured output without the ~5x latency overhead of gpt-5-series reasoning
# models. See <data-dir>/CONFIG_LOG.md for the 2026-04-23 post-mortem.
OPENAI_EXTRACTION_MODEL = "gpt-4.1-mini"
_OLLAMA_EXTRACTION_DEFAULT = "qwen2.5:14b"


@dataclass(frozen=True)
class RuntimeConfig:
    """All per-run configuration (env-var-driven). Construct via `bootstrap()`."""
    # Target site + auth
    discourse_url: str
    discourse_api_key: str = field(repr=False)
    discourse_api_username: str
    discourse_cookie: str = field(repr=False)
    discourse_username: str
    discourse_password: str = field(repr=False)
    # Resolved absolute data directory
    data_dir: Path
    # Models
    extraction_model: str
    query_model: str
    openai_api_key: str = field(repr=False)
    ollama_host: str
    # Embeddings (changes require --clear re-index)
    embed_model: str
    ollama_embed_dim: int
    openai_embed_model: str
    openai_embed_dim: int
    # Extraction behavior
    gleaning: int
    # LightRAG concurrency (0 = use provider-specific default in `_get_rag`)
    llm_model_max_async: int
    max_parallel_insert: int
    # Rerank (optional). Empty provider = no rerank (QueryParam.enable_rerank
    # is also flipped to False to suppress LightRAG's "configured but no model"
    # warning). Supported providers: `jina`, `cohere`, `ali` — all remote HTTP
    # APIs shipped by LightRAG's `rerank.py`. A self-hosted bge / llama.cpp
    # reranker works via `cohere` + a local `rerank_base_url` (TEI and similar
    # expose a Cohere-compatible /rerank endpoint).
    rerank_provider: str
    rerank_model: str
    rerank_api_key: str = field(repr=False)
    rerank_base_url: str
    # Per-entity description-summarization threshold, passed explicitly to the
    # LightRAG constructor so it never depends on the ambient environment. See
    # SUMMARY_ON_MERGE_DEFAULT. Defaulted (and therefore last) so that adding it
    # doesn't break callers that build a RuntimeConfig field-by-field.
    force_llm_summary_on_merge: int = SUMMARY_ON_MERGE_DEFAULT

    @property
    def is_openai(self) -> bool:
        return bool(self.openai_api_key)

    def default_extraction_model(self) -> str:
        """Resolve the extraction model with provider-aware fallback.

        Explicit EXTRACTION_MODEL overrides everything. If it's at the Ollama
        default and OPENAI_API_KEY is set, we prefer the OpenAI fallback.
        """
        if self.extraction_model != _OLLAMA_EXTRACTION_DEFAULT:
            return self.extraction_model
        return OPENAI_EXTRACTION_MODEL if self.is_openai else self.extraction_model

    def paths(self) -> SitePaths:
        return site_paths_from_dir(self.data_dir)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

PROJECT_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"


def bootstrap(cli_data_dir: Optional[Path] = None) -> RuntimeConfig:
    """Resolve per-run configuration. Call once per CLI invocation.

    Order of operations:
      1. Load `<project-root>/.env` (non-override) so `DISCOURSE_DATA_DIR`
         may flow from it without clobbering real shell exports.
      2. Resolve data dir: explicit `cli_data_dir` ≻ `DISCOURSE_DATA_DIR`.
      3. Load `<data-dir>/config/.env` with `override=True` — data-dir values
         become authoritative for overlapping keys.
      4. Auto-seed `<data-dir>/config/entity_types.json` from the embedded
         default if the data dir exists but config/ does not. Idempotent.
      5. Build and return a frozen `RuntimeConfig`.
    """
    if PROJECT_ROOT_ENV.exists():
        load_dotenv(PROJECT_ROOT_ENV, override=False)

    data_dir = _resolve_data_dir(cli_data_dir)
    paths = site_paths_from_dir(data_dir)

    if paths.env_file.exists():
        load_dotenv(paths.env_file, override=True)

    _maybe_migrate(paths)

    openai_embed_model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-large")
    openai_embed_dim = int(
        os.environ.get("OPENAI_EMBED_DIM")
        or _OPENAI_EMBED_DIMS.get(openai_embed_model, 1536)
    )

    return RuntimeConfig(
        discourse_url=os.environ.get("DISCOURSE_URL", "").rstrip("/"),
        discourse_api_key=os.environ.get("DISCOURSE_API_KEY", ""),
        discourse_api_username=os.environ.get("DISCOURSE_API_USERNAME", ""),
        discourse_cookie=os.environ.get("DISCOURSE_COOKIE", ""),
        discourse_username=os.environ.get("DISCOURSE_USERNAME", ""),
        discourse_password=os.environ.get("DISCOURSE_PASSWORD", ""),
        data_dir=data_dir,
        extraction_model=os.environ.get("EXTRACTION_MODEL", _OLLAMA_EXTRACTION_DEFAULT),
        query_model=os.environ.get("QUERY_MODEL", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        ollama_embed_dim=int(os.environ.get("OLLAMA_EMBED_DIM", "768")),
        openai_embed_model=openai_embed_model,
        openai_embed_dim=openai_embed_dim,
        gleaning=int(os.environ.get("GLEANING", "1")),
        force_llm_summary_on_merge=int(os.environ.get(
            "FORCE_LLM_SUMMARY_ON_MERGE", str(SUMMARY_ON_MERGE_DEFAULT))),
        llm_model_max_async=int(os.environ.get("LLM_MODEL_MAX_ASYNC", "0")),
        max_parallel_insert=int(os.environ.get("MAX_PARALLEL_INSERT", "0")),
        rerank_provider=os.environ.get("RERANK_PROVIDER", "").strip().lower(),
        rerank_model=os.environ.get("RERANK_MODEL", "").strip(),
        rerank_api_key=os.environ.get("RERANK_API_KEY", ""),
        rerank_base_url=os.environ.get("RERANK_BASE_URL", "").strip(),
    )


def _resolve_data_dir(cli_data_dir: Optional[Path]) -> Path:
    if cli_data_dir is not None:
        return Path(cli_data_dir).expanduser().resolve()
    env_val = os.environ.get("DISCOURSE_DATA_DIR", "").strip()
    if not env_val:
        raise ConfigError(
            "Data directory is required. Pass it as a CLI argument "
            "(--path / --output / positional) or set DISCOURSE_DATA_DIR "
            "in the project-root .env."
        )
    return Path(env_val).expanduser().resolve()


def _maybe_migrate(paths: SitePaths) -> None:
    """Seed <data-dir>/config/entity_types.json from the embedded default.

    Only acts when the data dir already exists (i.e., we're upgrading a
    scraped-but-not-yet-config-ified dir). Doesn't create .env — credentials
    stay an explicit user action.
    """
    if not paths.data_dir.exists():
        return
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    if not paths.entity_types_file.exists():
        paths.entity_types_file.write_text(
            json.dumps(_DEFAULT_ENTITY_TYPES, indent=2) + "\n"
        )


# ---------------------------------------------------------------------------
# Entity-type vocabulary loader
# ---------------------------------------------------------------------------

def load_entity_types(data_dir: Path) -> dict:
    """Return the parsed entity_types.json payload, validated.

    Validation guarantees every caller can rely on the four structural types
    being present (query.py's Pass 1 depends on exactly those names).
    """
    paths = site_paths_from_dir(data_dir)
    if not paths.entity_types_file.exists():
        raise ConfigError(
            f"Entity-type vocabulary missing at {paths.entity_types_file}. "
            "Run `/discover-entity-types` to build one, or copy the example "
            "at discourse_explorer/config/entity_types.example.json."
        )
    data = json.loads(paths.entity_types_file.read_text())
    types = data.get("types") or []
    if not types:
        raise ConfigError(
            f"Entity-type vocabulary at {paths.entity_types_file} has no "
            "'types' array."
        )
    names = {t.get("name") for t in types}
    missing = set(STRUCTURAL_TYPE_NAMES) - names
    if missing:
        raise ConfigError(
            f"Entity-type vocabulary at {paths.entity_types_file} is missing "
            f"structural types: {sorted(missing)}. These are required and "
            "must be present with structural=true."
        )
    for t in types:
        if t.get("name") in STRUCTURAL_TYPE_NAMES and not t.get("structural"):
            raise ConfigError(
                f"'{t['name']}' in {paths.entity_types_file} must be marked "
                "structural=true."
            )
    return data


def all_type_names(vocab: dict) -> list[str]:
    return [t["name"] for t in vocab["types"]]


def content_type_names(vocab: dict) -> list[str]:
    """Return types the LLM is allowed to extract (non-structural)."""
    return [t["name"] for t in vocab["types"] if not t.get("structural")]


def structural_type_names(vocab: dict) -> list[str]:
    return [t["name"] for t in vocab["types"] if t.get("structural")]


# Color assignment is a presentation concern owned by `visualize.py` — it
# paints a palette in pin-first, discovery-second order at render time.
# Entity-type colors used to live in entity_types.json; that field is now
# ignored if present (no-op for backward compatibility with older files).
