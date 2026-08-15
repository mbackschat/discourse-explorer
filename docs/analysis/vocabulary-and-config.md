# Vocabulary and configuration

Deep reference for the two-tier config system, the entity-type vocabulary JSON, the pinned relationship vocabulary, and every per-run env var. Source of truth for what lives where and which tier wins.

## Two-tier config layout

A single project checkout supports multiple forums. Each forum gets its own data directory with its own config. A 1-line selector at the project root points at whichever forum is currently "active."

| Location | Purpose | Who writes it |
|---|---|---|
| `<project-root>/.env` | **Selector only** — sets `DISCOURSE_DATA_DIR` so unflagged CLIs pick up the right data dir. Shell exports override. | User (one line) |
| `<project-root>/.env.example` | Documents the selector | Checked-in template |
| `<data-dir>/config/.env` | **All per-run env vars** (URL, auth, models, embedding, gleaning, concurrency, rerank). Authoritative. | User, or persisted by `/index-and-embed` when you pick values in the skill |
| `<data-dir>/config/entity_types.json` | Entity-type vocabulary (names + `structural` flag). Single source of truth for `query.py` (Pass 2 allowed types) and `visualize.py` (legend order). No colors. | `discover_types.py`, hand-tunable |
| `discourse_explorer/config/env.example` | Template for `<data-dir>/config/.env` | Checked-in template |
| `discourse_explorer/config/entity_types.example.json` | Template vocabulary | Checked-in template |

### Resolution priority (highest wins)

1. **CLI flag** (e.g. `--output`, `--path`, positional, `--extraction-model`).
2. **Shell export** of the env var.
3. **`<data-dir>/config/.env`** (loaded with `override=True`).
4. **`<project-root>/.env`** (loaded first, non-override).
5. **Hardcoded default** in `config.py`.

`<data-dir>/config/.env` is loaded with `override=True`, so identical keys in both dotenv files — the data-dir value wins. This is load-bearing for multi-forum setups on a single checkout.

## Bootstrap flow

`config.bootstrap(cli_data_dir: Path | None) -> RuntimeConfig` is the single entry point every CLI calls once after arg parsing. Order:

1. If `<project-root>/.env` exists, `load_dotenv(..., override=False)` — `DISCOURSE_DATA_DIR` flows from it without clobbering real shell exports.
2. Resolve data dir: explicit `cli_data_dir` ≻ `DISCOURSE_DATA_DIR` env. Raise `ConfigError` if both unset.
3. If `<data-dir>/config/.env` exists, `load_dotenv(..., override=True)`.
4. `_maybe_migrate(paths)` — if the data dir exists but `config/entity_types.json` doesn't, seed it from `_DEFAULT_ENTITY_TYPES`. Idempotent. Doesn't create `.env` (credentials are an explicit user action).
5. Build and return a frozen `RuntimeConfig` dataclass.

Helpers should receive the returned `RuntimeConfig` as a parameter. Don't re-read `os.environ` ad-hoc inside the code paths — that bypasses the layering.

### `RuntimeConfig` fields

Frozen dataclass in `config.py`. Grouped by concern:

- Target site + auth: `discourse_url`, `discourse_api_key`, `discourse_api_username`, `discourse_cookie`, `discourse_username`, `discourse_password`.
- Resolved path: `data_dir` (absolute).
- Models: `extraction_model`, `query_model`, `openai_api_key`, `ollama_host`.
- Embeddings: `embed_model` (Ollama), `ollama_embed_dim`, `openai_embed_model`, `openai_embed_dim`.
- Extraction behavior: `gleaning`.
- Concurrency: `llm_model_max_async`, `max_parallel_insert` (0 = use provider-specific default in `_get_rag`).
- Rerank: `rerank_provider`, `rerank_model`, `rerank_api_key`, `rerank_base_url`.

Properties:

- `rc.is_openai` — `bool(rc.openai_api_key)`.
- `rc.default_extraction_model()` — provider-aware fallback. Explicit `EXTRACTION_MODEL` overrides; if it's at the Ollama default AND `OPENAI_API_KEY` is set, returns `OPENAI_EXTRACTION_MODEL = "gpt-4.1-mini"`.
- `rc.paths() -> SitePaths` — all filesystem paths rooted at `data_dir`.

### `SitePaths` fields

```python
@dataclass(frozen=True)
class SitePaths:
    data_dir: Path
    topics_dir: Path          # <data-dir>/topics/
    sync_state_file: Path     # <data-dir>/sync_state.json
    index_file: Path          # <data-dir>/index.json
    categories_file: Path     # <data-dir>/categories.json
    config_dir: Path          # <data-dir>/config/
    env_file: Path            # <data-dir>/config/.env
    entity_types_file: Path   # <data-dir>/config/entity_types.json
```

## Entity-type vocabulary (`entity_types.json`)

Schema v2 holds names + structural flags only. Colors are the visualizer's concern (applied via `_PALETTE` at render time).

