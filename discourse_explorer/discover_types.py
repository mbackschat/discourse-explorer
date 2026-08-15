#!/usr/bin/env python3
"""Schema-discovery utility. Run once before --index to derive a good
ENTITY_TYPES list from the actual scraped corpus.

Usage:
    uv run discourse-explorer discover-types <path>
    uv run discourse-explorer discover-types <path> --sample-size 50
"""

import argparse
import asyncio
import json
import logging
import random
import sys
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

from discourse_explorer.config import (
    ConfigError,
    RuntimeConfig,
    bootstrap,
    load_entity_types,
    site_paths_from_dir,
)
from discourse_explorer.query import topic_to_document


# ---------- Phase 1: free structural profiling ----------

def profile_structure(topics_dir: Path) -> dict:
    """No LLM. Pure metadata aggregation from the JSON dump."""
    cats, tags, users = Counter(), Counter(), Counter()
    n_topics = n_posts = 0
    for tf in topics_dir.glob("*.json"):
        try:
            topic = json.loads(tf.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("Skipping %s: %s", tf.name, exc)
            continue
        n_topics += 1
        if topic.get("category_name"):
            cats[topic["category_name"]] += 1
        for tag in topic.get("tags", []):
            name = tag["name"] if isinstance(tag, dict) else str(tag)
            tags[name] += 1
        for post in topic.get("posts", []):
            n_posts += 1
            if post.get("username"):
                users[post["username"]] += 1
    return {
        "n_topics": n_topics, "n_posts": n_posts,
        "categories": cats, "tags": tags, "users": users,
    }


# ---------- Phase 2: LLM-driven content type discovery ----------

DISCOVERY_PROMPT = """\
You are a knowledge graph schema designer. Read this forum thread and
identify what KINDS of things (entity types) are being discussed.

Output ONLY a JSON array of type labels you would use to classify
entities mentioned in this thread. Use 1-3 words per label, in
PascalCase. Examples: "Tool", "BugReport", "ApiEndpoint".

Do NOT output specific entity instances — only the *kinds*. Aim for
5-10 labels per thread, fewer if appropriate. Do not include the
universal types User, Topic, Category, or Tag — those are handled
separately. No explanations, just the JSON array.

Thread:
{thread}

Output:"""


async def discover_content_types(topics: list[dict], llm_func, sample_size: int) -> Counter:
    """Sample N topics, ask the LLM what kinds of entities exist."""
    sample = random.sample(topics, min(sample_size, len(topics)))
    type_counts: Counter = Counter()
    for i, topic in enumerate(sample, start=1):
        thread_text = topic_to_document(topic)[:4000]  # cap input
        try:
            raw = await llm_func(DISCOVERY_PROMPT.format(thread=thread_text))
            cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            labels = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"  [{i}/{len(sample)}] Skipping (parse error): {e}", file=sys.stderr)
            continue
        for label in labels:
            if isinstance(label, str) and label.strip():
                type_counts[label.strip()] += 1
        if i % 5 == 0:
            print(f"  Processed {i}/{len(sample)} samples")
    return type_counts


# ---------- Phase 3: distillation ----------

DISTILL_PROMPT = """\
You are designing the entity-type vocabulary for a knowledge graph
extracted from a Discourse forum.

Forum profile:
- Top categories: {top_categories}
- Top tags: {top_tags}

Below are entity-type labels that emerged from analyzing {sample_size}
sample threads, with their occurrence counts.

Distill these labels into a clean, non-overlapping vocabulary of
EXACTLY 4-6 CONTENT entity types that:
- Are mutually distinct (no two types describe the same kind of thing)
- Cover at least 80% of the observed labels
- Use single capitalized words (e.g. "Tool", "Issue", "Concept")

These will be added to the universal structural types
[User, Topic, Category, Tag] which are handled separately.

Raw labels with frequencies:
{labels}

Output ONLY a JSON object on a single line:
{{"content_types": ["Type1", "Type2", ...], "rationale": "one-line explanation"}}"""


async def distill_vocabulary(type_counts: Counter, struct: dict, llm_func) -> dict:
    labels_str = "\n".join(f"  {c:4d}  {t}" for t, c in type_counts.most_common(50))
    raw = await llm_func(DISTILL_PROMPT.format(
        sample_size=sum(type_counts.values()),
        top_categories=", ".join(t for t, _ in struct["categories"].most_common(10)) or "(none)",
        top_tags=", ".join(t for t, _ in struct["tags"].most_common(15)) or "(none)",
        labels=labels_str,
    ))
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


# ---------- LLM helper ----------

