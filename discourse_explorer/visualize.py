#!/usr/bin/env python3
"""
Interactive HTML visualization of the GraphRAG knowledge graph.

Generates a small HTML file (`visualize/graph.html`, ~70 KB) that loads an
interactive force-directed graph explorer. The node + edge payload lives in
a sibling `visualize/data.js` (~22 MB for a 16k-node graph) — the whole
`visualize/` directory must be kept together for the visualization to load.
This split is a deliberate trade-off: on-disk it's no smaller, but the HTML
becomes tiny (fast to iterate on) and the browser caches the data payload
across reloads. Nodes colored by entity type (super-category), with filtering
by type/degree/weight, search with neighbor highlighting, and a detail panel
with grouped connections.

Usage:
    uv run discourse-explorer visualize ./path/to/data
    uv run discourse-explorer visualize ./path/to/data --open
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from pyvis.network import Network

from discourse_explorer.config import (
    STRUCTURAL_TYPE_NAMES,
    ConfigError,
    SitePaths,
    bootstrap,
    load_entity_types,
)
from discourse_explorer import rel_clusters as _rel_clusters
from discourse_explorer.rel_clusters import ClusterMap

# ---------------------------------------------------------------------------
# Constants — keep magic strings in one place. The string-typed contracts
# below are also referenced by the JS side (graph.js); changes must land
# on both ends.
# ---------------------------------------------------------------------------

# Sink bucket for unrecognized entity types and rel-clusters.
OTHER_BUCKET = "Other"
# Fallback when an entity has no entity_type attribute on the graphml node.
UNKNOWN_ENTITY_TYPE = "Unknown"
# Multi-phrase descriptions are joined with this byte. Mirrors LightRAG's
# convention; the JS edge-detail panel splits on it to render per-phrase
# bullet lists.
DESC_SEP = "\x1f"
# LightRAG's literal GRAPH_FIELD_SEP — used in graphml `description` (joining
# merged per-chunk descriptions of the same entity/relation) AND `source_id`
# (joining chunk-id lists). We normalize it to DESC_SEP at load time so the
# rest of the pipeline only has to know one separator. Note: phrase order in
# `description` does NOT align with chunk order in `source_id` — LightRAG's
# `_merge_nodes_then_upsert` sorts descriptions by (timestamp, -length) and
# may LLM-summarize the list; do not zip the two.
LIGHTRAG_GRAPH_FIELD_SEP = "<SEP>"
SOURCE_ID_SEP = LIGHTRAG_GRAPH_FIELD_SEP
# Cat-pair keys for inter-category edge aggregates (sorted-join). The JS
# side reads `superCategoryEdges` keys with the same shape.
CAT_EDGE_SEP = "|"
# Type-pair keys for the (Cat, EntityType) cross-tab — "Cat|Type||Cat|Type".
TYPE_EDGE_SEP = "||"
# Tooltip body cap. ~240 chars fits 4-5 lines wrapped at 360px (the
# .vis-tooltip max-width set in graph.css).
TOOLTIP_TRUNCATE = 240
# Canvas label cap. Topic / Issue / Guide / Document entities frequently
# carry sentence-length names; vis-network draws the whole string at the
# node, which collides into mush in dense regions. The cap applies ONLY
# to `node.label` (what vis-network draws); `node.id` keeps the full
# text so search, detail panels, copy, and Markdown emitters are
# unaffected. Single-char ellipsis `…` (U+2026) keeps the visible width
# tight: max rendered label = LABEL_TRUNCATE + 1.
LABEL_TRUNCATE = 30
LABEL_ELLIPSIS = "…"
# Per-post body cap inside GRAPH_META.topicIndex (used by the topic
# preview modal). 4 KB fits a long forum post without truncation in
# the typical case; runaway long posts get cut off so a single
# pathological topic can't bloat graph.html unboundedly.
TOPIC_POST_LEN = 4000

# Color palette — single source of truth for the visualizer.
# Assigned to bucket names in deterministic order (see `_assign_colors`):
# pinned/structural first, discovered after. `_PALETTE[i % len]` wraps if
# the legend exceeds 16 entries. "Other" always gets the reserved gray.
_PALETTE: list[str] = [
    "#EF5350", "#42A5F5", "#66BB6A", "#FFA726",
    "#AB47BC", "#26A69A", "#EC407A", "#78909C",
    "#FFCA28", "#5C6BC0", "#8D6E63", "#26C6DA",
    "#7E57C2", "#9CCC65", "#FF7043", "#29B6F6",
]
_OTHER_COLOR = "#888888"

# Static assets — dedicated files for syntax highlighting + linting.
# Read at import time and wrapped back into <style>/<script> tags so the
# generated graph.html stays self-contained except for its sibling data.js.
_STATIC_DIR = Path(__file__).parent / "static"
CUSTOM_CSS = "<style>\n" + (_STATIC_DIR / "graph.css").read_text() + "</style>\n"
CUSTOM_JS = "<script>\n" + (_STATIC_DIR / "graph.js").read_text() + "</script>\n"

_LAYOUT_CACHE_SCHEMA = 1


# ---------------------------------------------------------------------------
# Color assignment
# ---------------------------------------------------------------------------

def _assign_colors(
    pinned_names: list[str],
    discovered_names: list[str],
) -> dict[str, str]:
    """Paint names from the palette in pin-first, discovery-second order.

    Pinned buckets always occupy palette slots 0..N-1, so the same pin name
    gets the same color across any corpus. Discovered buckets continue in
    slots N..N+M-1, ordered as caller passes them (typically size-rank).
    Palette wraps with `% len`. `Other` always gets `_OTHER_COLOR`.
    """
    colors: dict[str, str] = {}
    for i, name in enumerate(pinned_names):
        colors[name] = _PALETTE[i % len(_PALETTE)]
    offset = len(pinned_names)
    for i, name in enumerate(discovered_names):
        colors[name] = _PALETTE[(offset + i) % len(_PALETTE)]
    colors[OTHER_BUCKET] = _OTHER_COLOR
    return colors


def _build_entity_color_map(data_dir) -> dict[str, str]:
    """Entity-type color map: structural pins first, then discovered content
    types from `entity_types.json` in their declared order."""
    vocab = load_entity_types(data_dir)
    pinned = [t["name"] for t in vocab["types"] if t.get("structural")]
    discovered = [t["name"] for t in vocab["types"] if not t.get("structural")]
    return _assign_colors(pinned, discovered)


def _get_super_category(entity_type: str, lc_to_canonical: dict[str, str]) -> str:
    """Resolve a raw entity_type to the display super-category, falling back
    to OTHER_BUCKET. `lc_to_canonical` is a `{lower(name): name}` index of the
    color map — built once by the caller so the lookup is O(1) per node
    instead of O(types) per node.
    """
    if not entity_type:
        return OTHER_BUCKET
    return lc_to_canonical.get(entity_type.strip().lower(), OTHER_BUCKET)


# ---------------------------------------------------------------------------
# Relationship classification
# ---------------------------------------------------------------------------
# Bucket names come from `<data-dir>/visualize/cache/rel-clusters.json`
# (built by `rel_clusters.load_or_build` on the first viz run). Bucket
# names are Discourse-universal pins (`config.STRUCTURAL_REL_PINS`) +
# corpus-derived clusters. Colors come from `_assign_colors()` applied to
# (pinned_names, discovered_names).

def _classify_edge(keywords_str: str, cluster_map: ClusterMap) -> str:
    """Return the bucket name for an edge given its `keywords` string.

    Counter-based vote across the comma-separated keywords; ties are broken
    by bucket size — bucket index in `cluster_map.buckets` is already
    size-rank order (index 0 = largest), so a `min(...)` on bucket index
    naturally favors the larger bucket. Edges with no recognized keywords
    land in OTHER_BUCKET.
    """
    if not keywords_str:
        return cluster_map.buckets[cluster_map.other_idx].name

    matches: Counter = Counter()
    for kw in keywords_str.replace("<SEP>", ",").split(","):
        kw = kw.strip().lower()
        idx = cluster_map.keyword_to_bucket_idx.get(kw)
        if idx is not None:
            matches[idx] += 1

    if not matches:
        return cluster_map.buckets[cluster_map.other_idx].name

    bidx = min(matches.items(), key=lambda x: (-x[1], x[0]))[0]
    return cluster_map.buckets[bidx].name


def _truncate(text: str, max_len: int = 300) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _truncate_label(text: str) -> str:
    """Canvas-display truncator. Caps at LABEL_TRUNCATE chars + a single
    `…` ellipsis (1 char wide). Used only for vis-network's drawn label;
    `node.id` keeps the full text so search / detail panels / copy
    formats see the original.
    """
    if not text or len(text) <= LABEL_TRUNCATE:
        return text
    return text[:LABEL_TRUNCATE] + LABEL_ELLIPSIS


def _format_data_dir_for_meta(data_dir: Path) -> str:
    resolved = data_dir.resolve()
    try:
        rel = resolved.relative_to(Path.cwd().resolve())
    except ValueError:
        return str(resolved)
    return str(rel) or "."


# ---------------------------------------------------------------------------
# Layout cache (positions persisted across runs, keyed on graph signature)
# ---------------------------------------------------------------------------

def _graph_signature(G: nx.Graph) -> str:
    """sha256 over structure only (not node attributes).

    Layout depends on nodes + edges, not on `entity_type` or description text.
    Attribute changes (e.g. a Pass 3 enrichment) don't invalidate the cached
    positions — which is the whole point of caching.
    """
    h = hashlib.sha256()
    for n in sorted(G.nodes()):
        h.update(n.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    h.update(b"\x01")
    for src, tgt in sorted((str(s), str(t)) for s, t in G.edges()):
        h.update(src.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
        h.update(tgt.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return "sha256:" + h.hexdigest()


def _load_cached_layout(path: Path, signature: str) -> dict | None:
    """Return cached positions if signature matches, else None."""
    if not path.exists():
        print(f"  Layout cache missing at {path.name} — computing from scratch...")
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  Layout cache unreadable ({type(e).__name__}); recomputing...")
        return None
    if data.get("schema") != _LAYOUT_CACHE_SCHEMA:
        print(f"  Layout cache schema mismatch (expected {_LAYOUT_CACHE_SCHEMA}, "
              f"got {data.get('schema')}); recomputing...")
        return None
    if data.get("graph_signature") != signature:
        print(f"  Graph structure changed since last layout "
              f"(cache signature {data.get('graph_signature','?')[:16]}... != "
              f"current {signature[:16]}...); recomputing...")
        return None
    positions = data.get("positions") or {}
    print(f"  Using cached layout from {path.name} "
          f"(generated {data.get('generated_at', '?')}, "
          f"{len(positions):,} positions)")
    return {k: (float(v[0]), float(v[1])) for k, v in positions.items()}


def _save_layout_cache(path: Path, positions: dict, signature: str,
                       scale: float) -> None:
    payload = {
        "schema": _LAYOUT_CACHE_SCHEMA,
        "graph_signature": signature,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scale": scale,
        "positions": {k: [v[0], v[1]] for k, v in positions.items()},
    }
    path.write_text(json.dumps(payload) + "\n")
    print(f"  Layout cache saved to {path.name} ({len(positions):,} positions).")


def _compute_layout(G: nx.Graph, cache_path: Path, scale: float = 2000.0) -> dict:
    """Pre-compute layout positions; cached by graph signature.

    Caches positions to `cache_path` keyed on the graph's structural signature.
    On subsequent runs, if the graph hasn't changed structurally, load the
    cache instead of re-running the layout.

    Algorithm choice depends on graph size:

    - n < 1000: kamada_kawai_layout. Minimizes pairwise stress and produces
      dramatically better spread on small dense graphs (~1 s on 400 nodes).
      O(n^2), so it becomes impractical above ~1000 nodes.
    - n >= 1000: spring_layout with explicit k = 5 / sqrt(n). networkx's
      default k = 1 / sqrt(n) packs hub clusters into a hairball; bumping
      it 5x gives a more readable spread without changing the iteration
      count or seed. ~6 min on the 16k canonical corpus.
    """
    if G.number_of_nodes() == 0:
        return {}
    signature = _graph_signature(G)
    cached = _load_cached_layout(cache_path, signature)
    if cached is not None:
        return cached
    n = G.number_of_nodes()
    if n < 1000:
        print(f"  Computing kamada-kawai layout (n={n})...")
        pos = nx.kamada_kawai_layout(G, scale=scale)
    else:
        k = 5.0 / (n ** 0.5)
        print(f"  Computing spring layout (n={n}, k={k:.4f})...")
        pos = nx.spring_layout(G, k=k, iterations=50, seed=42, scale=scale)
    positions = {node: (float(xy[0]), float(xy[1])) for node, xy in pos.items()}
    _save_layout_cache(cache_path, positions, signature, scale)
    return positions


# ---------------------------------------------------------------------------
# Topic provenance — chunk_id → topic_id resolution at build time.
# ---------------------------------------------------------------------------
# Every graphml node + edge carries a `<SEP>`-joined list of chunk IDs in
# `source_id`. Two LightRAG kv_stores resolve those chunks to topics:
# - `kv_store_text_chunks.json` — chunk → `full_doc_id`
# - `kv_store_full_docs.json`   — `doc-<hash>` → `file_path: 'topic-NNNN.json'`
#
# `full_doc_id` comes in two shapes on this corpus:
# - `topic-NNNN`  → Pass-1 custom_kg seed; strip prefix.
# - `doc-<hash>`  → Pass-2 LLM extraction; resolve via kv_store_full_docs.json.
#
# Resolution is 100% on the canonical corpus (no unresolved chunks). The
# pre-pass runs once at build start; per-node + per-edge lookups during the
# main passes are O(1) hash hits against the resulting `chunk_to_topic` dict.

def _resolve_topic_id(full_doc_id: str, docs: dict) -> str | None:
    """LightRAG full_doc_id → topic id (numeric string), or None."""
    if not full_doc_id:
        return None
    if full_doc_id.startswith("topic-"):
        return full_doc_id[len("topic-"):]
    if full_doc_id.startswith("doc-"):
        d = docs.get(full_doc_id)
        if not d:
            return None
        fp = d.get("file_path") or ""
        if fp.startswith("topic-") and fp.endswith(".json"):
            return fp[len("topic-"):-len(".json")]
    return None


def _load_chunk_to_topic(graphrag_dir: Path) -> dict[str, str]:
    """Build `chunk_id → topic_id` by joining the two LightRAG kv_stores.

    Returns `{}` if either file is missing or unreadable — provenance
    just won't render in the panel; the rest of the build is unaffected.
    """
    chunks_path = graphrag_dir / "kv_store_text_chunks.json"
    docs_path = graphrag_dir / "kv_store_full_docs.json"
    if not chunks_path.exists():
        return {}
    try:
        chunks = json.loads(chunks_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    docs: dict = {}
    if docs_path.exists():
        try:
            docs = json.loads(docs_path.read_text())
        except (json.JSONDecodeError, OSError):
            docs = {}
    out: dict[str, str] = {}
    for cid, rec in chunks.items():
        fdid = (rec or {}).get("full_doc_id") or (rec or {}).get("source_id") or ""
        tid = _resolve_topic_id(fdid, docs)
        if tid:
            out[cid] = tid
    return out


# ---------------------------------------------------------------------------
# Time-window helpers (source-post time, not index time).
#
# The graphml's `created_at` on every node + edge reflects the indexer run,
# not the underlying forum post — degenerate for time-windowing on a
# re-indexed corpus (every value clusters around the index timestamp).
# Real source-time lives in topic JSONs (`created_at` ISO-8601 from
# Discourse). Since topic provenance shipped, every node + edge already
# carries `topicIds` and `topicIndex[tid].createdAt` is in GRAPH_META —
# the slider derives bounds from that path.
#
# Month-bin epoch is 2018-01 (the canonical corpus's earliest topic falls
# in 2018-06; absolute epoch keeps the index stable across corpora).
# ---------------------------------------------------------------------------

# Months since 1970-01 for 2018-01 (UTC). Used as the slider's epoch so
# month indices stay small ints (0..~120 covers a decade).
_MONTH_BIN_EPOCH_TS = int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp())


def _parse_topic_ts(iso: str | None) -> int | None:
    """Parse an ISO-8601 string (e.g. `2022-11-04T09:27:09.387Z`) to a
    Unix-second integer. Returns None for empty / unparseable input.
    """
    if not iso:
        return None
    try:
        # `fromisoformat` handles `+00:00` natively; `Z` only on 3.11+.
        # Normalize for portability.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError, TypeError):
        return None


def _bounds_for_topics(
    topic_ids: list[str], topic_to_ts: dict[str, int]
) -> tuple[int, int] | None:
    """Min/max Unix-second timestamps over the given topic ids. Topics
    missing from the lookup are silently skipped. Returns None when no
    id resolves — the caller skips attaching bounds (no-signal entries
    pass any time-window filter, see the JS side)."""
    ts = [topic_to_ts[t] for t in topic_ids if t in topic_to_ts]
    if not ts:
        return None
    return (min(ts), max(ts))


def _ts_to_month_bin(ts: int) -> int:
    """Unix-second → months since the slider epoch (2018-01). Pre-epoch
    timestamps clamp to 0 so the slider stays addressable on corpora
    older than the epoch (rare; all forum data we know of is post-2018).
    """
    if ts <= _MONTH_BIN_EPOCH_TS:
        return 0
    dt = datetime.fromtimestamp(ts, timezone.utc)
    epoch_dt = datetime.fromtimestamp(_MONTH_BIN_EPOCH_TS, timezone.utc)
    return (dt.year - epoch_dt.year) * 12 + (dt.month - epoch_dt.month)


def _topics_for_source_id(source_id: str, chunk_to_topic: dict[str, str]) -> list[str]:
    """Parse a graphml `source_id` ("chunk-A<SEP>chunk-B<SEP>...") into a
    list of topic ids, deduped, preserving first-mention order. The order
    tends to align with extraction strength — earlier chunks are usually
    where the entity/relation was first introduced, so the JS panel can
    render them top-down without further sorting.
    """
    if not source_id or not chunk_to_topic:
        return []
    seen: dict[str, None] = {}
    for cid in source_id.split(SOURCE_ID_SEP):
        cid = cid.strip()
        if not cid:
            continue
        tid = chunk_to_topic.get(cid)
        if tid:
            seen.setdefault(tid, None)
    return list(seen)


def _build_topic_index(topics_dir: Path, topic_ids: set[str]) -> dict[str, dict]:
    """Build `{topic_id: {title, createdAt, postCount, excerpt}}` by reading
    each referenced topic JSON. The excerpt is the first ~240 chars of the
    first post's plain_text — enough for a one-row preview in the panel.
    Skips missing files silently; that topic just won't render.
    """
    out: dict[str, dict] = {}
    for tid in topic_ids:
        path = topics_dir / f"{tid}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        posts = data.get("posts") or []
        first_post = posts[0] if posts else {}
        title = data.get("title") or data.get("fancy_title") or f"Topic {tid}"
        created_at = data.get("created_at") or first_post.get("created_at") or ""
        excerpt = first_post.get("plain_text") or ""
        # Full thread for the topic-preview modal — every post with
        # author + date + truncated body. Skips empty posts. Each entry
        # is the smallest shape the JS modal needs; matching the topic
        # JSON's flat post structure (no post_stream nesting on this
        # corpus).
        posts_payload: list[dict] = []
        for p in posts:
            plain = (p.get("plain_text") or "").strip()
            if not plain:
                continue
            posts_payload.append({
                "postNumber": p.get("post_number") or len(posts_payload) + 1,
                "createdAt": p.get("created_at") or "",
                "username": p.get("username") or "",
                "displayName": p.get("display_name") or "",
                "plainText": _truncate(plain, TOPIC_POST_LEN),
            })
        out[tid] = {
            "title": title,
            "createdAt": created_at,
            "postCount": data.get("posts_count") or len(posts),
            # Short form for the in-panel preview (line-clamped to 2 lines).
            "excerpt": _truncate(excerpt, TOOLTIP_TRUNCATE),
            "firstPostBy": first_post.get("display_name") or first_post.get("username") or "",
            # Full thread used by the modal; iterate to render one
            # block per post.
            "posts": posts_payload,
        }
    return out


# ---------------------------------------------------------------------------
# Build seams — `build_visualization` orchestrates these in order.
# ---------------------------------------------------------------------------

@dataclass
class NodeMeta:
    """Per-node metadata captured in a single pass over `G.nodes(data=True)`."""

    categories: dict[str, str]      # node_id → super-category
    entity_types: dict[str, str]    # node_id → raw entity_type (or UNKNOWN_ENTITY_TYPE)
    type_counts: Counter            # super-category → node count
    entity_type_counts: dict[str, int]  # f"{Cat}|{EntityType}" → count
    degrees: dict[str, int]         # node_id → degree
    max_degree: int
    hub_ids: set[str]               # top-N by degree, used for label-LOD
    topic_ids: dict[str, list[str]] # node_id → deduped, first-mention-order topic ids
    referenced_topic_ids: set[str]  # union of all topic ids across nodes


def _load_graph(graphml_path: Path) -> nx.Graph:
    """Read the graphml file and log basic counts."""
    print(f"Loading graph from {graphml_path}...")
    G = nx.read_graphml(graphml_path)
    print(f"  Nodes: {G.number_of_nodes()}, Edges: {G.number_of_edges()}")
    return G


def _compute_node_metadata(
    G: nx.Graph,
    color_map: dict[str, str],
    hub_label_count: int,
    chunk_to_topic: dict[str, str],
) -> NodeMeta:
    """Single-pass derivation of every per-node fact the build needs.

    Folds together super-category, entity-type, type counts, entity-type
    counts, and topic-provenance resolution so we walk `G.nodes` once.
    `chunk_to_topic` may be `{}` (LightRAG kv_stores missing) — provenance
    just won't render in the panel; the rest of the build is unaffected.
    """
    lc_to_canonical = {name.lower(): name for name in color_map}
    degrees = dict(G.degree())
    max_degree = max(degrees.values()) if degrees else 1

    categories: dict[str, str] = {}
    entity_types: dict[str, str] = {}
    type_counts: Counter = Counter()
    entity_type_counts: dict[str, int] = {}
    topic_ids: dict[str, list[str]] = {}
    referenced_topic_ids: set[str] = set()
    for node_id, data in G.nodes(data=True):
        cat = _get_super_category(data.get("entity_type", ""), lc_to_canonical)
        etype = data.get("entity_type", "") or UNKNOWN_ENTITY_TYPE
        categories[node_id] = cat
        entity_types[node_id] = etype
        type_counts[cat] += 1
        et_key = f"{cat}{CAT_EDGE_SEP}{etype}"
        entity_type_counts[et_key] = entity_type_counts.get(et_key, 0) + 1
        node_topics = _topics_for_source_id(data.get("source_id", ""), chunk_to_topic)
        topic_ids[node_id] = node_topics
        referenced_topic_ids.update(node_topics)

    hub_ids: set[str] = set()
    if hub_label_count > 0:
        hub_ids = {
            nid for nid, _ in sorted(degrees.items(), key=lambda kv: -kv[1])[:hub_label_count]
        }

    return NodeMeta(
        categories=categories,
        entity_types=entity_types,
        type_counts=type_counts,
        entity_type_counts=entity_type_counts,
        degrees=degrees,
        max_degree=max_degree,
        hub_ids=hub_ids,
        topic_ids=topic_ids,
        referenced_topic_ids=referenced_topic_ids,
    )


def _compute_articulation_points(G: nx.Graph) -> set[str]:
    """Tarjan's O(n+m) cut-vertex set. Surfaced as 'Cut node' in the panel."""
    print("  Computing articulation points...")
    try:
        result: set[str] = set(nx.articulation_points(G))
    except Exception as exc:
        print(f"    skipped: {exc}")
        return set()
    print(f"    {len(result):,} articulation points")
    return result