```json
{
  "version": 2,
  "types": [
    {"name": "User",      "structural": true},
    {"name": "Topic",     "structural": true},
    {"name": "Category",  "structural": true},
    {"name": "Tag",       "structural": true},
    {"name": "Model",     "structural": false},
    {"name": "Issue",     "structural": false},
    ...
  ]
}
```

### Structural types

`config.py`:

```python
STRUCTURAL_TYPE_NAMES: tuple[str, ...] = ("User", "Topic", "Category", "Tag")
```

These four are **load-bearing**: `_topic_to_custom_kg` in `query.py` emits nodes typed `user` / `topic` / `category` / `tag` (lowercase — matches LightRAG's Pass 2 auto-lowering so the Counter merge can compare them). The JSON keeps PascalCase names as canonical labels.

`load_entity_types(data_dir)` validates:

- All four `STRUCTURAL_TYPE_NAMES` are present as entries.
- Each has `structural: true`.

Raises `ConfigError` otherwise. Legacy `color` fields in older files are silently ignored — backward compatible with v1 that had them.

Helpers:

- `all_type_names(vocab) -> list[str]` — every type name.
- `content_type_names(vocab) -> list[str]` — `structural: false` only. This is what `addon_params["entity_types"]` receives in `_get_rag`, so Pass 2 can't intentionally extract structural types.
- `structural_type_names(vocab) -> list[str]` — `structural: true` only.

### Migration seed

`_DEFAULT_ENTITY_TYPES` in `config.py` is an embedded fallback used by `_maybe_migrate` to seed `entity_types.json` for data dirs created before the JSON file existed. **Not a runtime source of truth** — after migration, the JSON file is authoritative and the constant is never consulted again.

### Discovery

`discourse_explorer.discover_types <data-dir>` re-derives the content types from your corpus. Three phases:

1. **Phase 1** — profile structural metadata (categories, tags, users) from the scraped JSON. No LLM cost.
2. **Phase 2** — sample N topics (`--sample-size`, default 30), ask the LLM what *kinds* of entities appear. Collects raw labels with frequencies.
3. **Phase 3** — distill the raw labels into 4–6 content types.

Writes the result to `<data-dir>/config/entity_types.json` preserving structural entries, merging in the new content types. Also persists a full audit artifact to `<data-dir>/discovery_result.json` for review.

Flags: `--sample-size`, `--model`, `--top`, `--show-artifact` (read-only review of prior run, no LLM cost), `--no-distill` (skip Phase 3).

## Relationship vocabulary

### Pinned relations (`STRUCTURAL_REL_PINS`)

```python
class StructuralRelPin(NamedTuple):
    id: str
    display_name: str
    keywords_csv: str

STRUCTURAL_REL_PINS: tuple[StructuralRelPin, ...] = (
    StructuralRelPin("user_posted",       "Posted",      "posted, authored, participated"),
    StructuralRelPin("topic_tagged",      "Tagged",      "tagged, tag, labeled"),
    StructuralRelPin("topic_in_category", "Categorized", "posted in, category, section"),
)
```

Single source of truth for Discourse-universal relationship primitives. `query.py`'s `_topic_to_custom_kg` reads `config.STRUCTURAL_REL_KEYWORDS[pin_id]` (derived from the tuple) to author canonical edge keywords. `rel_clusters.py` imports the full pin tuple and routes matching keywords to their fixed bucket.

```python
STRUCTURAL_REL_KEYWORDS: dict[str, str] = {
    p.id: p.keywords_csv for p in STRUCTURAL_REL_PINS
}
```

Derived — never define both sides independently; they can drift.

### Why free-form for LLM-extracted keywords

Relationship keywords are **not** constrained at extraction time. The split:

- Entity types serve classification (UI color, filter, group) → tight vocabulary helps.
- Relation keywords serve *retrieval* (embedded into relation VDBs, matched against user queries in global-mode) → free-form variety gives richer embeddings and broader semantic match.

Only *structural* edges we author ourselves get canonical keywords (via `STRUCTURAL_REL_KEYWORDS`). Don't constrain LLM-extracted keywords globally — it would degrade global-mode retrieval.

## Per-run env vars

All in `<data-dir>/config/.env`.

### Target site + auth (scraper only)

| Var | Required | Purpose |
|---|---|---|
| `DISCOURSE_URL` | scraper | CLI positional overrides. `https://...` form. |
| `DISCOURSE_API_KEY` + `DISCOURSE_API_USERNAME` | one of | Preferred auth. Generate at Admin → API → New API Key. |
| `DISCOURSE_COOKIE` | one of | Session cookie (`_t` value). Expires in a few weeks. |
| `DISCOURSE_USERNAME` + `DISCOURSE_PASSWORD` | one of | OIDC / Keycloak automated login. |

Priority at runtime: API key ≻ cookie ≻ OIDC. Set in `auth.py::get_session(rc)`.

### LLM models + provider switch

| Var | Default | CLI override | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | unset | — | Set → OpenAI provider; unset → Ollama. |
| `EXTRACTION_MODEL` | `qwen2.5:14b` → fallback `gpt-4.1-mini` when `OPENAI_API_KEY` set | `--extraction-model` | `gpt-4.1-mini` chosen for `<\|#\|>` tuple extraction discipline. Avoid gpt-5-series for indexing — reasoning latency is ~5× higher. |
| `QUERY_MODEL` | = extraction | `--query-model` | gpt-5-series *is* appropriate here for synthesis. |
| `OLLAMA_HOST` | `http://localhost:11434` | — | |
| `FORCE_LLM_SUMMARY_ON_MERGE` | `999` (`config.SUMMARY_ON_MERGE_DEFAULT`, **not** LightRAG's `8`) | — | Per-entity summary cascade threshold. Passed explicitly to the LightRAG constructor, so it never depends on how the run was launched. Lower it to `8` to restore LightRAG's summarizing behaviour at 3–5× the cost. |

### Embeddings

| Var | Default | CLI override | Notes |
|---|---|---|---|
| `OPENAI_EMBED_MODEL` | `text-embedding-3-large` | — | Change requires `--index --clear`. |
| `OPENAI_EMBED_DIM` | auto-resolved from `_OPENAI_EMBED_DIMS` | — | Override only for non-standard models. |
| `EMBED_MODEL` | `nomic-embed-text` | — | Ollama embedding model. |
| `OLLAMA_EMBED_DIM` | `768` | — | Must match the model's actual dim. |

**Switching provider or embedding model requires `--clear`** — Faiss indices are dim-bound.

### Gleaning + concurrency

| Var | Default | CLI override | Notes |
|---|---|---|---|
| `GLEANING` | `1` | `--gleaning N` | `0=cheap/baseline, 1=recommended, 2+=diminishing returns`. Per-run override threaded through `_get_rag` to `entity_extract_max_gleaning`. |
| `LLM_MODEL_MAX_ASYNC` | `0` → provider default (OpenAI 8, Ollama 1) | `--llm-concurrency N` | Probe with `query --detect-limits`. Tier 3 OpenAI typically supports `13`. Also caps the Pass 3 enrichment semaphore. |
| `MAX_PARALLEL_INSERT` | `0` → `4` | `--parallel-insert N` | Lower reduces rate-limit stutter at throughput cost. |

### Rerank (query-time only)

| Var | Default | Notes |
|---|---|---|
| `RERANK_PROVIDER` | unset | One of `jina`, `cohere`, `ali`. Empty = disabled + warning suppressed via `QueryParam.enable_rerank=False`. |
| `RERANK_MODEL` | provider default | e.g. `jina-reranker-v2-base-multilingual` for `jina`. |
| `RERANK_API_KEY` | — | Provider-specific. |
| `RERANK_BASE_URL` | provider default | Override for self-hosted endpoints. TEI/llama-server bge via `provider=cohere` + local URL. |

`_get_rag` calls `_build_rerank_func(rc)` which binds env values at construction time and passes a `rerank_model_func` to `LightRAG(...)`. No re-index needed — rerank is pure query-time.

### Retrieval knobs (LightRAG `QueryParam`, env-only)

No CLI flag; set in `<data-dir>/config/.env` or inline per-invocation:

| Var | Default | Effect |
|---|---|---|
| `TOP_K` | `60` | Entities in `local` / relationships in `global`. Raise for breadth. |
| `CHUNK_TOP_K` | `20` | Raw chunks kept after rerank. |
| `MAX_ENTITY_TOKENS` | `6000` | Per-query entity-context budget. |
| `MAX_RELATION_TOKENS` | `8000` | Per-query relation-context budget. |
| `MAX_TOTAL_TOKENS` | `30000` | Overall ceiling sent to LLM. |

Authoritative reference: `docs/lightrag/ProgramingWithCore.md` §QueryParam.

## Codebase-level constants (in `config.py`, not env)

Stable across all data dirs. Don't move to env unless they really vary per-run.

- **Rate limits** (scraper): `REQUEST_DELAY = 1.0 s`, `MAX_RETRIES = 3`, `RETRY_BACKOFF = 2.0`.
- **Embed-dim lookup**: `_OPENAI_EMBED_DIMS` maps known model → dim.
- **Extraction fallback**: `OPENAI_EXTRACTION_MODEL = "gpt-4.1-mini"`.
- **Structural type names**: `STRUCTURAL_TYPE_NAMES = ("User", "Topic", "Category", "Tag")`.
- **Structural rel pins**: `STRUCTURAL_REL_PINS` (tuple of `StructuralRelPin`) → derived `STRUCTURAL_REL_KEYWORDS` dict.
- **Migration seed**: `_DEFAULT_ENTITY_TYPES` (used once, not a runtime source of truth).

## Data-path resolution summary

All tools resolve the data dir the same way:

```
CLI flag (--output / --path / positional)
  ≻ DISCOURSE_DATA_DIR (shell export)
  ≻ DISCOURSE_DATA_DIR in <project-root>/.env
  → else: ConfigError
```

`scraper` additionally needs a URL (CLI positional ≻ `DISCOURSE_URL` in `<data-dir>/config/.env`). All other tools (`query`, `visualize`, `stats`, `discover_types`) only need the path.