def _make_llm_func(rc: RuntimeConfig, model: str | None = None):
    """Returns (async_llm_callable, resolved_model_name).

    `model` overrides the default. OpenAI path: defaults to gpt-4.1-mini
    (aligns with query.py's extraction default; reasoning-heavy gpt-5-series
    is avoided for the same latency reasons that apply to indexing — see
    CONFIG_LOG.md). Ollama path: defaults to the run's EXTRACTION_MODEL.
    """
    if rc.is_openai:
        from lightrag.llm.openai import openai_complete_if_cache
        chosen = model or "gpt-4.1-mini"
        async def _llm(prompt):
            return await openai_complete_if_cache(chosen, prompt)
        return _llm, chosen
    else:
        from lightrag.llm.ollama import ollama_model_complete
        chosen = model or rc.extraction_model
        ollama_host = rc.ollama_host
        async def _llm(prompt):
            return await ollama_model_complete(
                prompt,
                hashing_kv=type("FakeKV", (), {
                    "global_config": {"llm_model_name": chosen},
                })(),
                host=ollama_host,
                options={"num_ctx": 32768},
            )
        return _llm, chosen


# ---------- Artifact I/O ----------

def _artifact_path(data_dir: Path) -> Path:
    return site_paths_from_dir(data_dir).data_dir / "discovery_result.json"


def _write_artifact(path: Path, artifact: dict) -> None:
    path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))


def _print_artifact_summary(artifact: dict, top: int) -> None:
    """Render a review-friendly summary of a discovery artifact."""
    print("Discovery artifact summary")
    print(f"  Sample size: {artifact.get('sample_size', '?')}")
    print(f"  Provider/Model: {artifact.get('provider', '?')}/{artifact.get('model', '?')}")
    print(f"  Corpus: {artifact.get('n_topics', '?')} topics, {artifact.get('n_posts', '?')} posts")
    top_tags = artifact.get("top_tags", [])
    if top_tags:
        print(f"  Top 10 tags: {', '.join(t for t, _ in top_tags[:10])}")
    print(f"  Distinct labels discovered: {artifact.get('distinct_labels', '?')}")
    labels = artifact.get("all_labels", [])
    if labels:
        print(f"\n  Top {min(top, len(labels))} labels by frequency:")
        for label, count in labels[:top]:
            print(f"    {count:4d}  {label}")
    distilled = artifact.get("distilled_content_types")
    if distilled:
        print(f"\n  Distilled content types: {distilled}")
        print(f"  Rationale: {artifact.get('rationale', '(none)')}")
    else:
        print("\n  (No distillation stored in this artifact — run without --no-distill to produce one.)")