def _compute_louvain_communities(G: nx.Graph) -> tuple[dict[str, int], list[int]]:
    """Modularity-based clustering; returns `(node_community, sizes_desc)`.

    Community 0 is the largest. Seeded for stable results across runs.
    """
    print("  Computing Louvain communities...")
    node_community: dict[str, int] = {}
    sizes: list[int] = []
    try:
        from networkx.algorithms.community import louvain_communities
        communities = louvain_communities(G, seed=42)
        communities_sorted = sorted(communities, key=lambda c: -len(c))
        for cidx, members in enumerate(communities_sorted):
            sizes.append(len(members))
            for nid in members:
                node_community[nid] = cidx
    except Exception as exc:
        print(f"    skipped: {exc}")
    print(f"    {len(sizes):,} communities (top sizes: {sizes[:5]})")
    return node_community, sizes


def _compute_max_weight(G: nx.Graph) -> float:
    """Max edge weight across the whole graph (or 1.0 for empty graphs)."""
    return max((float(d.get("weight", 1)) for *_, d in G.edges(data=True)), default=1.0)


def _compute_min_weight(G: nx.Graph) -> float:
    """Min edge weight across the whole graph (or 1.0 for empty graphs).
    Used as the lower bound of the Min Edge Weight slider so corpora
    with fractional weights below 1 stay reachable.
    """
    return min((float(d.get("weight", 1)) for *_, d in G.edges(data=True)), default=1.0)


def _node_tooltip(node_id: str, cat: str, entity_type: str,
                  degree: int, description: str) -> str:
    """Plain-text node tooltip — vis-network 9.1.2 renders string titles via
    textContent, so HTML tags would show up as literal text. `\\n` honoured
    as a line break (CSS `white-space: pre-wrap` in graph.css)."""
    lines: list[str] = [str(node_id)]
    meta = [cat]
    if entity_type and entity_type != cat:
        meta.append(entity_type)
    if degree:
        meta.append(f"deg {degree}")
    lines.append(" · ".join(meta))
    if description:
        lines.append("")
        lines.append(_truncate(description, TOOLTIP_TRUNCATE))
    return "\n".join(lines)


def _edge_tooltip(rel_cat: str, raw_description: str) -> str:
    """Plain-text edge tooltip — rel-bucket header + up to 3 description
    phrases, with `…` if more were truncated."""
    phrases = [p.strip() for p in raw_description.split(DESC_SEP) if p.strip()]
    lines: list[str] = []
    if rel_cat:
        lines.append(rel_cat)
    lines.extend(_truncate(p, TOOLTIP_TRUNCATE) for p in phrases[:3])
    if len(phrases) > 3:
        lines.append("…")
    return "\n".join(lines)


def _add_nodes_to_net(
    net: Network,
    G: nx.Graph,
    meta: NodeMeta,
    color_map: dict[str, str],
    positions: dict,
    articulation_set: set[str],
    node_community: dict[str, int],
) -> None:
    """Populate the PyVis network with every real-graph node."""
    print(f"  Adding {G.number_of_nodes()} nodes...")
    for node_id, data in G.nodes(data=True):
        cat = meta.categories[node_id]
        color = color_map.get(cat, _OTHER_COLOR)
        degree = meta.degrees.get(node_id, 0)
        size = (
            8 + (math.log1p(degree) / math.log1p(meta.max_degree)) * 35
            if meta.max_degree > 0 else 8
        )
        # Normalize LightRAG's <SEP> phrase joiner to DESC_SEP. fullDescription
        # ships with DESC_SEP-separated phrases (JS panel splits to render each
        # as its own paragraph); the tooltip variant uses \n for visual breaks.
        description = (data.get("description") or "").replace(LIGHTRAG_GRAPH_FIELD_SEP, DESC_SEP)
        tooltip_text = description.replace(DESC_SEP, "\n")
        entity_type = data.get("entity_type", "")
        pos = positions.get(node_id, (0, 0))
        net.add_node(
            node_id,
            label=_truncate_label(node_id),
            title=_node_tooltip(node_id, cat, entity_type, degree, tooltip_text),
            color=color,
            size=size,
            x=pos[0],
            y=pos[1],
            entityType=entity_type,
            superCategory=cat,
            degree=degree,
            isHub=(node_id in meta.hub_ids),
            isArticulation=(node_id in articulation_set),
            community=node_community.get(node_id, -1),
            fullDescription=description,
            topicIds=meta.topic_ids.get(node_id, []),
        )