def _write_entity_types_json(
    path: Path,
    content_types: list[str],
    existing: dict | None,
) -> list[dict]:
    """Merge discovered content types into <data-dir>/config/entity_types.json.

    Structural types are preserved verbatim from `existing`. Content types
    that drop out of the new list are removed. Colors are NOT managed here —
    the visualizer paints names from a palette at render time
    (`visualize._assign_colors`), so entity_types.json holds vocabulary
    only: `{name, structural}`. Legacy `color` fields in `existing` are
    dropped silently.
    """
    structural_entries: list[dict] = []
    if existing:
        for t in existing.get("types", []):
            if t.get("structural"):
                structural_entries.append({
                    "name": t["name"],
                    "structural": True,
                })

    new_content_entries = [
        {"name": name, "structural": False} for name in content_types
    ]

    payload = {
        "version": 2,
        "types": structural_entries + new_content_entries,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return new_content_entries


# ---------- Orchestration ----------

async def main_async(
    rc: RuntimeConfig,
    sample_size: int,
    model: str | None = None,
    top: int = 15,
    show_artifact: bool = False,
    no_distill: bool = False,
):
    paths = rc.paths()
    data_dir = rc.data_dir
    artifact_path = _artifact_path(data_dir)

    # --show-artifact: read the cached artifact, print, exit. No LLM cost.
    if show_artifact:
        if not artifact_path.exists():
            raise ConfigError(
                f"No discovery artifact at {artifact_path}. "
                "Run `discover_types` first to produce one."
            )
        artifact = json.loads(artifact_path.read_text())
        _print_artifact_summary(artifact, top)
        return

    topics_dir = paths.topics_dir
    if not topics_dir.exists():
        raise ConfigError(f"No scraped data at {topics_dir}")

    print(f"Phase 1: Profiling structural metadata ({topics_dir})...")
    struct = profile_structure(topics_dir)
    print(f"  Topics: {struct['n_topics']}, Posts: {struct['n_posts']}")
    print(f"  Categories: {len(struct['categories'])}, Tags: {len(struct['tags'])}, Users: {len(struct['users'])}")
    print(f"  Top 10 tags: {', '.join(t for t, _ in struct['tags'].most_common(10))}")

    provider = "OpenAI" if rc.is_openai else "Ollama"
    llm, resolved_model = _make_llm_func(rc, model)
    print(f"\nPhase 2: LLM discovery on {sample_size} sampled topics...")
    print(f"  Provider: {provider}, Model: {resolved_model}")
    topics: list[dict] = []
    for tf in topics_dir.glob("*.json"):
        try:
            topics.append(json.loads(tf.read_text()))
        except json.JSONDecodeError as exc:
            logger.warning("Skipping %s: %s", tf.name, exc)
    type_counts = await discover_content_types(topics, llm, sample_size)
    print(f"  Distinct labels emerged: {len(type_counts)}")
    print(f"\n  Top {top} labels by frequency:")
    for label, count in type_counts.most_common(top):
        print(f"    {count:4d}  {label}")

    # Persist partial artifact after Phase 2 so the label data survives even if
    # the user Ctrl-C's or --no-distill is set. Reviewable later via --show-artifact.
    artifact = {
        "sample_size": sample_size,
        "model": resolved_model,
        "provider": provider,
        "n_topics": struct["n_topics"],
        "n_posts": struct["n_posts"],
        "top_categories": struct["categories"].most_common(20),
        "top_tags": struct["tags"].most_common(30),
        "distinct_labels": len(type_counts),
        "all_labels": type_counts.most_common(),
        "distilled_content_types": None,
        "rationale": "",
    }
    _write_artifact(artifact_path, artifact)
    print(f"\n  Artifact written (labels only): {artifact_path}")
    print(f"  Review later with: uv run discourse-explorer discover-types --show-artifact --top N")

    if no_distill:
        print("\n(Skipping Phase 3 distillation per --no-distill.)")
        return

    print(f"\nPhase 3: Distilling into final vocabulary...")
    result = await distill_vocabulary(type_counts, struct, llm)
    content_types = result.get("content_types", [])

    # Update artifact with distillation result.
    artifact["distilled_content_types"] = content_types
    artifact["rationale"] = result.get("rationale", "")
    _write_artifact(artifact_path, artifact)

    # Merge into <data-dir>/config/entity_types.json. Structural types are
    # preserved; only the content-type portion changes. The JSON file is the
    # single source of truth consumed by query.py (Pass 2 allowed types) and
    # visualize.py (color map).
    existing_vocab = None
    try:
        existing_vocab = load_entity_types(data_dir)
    except ConfigError:
        existing_vocab = None
    new_entries = _write_entity_types_json(
        paths.entity_types_file, content_types, existing_vocab,
    )

    print("\n" + "=" * 60)
    print(f"Wrote {paths.entity_types_file}")
    print("=" * 60)
    print(f"\nContent types now in the vocabulary ({len(new_entries)}):")
    for t in new_entries:
        print(f"  - {t['name']}")
    print(f"\nStructural types (unchanged, owned by Pass 1):")
    if existing_vocab:
        for t in existing_vocab.get("types", []):
            if t.get("structural"):
                print(f"  - {t['name']}")
    print(f"\nDistillation rationale: {result.get('rationale', '(none provided)')}")
    print(f"\nThe vocabulary is live — no source-code edits needed. Re-run "
          f"`query --index --clear` to rebuild the graph with the new types.\n")


def main():
    parser = argparse.ArgumentParser(description="Discover entity-type vocabulary from scraped Discourse data.")
    parser.add_argument(
        "path", nargs="?", default=None, type=Path,
        help="Path to scraped data directory. Falls back to DISCOURSE_DATA_DIR in the project-root .env.",
    )
    parser.add_argument("--sample-size", type=int, default=30, help="Topics to LLM-sample (default: 30).")
    parser.add_argument(
        "--model", default=None,
        help="LLM for discovery calls. OpenAI default: gpt-4.1-mini. Ollama default: EXTRACTION_MODEL.",
    )
    parser.add_argument(
        "--top", type=int, default=15,
        help="How many top raw labels to print (default: 15).",
    )
    parser.add_argument(
        "--show-artifact", action="store_true",
        help="Read and print the prior run's artifact (no LLM cost). Requires a previous run.",
    )
    parser.add_argument(
        "--no-distill", action="store_true",
        help="Run Phase 1+2 only; skip the Phase 3 distillation LLM call. Cheaper; useful for review.",
    )
    args = parser.parse_args()

    try:
        rc = bootstrap(args.path)
    except ConfigError as e:
        parser.error(str(e))

    try:
        asyncio.run(main_async(
            rc, args.sample_size, args.model, args.top,
            show_artifact=args.show_artifact, no_distill=args.no_distill,
        ))
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