def _process_edges(
    net: Network,
    G: nx.Graph,
    meta: NodeMeta,
    rel_cluster_map: ClusterMap,
    relationship_colors: dict[str, str],
    max_weight: float,
    chunk_to_topic: dict[str, str],
) -> tuple[Counter, dict[str, dict], dict[str, dict], set[str]]:
    """Single edge pass: classify, add to PyVis, aggregate the two view
    levels (cat-pair and (cat,entity-type)-pair), and resolve topic
    provenance. Returns `(rel_type_counts, cat_edges, entity_type_edges,
    edge_topic_ids)` — the last is the union of topic ids referenced by
    edges (typically a subset of node-level topics, but not always:
    relations can be extracted from chunks where neither endpoint
    independently appeared as an entity).
    """
    print(f"  Adding {G.number_of_edges()} edges...")
    rel_type_counts: Counter = Counter()
    cat_edges: dict[str, dict] = {}
    entity_type_edges: dict[str, dict] = {}
    edge_topic_ids: set[str] = set()
    for u, v, data in G.edges(data=True):
        weight = float(data.get("weight", 1))
        width = 0.5 + (weight / max_weight) * 4
        # Same <SEP> → DESC_SEP normalization as nodes; downstream split/join
        # patterns (e.g. _edge_tooltip) already speak DESC_SEP.
        raw_description = (data.get("description") or "").replace(LIGHTRAG_GRAPH_FIELD_SEP, DESC_SEP)
        rel_cat = _classify_edge(data.get("keywords", ""), rel_cluster_map)
        rel_type_counts[rel_cat] += 1
        topic_ids = _topics_for_source_id(data.get("source_id", ""), chunk_to_topic)
        edge_topic_ids.update(topic_ids)

        # `fullDescription` keeps the raw \x1f-separated form so the JS
        # edge-detail panel can split per phrase. The `title` string is
        # the truncated-and-joined form vis-network shows on hover.
        edge_color = relationship_colors.get(rel_cat, _OTHER_COLOR)
        net.add_edge(
            u, v,
            width=width,
            title=_edge_tooltip(rel_cat, raw_description),
            edgeWeight=weight,
            relCategory=rel_cat,
            fullDescription=raw_description,
            topicIds=topic_ids,
            color={"color": edge_color, "opacity": 0.4,
                   "highlight": edge_color, "hover": edge_color},
        )

        # View-level aggregates. Both share the sorted-pair-key shape so
        # the JS side reads them with one helper.
        u_cat = meta.categories.get(u, OTHER_BUCKET)
        v_cat = meta.categories.get(v, OTHER_BUCKET)
        cat_key = CAT_EDGE_SEP.join(sorted([u_cat, v_cat]))
        bucket = cat_edges.setdefault(cat_key, {"count": 0, "weight": 0.0})
        bucket["count"] += 1
        bucket["weight"] += weight

        u_et = meta.entity_types.get(u, UNKNOWN_ENTITY_TYPE)
        v_et = meta.entity_types.get(v, UNKNOWN_ENTITY_TYPE)
        u_key = f"{u_cat}{CAT_EDGE_SEP}{u_et}"
        v_key = f"{v_cat}{CAT_EDGE_SEP}{v_et}"
        et_key = TYPE_EDGE_SEP.join(sorted([u_key, v_key]))
        et_bucket = entity_type_edges.setdefault(et_key, {"count": 0, "weight": 0.0})
        et_bucket["count"] += 1
        et_bucket["weight"] += weight

    return rel_type_counts, cat_edges, entity_type_edges, edge_topic_ids


def _print_coverage_diagnostic(
    rel_type_counts: Counter, mode: str
) -> None:
    """Surface when the cached cluster map has gone stale for this corpus
    (a re-index added many new keyword strings)."""
    total_edges = sum(rel_type_counts.values())
    other_count = rel_type_counts.get(OTHER_BUCKET, 0)
    if not total_edges:
        return
    coverage_pct = 100 * (1 - other_count / total_edges)
    print(f"  Edge categorization coverage: {coverage_pct:.1f}% "
          f"({other_count}/{total_edges} fell to {OTHER_BUCKET}; mode={mode})")
    if coverage_pct < 75:
        print(f"  Low coverage — the cached cluster map likely doesn't "
              f"cover this corpus's keyword vocabulary. Re-run with "
              f"`--regenerate-keyword-clusters` to rebuild from the "
              f"current graph.")


# ---------------------------------------------------------------------------
# vis-network options + filter HTML helpers (sidebar legend rows).
# ---------------------------------------------------------------------------

# Keep dragNodes at vis-network's default (true). Setting it false suppresses
# click events for nodes in the bundled vis-network 9.1.2 — the JS side
# handles drag with a no-op snap-back.
_VIS_NETWORK_OPTIONS: dict = {
    "physics": {"enabled": False},
    "interaction": {
        "hover": True,
        "tooltipDelay": 200,
        "multiselect": True,
        "zoomView": True,
        "dragView": True,
        "keyboard": {"enabled": True},
    },
    "nodes": {
        "font": {"size": 11, "face": "system-ui, sans-serif",
                 "strokeWidth": 2, "strokeColor": "#1a1a2e"},
        "borderWidth": 1.5,
        "borderWidthSelected": 3,
    },
    "edges": {
        "color": {"opacity": 0.4, "inherit": False},
        "font": {"size": 0},
        "smooth": False,
        "selectionWidth": 2,
    },
}


def _filter_row(cls_prefix: str, cat: str, color: str, count: int) -> str:
    """Single sidebar filter row — checkbox + colored dot + label + count.

    The `data-cat` / `data-rel` attribute name follows from `cls_prefix`.
    Initial state: all filters checked → L == N → the `.equal` class hides
    the parens. When the checkbox is unchecked AND the leave-one-out count
    > 0, JS shows the .gain-pill so the user sees what they'd recover.
    """
    data_attr = "data-cat" if cls_prefix == "type" else "data-rel"
    count_html = (
        f'<span class="row-count {cls_prefix}-count equal" data-total="{count}">'
        f'<span class="row-count-visible">{count}</span>'
        f'<span class="row-count-total">({count})</span>'
        f'</span>'
        f'<span class="gain-pill" style="display:none" '
        f'title="Click to re-enable this filter and gain these back"></span>'
    )
    return (
        f'<label class="{cls_prefix}-filter" style="--{cls_prefix[:3]}-color:{color}">'
        f'<input type="checkbox" class="{cls_prefix}-cb" {data_attr}="{cat}" checked>'
        f'<span class="{cls_prefix}-dot" style="background:{color}"></span>'
        f'<span class="{cls_prefix}-label">{cat}</span>'
        f'{count_html}'
        f'</label>\n'
    )


def _sub_heading(label: str) -> str:
    return f'<div class="filter-group-heading">{label}</div>\n'


def _render_filter_section(
    cls_prefix: str,
    pinned: list[str],
    discovered: list[str],
    counts: dict[str, int],
    colors: dict[str, str],
) -> str:
    """Render a sidebar filter section (Forum primitives + Discovered + Other).

    Empty sub-sections collapse entirely. Discovered buckets are sorted by
    size descending. Other is pinned to the bottom.
    """
    pinned_present = [n for n in pinned if counts.get(n, 0) > 0]
    discovered_present = sorted(
        [n for n in discovered if counts.get(n, 0) > 0],
        key=lambda c: -counts[c],
    )
    other_count = counts.get(OTHER_BUCKET, 0)

    html = ""
    if pinned_present:
        html += _sub_heading("Forum primitives")
        for cat in pinned_present:
            html += _filter_row(cls_prefix, cat, colors.get(cat, _OTHER_COLOR), counts[cat])
    if discovered_present:
        html += _sub_heading("Discovered")
        for cat in discovered_present:
            html += _filter_row(cls_prefix, cat, colors.get(cat, _OTHER_COLOR), counts[cat])
    if other_count > 0:
        html += _filter_row(cls_prefix, OTHER_BUCKET, colors.get(OTHER_BUCKET, _OTHER_COLOR), other_count)
    return html


# ---------------------------------------------------------------------------
# Static HTML fragments (toolbar + detail panel) — module-level constants
# so they're not rebuilt on every visualize run.
# ---------------------------------------------------------------------------

_DETAIL_PANEL_HTML = """
<div id="detail-panel">
  <button class="close-btn" onclick="closeDetailPanel()" title="Close (reopen via the Details toolbar button)">&times;</button>
  <div id="detail-nav">
    <button id="nav-back" disabled title="Back">&#x25C0;</button>
    <button id="nav-forward" disabled title="Forward">&#x25B6;</button>
  </div>
  <div id="detail-content"></div>
</div>
"""

_TOOLBAR_HTML = """
<div id="loading-overlay">
  <div class="loading-spinner"></div>
  <div class="loading-text">Loading graph…</div>
</div>
<div id="breadcrumb"><span class="breadcrumb-seg current" data-seg="all">All</span></div>
<div id="toolbar">
  <div id="view-toggle-group" role="group" aria-label="View mode">
    <button data-mode="category" title="Overview: a handful of super-category nodes">Category</button>
    <button data-mode="node" class="active" title="Detail: individual nodes with filters">Node</button>
  </div>
  <button id="table-toggle" title="Open the searchable table of all entities (filterable, sortable by degree)">Table</button>
  <button id="reopen-detail-btn" disabled title="Reopen the last inspected detail panel (node, category, or edge)">Details</button>
  <button id="unselect-btn" title="Clear current selection and close the detail panel">Unselect</button>
  <button id="fit-btn" title="Re-center and zoom the canvas to fit all visible nodes">Fit All</button>
  <button id="reset-btn" title="Reset filters, search, and view — return to the Node-view default">Reset &amp; Refit</button>
  <select id="label-mode-select" title="Label visibility (hover any node to reveal its label regardless of mode)">
    <option value="hubs" selected>Labels: Hubs</option>
    <option value="all">Labels: All</option>
    <option value="none">Labels: None</option>
  </select>
  <button id="physics-toggle" title="Toggle force-directed simulation. Off (default) keeps the cached layout; on lets nodes settle into a fresh layout — useful after heavy filtering. Auto-disables when stabilization completes.">Enable Physics</button>
  <button id="reset-layout-btn" title="Restore the original cached layout positions (use after Enable Physics has drifted nodes apart)">Reset Layout</button>
</div>
<div id="pathfind-status">Click a destination node to highlight up to 3 shortest paths. <button onclick="cancelPathfind()" title="Stop pathfinding without highlighting anything">Cancel</button></div>
<div id="table-view">
  <div id="table-header">
    <h3>Entity Table</h3>
    <input id="table-search" type="text" placeholder="Search table..." />
    <span id="table-count"></span>
    <button id="table-close">Back to Graph</button>
  </div>
  <div id="table-wrap"><table id="data-table"><thead><tr>
    <th data-col="label">Name <span class="sort-arrow"></span></th>
    <th data-col="superCategory">Category <span class="sort-arrow"></span></th>
    <th data-col="entityType">Type <span class="sort-arrow"></span></th>
    <th data-col="degree">Degree <span class="sort-arrow"></span></th>
    <th data-col="desc">Description <span class="sort-arrow"></span></th>
  </tr></thead><tbody id="table-body"></tbody></table></div>
</div>
<div id="conn-table-view">
  <div id="table-header">
    <h3 id="conn-table-title">Connections</h3>
    <input id="conn-table-search" type="text" placeholder="Search connections..." />
    <span id="conn-table-count"></span>
    <button id="conn-table-close">Back to Graph</button>
  </div>
  <div id="table-wrap"><table id="conn-data-table"><thead><tr>
    <th data-col="label">Name <span class="sort-arrow"></span></th>
    <th data-col="superCategory">Category <span class="sort-arrow"></span></th>
    <th data-col="entityType">Type <span class="sort-arrow"></span></th>
    <th data-col="relCat">Relationship <span class="sort-arrow"></span></th>
    <th data-col="degree">Degree <span class="sort-arrow"></span></th>
    <th data-col="edgeDesc">Edge Description <span class="sort-arrow"></span></th>
  </tr></thead><tbody id="conn-table-body"></tbody></table></div>
</div>
"""


def _month_bin_label(bin_idx: int) -> str:
    """Month-bin index → "YYYY-MM" label. Inverse of `_ts_to_month_bin`,
    used to annotate the slider's initial extent in HTML."""
    epoch = datetime(2018, 1, 1, tzinfo=timezone.utc)
    year = epoch.year + (bin_idx // 12)
    month = epoch.month + (bin_idx % 12)
    if month > 12:
        year += 1
        month -= 12
    return f"{year:04d}-{month:02d}"


def _render_time_window_section(time_bounds: dict | None) -> str:
    """Optional 'Time window' sidebar section — only rendered when the
    corpus spans ≥2 months of source-post time. Empty string otherwise
    so the section header doesn't show up for single-day corpora.
    """
    if not time_bounds:
        return ""
    min_bin = int(time_bounds.get("minBin", 0))
    max_bin = int(time_bounds.get("maxBin", 0))
    if max_bin - min_bin < 1:
        return ""
    min_label = _month_bin_label(min_bin)
    max_label = _month_bin_label(max_bin)
    return f"""
    <h4 data-section="time-window" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Time window</h4>
    <div class="section-body" data-for="time-window">
      <div id="time-window-readout"
           title="Source-post time of the topics each edge was extracted from. Edges pass when ANY of their source topics overlaps the window. Hidden nodes follow the existing 'no visible edge' cascade.">
        <span id="time-window-label">{min_label} &rarr; {max_label}</span>
      </div>
      <div class="time-slider-row">
        <div class="time-slider-track">
          <div class="time-slider-fill" id="time-slider-fill"></div>
          <input type="range" id="time-min-slider" class="time-thumb time-thumb-min"
                 min="{min_bin}" max="{max_bin}" value="{min_bin}" step="1"
                 data-min-bin="{min_bin}" data-max-bin="{max_bin}"
                 title="Drag to move the start of the time window.">
          <input type="range" id="time-max-slider" class="time-thumb time-thumb-max"
                 min="{min_bin}" max="{max_bin}" value="{max_bin}" step="1"
                 data-min-bin="{min_bin}" data-max-bin="{max_bin}"
                 title="Drag to move the end of the time window.">
        </div>
        <span class="drop-pill" id="time-drop-pill" style="display:none"
              title="Click to reset the window to the full corpus span and recover the dropped edges"></span>
      </div>
    </div>
"""


def _render_control_panel(
    type_filters_html: str,
    rel_filters_html: str,
    max_weight: float,
    min_weight: float = 1.0,
    time_bounds: dict | None = None,
) -> str:
    """The left sidebar — Search / Time window (optional) / Glance / Type
    filters / Rel filters / Thresholds / Filter legend / Label density."""
    time_window_html = _render_time_window_section(time_bounds)
    # Slider step: weights are floats from LightRAG (0.5 / 0.8 / 1.5 / etc.
    # all show up). 0.1 captures the typical resolution; finer corpora can
    # always read the value off the readout.
    weight_step = 0.1
    # Default value: keep the historical "hide < 1" baseline when there
    # actually IS content above 1 to filter; otherwise sit at the corpus
    # min so no edges are silently dropped.
    weight_default = max(min_weight, 1.0) if max_weight >= 1.0 else min_weight
    weight_default = min(weight_default, max_weight)
    # Format helpers: keep the readout integer-clean when bounds are
    # whole numbers; otherwise show one decimal so 0.5 / 1.5 / etc.
    # render correctly.
    def _fmt(v: float) -> str:
        return str(int(v)) if abs(v - round(v)) < 1e-9 else f"{v:.1f}"
    weight_min_attr = _fmt(min_weight)
    weight_max_attr = _fmt(max_weight)
    weight_default_attr = _fmt(weight_default)
    return f"""
<div id="control-panel">
  <div id="cp-header">
    <button id="cp-toggle" title="Minimize">&#x25B4;</button>
  </div>
  <div class="cp-body">
    <h4 data-section="search" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Search</h4>
    <div class="section-body" data-for="search">
      <input id="search-input" type="text" placeholder="Search entities, descriptions, types..."
             title="Searches entity names, free-form descriptions, and entity types. Matches highlight on the canvas at full opacity; their 1-hop neighbours dim around them when the context toggle is on." />
      <div id="search-results"></div>
      <label class="search-context-toggle" title="When enabled, the 1-hop neighbours of every search match (that pass type/degree/community filters) appear dimmed for visual context. Uncheck for strict matches-only.">
        <input type="checkbox" id="show-search-context" checked>
        <span>Show 1-hop context (dim)</span>
      </label>
    </div>
    {time_window_html}
    <h4 data-section="glance" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> At a Glance</h4>
    <div class="section-body" data-for="glance">
      <div id="stats"></div>
      <div id="glance-panel">
        <div class="glance-empty">Filter or search to see what's visible.</div>
      </div>
    </div>

    <h4 data-section="entity-types" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Entity Types</h4>
    <div class="section-body" data-for="entity-types">
      <div class="type-actions">
        <button id="select-all-types" title="Check every entity-type filter">All</button>
        <button id="select-none-types" title="Uncheck every entity-type filter">None</button>
      </div>
      {type_filters_html}
    </div>

    <h4 data-section="rel-types" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Relationship Types</h4>
    <div class="section-body" data-for="rel-types">
      <div class="type-actions">
        <button id="select-all-rels" title="Check every relationship-type filter">All</button>
        <button id="select-none-rels" title="Uncheck every relationship-type filter">None</button>
      </div>
      {rel_filters_html}
    </div>

    <h4 data-section="sliders" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Thresholds</h4>
    <div class="section-body" data-for="sliders">
      <div style="font-size:11px;color:#aaa;margin-bottom:2px"
           title="Hide nodes with fewer than N connections. Higher values cut clutter; 0 shows all.">Min Degree</div>
      <div class="slider-row">
        <input type="range" id="degree-slider" min="0" max="20" value="2"
               title="Hide nodes with fewer than N connections.">
        <span class="slider-val" id="degree-val">2</span>
        <span class="slider-count" id="degree-count"></span>
        <span class="drop-pill" id="degree-drop-pill" style="display:none"
              title="Click to reset this threshold to 0 and recover the dropped nodes"></span>
      </div>
      <div style="font-size:11px;color:#aaa;margin:6px 0 2px 0"
           title="Hide edges below this LightRAG-extracted weight. Higher values keep only the most-confident relationships.">Min Edge Weight</div>
      <div class="slider-row">
        <input type="range" id="weight-slider" min="{weight_min_attr}" max="{weight_max_attr}" value="{weight_default_attr}" step="{weight_step}"
               title="Hide edges below this weight.">
        <span class="slider-val" id="weight-val">{weight_default_attr}</span>
        <span class="slider-count" id="weight-count"></span>
        <span class="drop-pill" id="weight-drop-pill" style="display:none"
              title="Click to reset this threshold to its minimum and recover the dropped edges"></span>
      </div>
    </div>

    <h4 data-section="filter-legend" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Filter legend</h4>
    <div class="section-body" data-for="filter-legend">
      <div id="filter-legend">
        Counts: <strong>L</strong> (N) &middot; L = visible now, N = graph total.
        <span class="pill-legend gain-pill-eg">+N</span> on a row = re-enable this filter to gain N back.
        <span class="pill-legend drop-pill-eg">&minus;N</span> on a slider = N items dropped by this threshold (click to reset).
      </div>
    </div>

    <h4 data-section="label-density" title="Click to collapse / expand this section"><span class="toggle-arrow">&#x25BE;</span> Label density</h4>
    <div class="section-body" data-for="label-density">
      <div id="label-density-count" class="label-density-count"
           title="Left: static labels currently rendered. Right: visible (non-hidden) nodes inside the canvas viewport — this is the input the 'auto-label viewport' threshold and 'all-mode cap' compare against. Hover labels are not counted. Updates on filter / zoom / mode change."></div>
      <div style="font-size:11px;color:#aaa;margin-bottom:2px"
           title="Top-N nodes by degree get static labels in 'Hubs' mode. Lower = less clutter; higher = more named hubs.">Hub labels (top-N)</div>
      <div class="slider-row">
        <input type="range" id="hub-label-slider" min="0" max="500" value="100" step="10"
               title="Top-N nodes by degree get static labels in 'Hubs' mode.">
        <span class="slider-val" id="hub-label-val">100</span>
      </div>
      <div style="font-size:11px;color:#aaa;margin:6px 0 2px 0"
           title="Only applies in 'Hubs' mode: when ≤ N visible nodes are inside the viewport, label all of them. Higher = more eager labelling on zoom-in. No effect in 'All' or 'None' modes.">Hubs mode: auto-label viewport &le; N</div>
      <div class="slider-row">
        <input type="range" id="viewport-threshold-slider" min="0" max="200" value="60" step="5"
               title="In Hubs mode only: label all visible nodes inside the viewport when there are this many or fewer.">
        <span class="slider-val" id="viewport-threshold-val">60</span>
      </div>
      <div style="font-size:11px;color:#aaa;margin:6px 0 2px 0"
           title="In 'All' mode, hard cap on rendered labels (top-N by degree when more would qualify). Prevents 16k labels at low zoom.">All-mode label cap</div>
      <div class="slider-row">
        <input type="range" id="all-cap-slider" min="50" max="1000" value="200" step="25"
               title="In 'All' mode, hard cap on rendered labels (top-N by degree).">
        <span class="slider-val" id="all-cap-val">200</span>
      </div>
      <button id="lod-reset" class="action-btn" style="margin-top:6px"
              title="Restore the build-time defaults for all three label-density sliders">Reset to defaults</button>
    </div>
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# Output writers — `data.js` + `graph.html`. Both have skip-if-identical
# gates so cache-hit runs leave on-disk mtimes stable.
# ---------------------------------------------------------------------------

def _write_data_payload(graph_data_path: Path, net: Network) -> None:
    """Write `<viz_dir>/data.js` with the node/edge payload, or skip if
    the file is byte-identical. PyVis normally inlines this ~22 MB into
    graph.html; outsourcing to a sibling .js keeps graph.html small and
    lets the browser cache the data across reloads. Loaded via
    `<script src>` rather than `fetch()` because Chrome's CORS policy
    blocks `file://` JSON fetches but allows script tags.
    """
    payload = {"nodes": list(net.nodes), "edges": list(net.edges)}
    new_contents = (
        "// Auto-generated by discourse_explorer.visualize. Loaded by graph.html\n"
        "// via <script src=\"data.js\">; populates the live vis.DataSet\n"
        "// objects from graph.js at DOMContentLoaded time.\n"
        "window.GRAPH_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    )
    existing = graph_data_path.read_text() if graph_data_path.exists() else None
    if existing == new_contents:
        print(f"  Graph data unchanged at {graph_data_path.name} "
              f"({graph_data_path.stat().st_size / 1024 / 1024:.1f} MB); skipping write.")
    else:
        graph_data_path.write_text(new_contents)
        print(f"  Graph data written to {graph_data_path.name} "
              f"({graph_data_path.stat().st_size / 1024 / 1024:.1f} MB).")


def _inject_and_write_html(
    output_path: Path,
    pyvis_html: str,
    *,
    control_panel_html: str,
    detail_panel_html: str,
    toolbar_html: str,
    meta_script: str,
    graph_data_script: str,
) -> None:
    """Inject our custom CSS + panels + data-script + custom JS into the
    PyVis-emitted HTML, then write to disk with a skip-if-identical gate.
    `<script src="data.js">` must come BEFORE the custom graph.js so
    `window.GRAPH_DATA` is defined by the time graph.js's DOMContentLoaded
    runs (script tags are sync by default).
    """
    injection = (
        CUSTOM_CSS
        + control_panel_html
        + detail_panel_html
        + toolbar_html
        + meta_script
        + graph_data_script
        + CUSTOM_JS
    )
    new_html = pyvis_html.replace("</body>", injection + "\n</body>")
    existing = output_path.read_text() if output_path.exists() else None
    if existing == new_html:
        print(f"  HTML unchanged at {output_path.name}; skipping write.")
    else:
        output_path.write_text(new_html)


# ---------------------------------------------------------------------------
# Main visualization builder — thin orchestrator over the seams above.
# ---------------------------------------------------------------------------

def build_visualization(
    rc,
    *,
    max_rel_types: int = 12,
    cache_k: int = 50,
    balance_threshold: float = 4.0,
    min_bucket_pct: float = 0.5,
    regenerate_keyword_clusters: bool = False,
    hub_label_count: int = 100,
) -> Path:
    """Build an interactive HTML visualization from the GraphRAG graph.

    See `--max-rel-types`, `--cache-k`, `--regenerate-keyword-clusters`
    in `main()` for the rel-cluster knobs.
    """
    paths: SitePaths = rc.paths()
    color_map = _build_entity_color_map(rc.data_dir)
    if not paths.graphml_file.exists():
        raise ConfigError(
            f"Graph not found at {paths.graphml_file}. "
            "Run indexing first: uv run discourse-explorer query <path> --index"
        )

    G = _load_graph(paths.graphml_file)

    # Topic provenance pre-pass: chunk → topic id lookup, joined from the
    # two LightRAG kv_stores. ~50 ms on 1.7k chunks; runs once and feeds
    # both _compute_node_metadata and _process_edges.
    chunk_to_topic = _load_chunk_to_topic(paths.graphrag_dir)

    # Build (or load) the dynamic keyword→bucket clustering. Two modes,
    # auto-selected by `OPENAI_API_KEY`: llm-cluster (embed + k-means +
    # LLM relabel) or token-cluster (top-N most-frequent tokens). Bucket
    # vocabulary is corpus-derived in both — see rel_clusters.py.
    keyword_counts = _rel_clusters.harvest_keywords(G)
    edge_keywords = _rel_clusters.harvest_edge_keywords(G)
    rel_cluster_map = _rel_clusters.load_or_build(
        rc, keyword_counts,
        edge_keywords=edge_keywords,
        max_rel_types=max_rel_types,
        cache_k=cache_k,
        balance_threshold=balance_threshold,
        min_bucket_pct=min_bucket_pct,
        force_rebuild=regenerate_keyword_clusters,
    )
    relationship_colors = _assign_colors(
        rel_cluster_map.pinned_names,
        rel_cluster_map.discovered_names,
    )

    node_meta = _compute_node_metadata(G, color_map, hub_label_count, chunk_to_topic)
    articulation_set = _compute_articulation_points(G)
    node_community, community_sizes = _compute_louvain_communities(G)
    max_weight = _compute_max_weight(G)
    min_weight = _compute_min_weight(G)

    viz_dir = paths.data_dir / "visualize"
    viz_cache_dir = viz_dir / "cache"
    viz_cache_dir.mkdir(parents=True, exist_ok=True)
    positions = _compute_layout(G, cache_path=viz_cache_dir / "layout.json")

    net = Network(
        height="100vh", width="100%",
        bgcolor="#1a1a2e", font_color="#eeeeee",
        directed=False, select_menu=False, filter_menu=False,
    )
    net.set_options(json.dumps(_VIS_NETWORK_OPTIONS))

    _add_nodes_to_net(
        net, G, node_meta, color_map, positions, articulation_set, node_community,
    )
    rel_type_counts, cat_edges, entity_type_edges, edge_topic_ids = _process_edges(
        net, G, node_meta, rel_cluster_map, relationship_colors, max_weight,
        chunk_to_topic,
    )
    _print_coverage_diagnostic(rel_type_counts, rel_cluster_map.mode)

    # Topic-provenance index: title + excerpt + post count + createdAt for
    # each topic referenced by any node or edge. Shipped in GRAPH_META so
    # the JS panel renders rows without needing to fetch topic JSONs over
    # `file://` (which Chrome blocks via CORS — same constraint that
    # pushed `data.js` over `data.json`).
    referenced_topics = node_meta.referenced_topic_ids | edge_topic_ids
    topic_index = _build_topic_index(paths.topics_dir, referenced_topics) if referenced_topics else {}
    if referenced_topics:
        print(f"  Topic provenance: {len(topic_index):,} / {len(referenced_topics):,} topics resolved.")

    # Time-window bounds: source-post time (topic JSON `created_at`) per
    # edge, packed as month-bin indices since 2018-01 to keep the
    # data.js delta small (~16 chars / edge × 24k edges ≈ 440 KB on the
    # canonical corpus). Edges without resolvable topics get no `tm`/`tM`
    # → JS treats them as time-pass (no signal, no penalty). Per-node
    # bounds are NOT shipped: the JS derives `nodeTimeOk` per slider
    # tick from the edges-in-window set, and the per-entity time-range
    # chip computes from the unioned topic createdAts in `topicIndex`.
    topic_to_ts: dict[str, int] = {}
    for tid, d in topic_index.items():
        ts = _parse_topic_ts(d.get("createdAt"))
        if ts is not None:
            topic_to_ts[tid] = ts
    for edge in net.edges:
        bounds = _bounds_for_topics(edge.get("topicIds") or [], topic_to_ts)
        if bounds is not None:
            edge["tm"] = _ts_to_month_bin(bounds[0])
            edge["tM"] = _ts_to_month_bin(bounds[1])
    if topic_to_ts:
        all_ts = list(topic_to_ts.values())
        global_min_bin = _ts_to_month_bin(min(all_ts))
        global_max_bin = _ts_to_month_bin(max(all_ts))
        time_bounds = {
            "minBin": global_min_bin,
            "maxBin": global_max_bin,
            "epoch": "2018-01",
        }
        print(f"  Time-window bounds: month bins {global_min_bin}..{global_max_bin} "
              f"({global_max_bin - global_min_bin + 1} months).")
    else:
        time_bounds = None

    _write_data_payload(viz_dir / "data.js", net)

    # Clear PyVis's internal lists so save_graph emits empty DataSet
    # literals — graph.js populates them from window.GRAPH_DATA on init.
    net.nodes = []
    net.edges = []

    output_path = viz_dir / "graph.html"
    net.save_graph(str(output_path))
    pyvis_html = output_path.read_text()

    # Render sidebar filter sections.
    pinned_entity_names = [n for n in STRUCTURAL_TYPE_NAMES if n in node_meta.type_counts]
    discovered_entity_names = sorted(
        set(node_meta.type_counts) - set(pinned_entity_names) - {OTHER_BUCKET}
    )
    type_filters_html = _render_filter_section(
        "type", pinned_entity_names, discovered_entity_names,
        node_meta.type_counts, color_map,
    )
    rel_filters_html = _render_filter_section(
        "rel", rel_cluster_map.pinned_names, rel_cluster_map.discovered_names,
        rel_type_counts, relationship_colors,
    )
    control_panel_html = _render_control_panel(
        type_filters_html, rel_filters_html, max_weight,
        min_weight=min_weight,
        time_bounds=time_bounds,
    )

    meta_script = (
        "<script>var GRAPH_META = "
        + json.dumps({
            "superCategoryColors": color_map,
            "relationshipColors": relationship_colors,
            "typeCounts": dict(node_meta.type_counts),
            "relTypeCounts": dict(rel_type_counts),
            "totalNodes": G.number_of_nodes(),
            "totalEdges": G.number_of_edges(),
            "categoryEdges": cat_edges,
            "entityTypeCounts": node_meta.entity_type_counts,
            "entityTypeEdges": entity_type_edges,
            "hubLabelCount": len(node_meta.hub_ids),
            "communitySizes": community_sizes,
            "articulationCount": len(articulation_set),
            "topicIndex": topic_index,
            "timeBounds": time_bounds,
            # Used by the detail-panel Stats / Query buttons to build
            # copy-paste-ready CLI commands. Relative to cwd when the
            # data dir lives under it, so committed fixture HTML doesn't
            # bake the maintainer's absolute path; absolute otherwise so
            # user-local corpora produce paste-from-anywhere commands.
            "dataDir": _format_data_dir_for_meta(rc.data_dir),
        })
        + ";</script>"
    )
    graph_data_script = '<script src="data.js"></script>\n'

    _inject_and_write_html(
        output_path, pyvis_html,
        control_panel_html=control_panel_html,
        detail_panel_html=_DETAIL_PANEL_HTML,
        toolbar_html=_TOOLBAR_HTML,
        meta_script=meta_script,
        graph_data_script=graph_data_script,
    )

    print(f"\nVisualization saved to {output_path}")
    print(f"  Entity types: {len(node_meta.type_counts)} super-categories")
    for cat in sorted(node_meta.type_counts, key=lambda c: (c == OTHER_BUCKET, -node_meta.type_counts[c])):
        print(f"    {cat}: {node_meta.type_counts[cat]} nodes")
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate an interactive HTML visualization of the GraphRAG knowledge graph."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        type=Path,
        help="Path to scraped data directory. Falls back to DISCOURSE_DATA_DIR in the project-root .env.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated HTML file in the default browser.",
    )
    parser.add_argument(
        "--max-rel-types", type=int, default=12, metavar="N",
        help="Number of relationship-type buckets to display in the legend. "
             "Cheap to change between runs — the cached analysis is reused; "
             "only the per-N derivation is recomputed (one LLM call "
             "~$0.0004 in llm-cluster mode, free in token-cluster mode). "
             "Default: 12.",
    )
    parser.add_argument(
        "--cache-k", type=int, default=50, metavar="N",
        help="Base k-means cluster count for llm-cluster mode (the cache "
             "stores N centroids; any --max-rel-types <= N reuses them via "
             "hierarchical merge). Bumping this triggers a re-embedding of "
             "all unique keywords (~$0.003-$0.005). Ignored in "
             "token-cluster mode. Default: 50.",
    )
    parser.add_argument(
        "--balance-threshold", type=float, default=4.0, metavar="F",
        help="Post-Ward balance target. If the largest discovered rel-type "
             "bucket exceeds F × the median bucket size, the smallest "
             "bucket's slot is reused by splitting the biggest (k-means=2). "
             "Strict N cap preserved. Pass `inf` to disable. Llm-cluster "
             "mode only. Default: 4.0.",
    )
    parser.add_argument(
        "--min-bucket-pct", type=float, default=0.5, metavar="F",
        help="Minimum bucket size as a percentage of total edges. Buckets "
             "below this are dropped and their slot reused by splitting "
             "the biggest bucket via k-means. Avoids absurdly tiny "
             "legend entries (e.g. a 2-edge bucket). Pass 0 to disable. "
             "Llm-cluster mode only. Default: 0.5.",
    )
    parser.add_argument(
        "--regenerate-keyword-clusters",
        action="store_true",
        help="Force rebuild of the cached keyword→bucket clustering at "
             "<data-dir>/visualize/cache/rel-clusters.json. In llm-cluster mode "
             "(OpenAI configured) this re-embeds + re-clusters (~$0.003); "
             "in token-cluster mode it re-tokenizes (free). Use when the "
             "coverage diagnostic warns, after a re-index that added many "
             "new keyword strings, or after switching embedding models.",
    )
    parser.add_argument(
        "--hub-label-count", type=int, default=100, metavar="N",
        help="Number of highest-degree nodes whose labels stay visible in "
             "the default 'Labels: Hubs' mode. Prevents the canvas from "
             "flooding with overlapping text when zooming into a large "
             "graph. Toggle to 'All' / 'Hover' in the toolbar. "
             "Pass 0 to start with no hub labels. Default: 100.",
    )
    args = parser.parse_args()

    try:
        rc = bootstrap(args.path)
        output_path = build_visualization(
            rc,
            max_rel_types=args.max_rel_types,
            cache_k=args.cache_k,
            balance_threshold=args.balance_threshold,
            min_bucket_pct=args.min_bucket_pct,
            regenerate_keyword_clusters=args.regenerate_keyword_clusters,
            hub_label_count=args.hub_label_count,
        )
    except ConfigError as e:
        parser.error(str(e))

    if args.open:
        subprocess.run(["open", str(output_path)])


if __name__ == "__main__":
    main()
