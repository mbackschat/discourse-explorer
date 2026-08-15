"""Derive a corpus-grounded QUERY-GUIDE.md from a scraped + indexed data dir.

Mechanical pipeline:

  1. Parse <data-dir>/graphrag/graph_chunk_entity_relation.graphml — node/edge
     counts, entity-type histogram, top-N content entities by degree.
  2. Scan <data-dir>/topics/*.json — topic counts per category and per version
     tag. Tag identity comes from `config.tag_label` (the slug), because a
     tag's display name varies by scrape date — the same release appears as
     `2025․06` with U+2024 in April-fetched topics and `2025-06` in
     August-fetched ones. Keying on the name would list one release twice.
  3. Harvest edge-keyword frequencies via `rel_clusters.harvest_keywords` +
     `rel_clusters._tokenize` (stem + stop-word filter already project-owned;
     don't reimplement).
  4. Compose deterministic §1–§5 + §7–§12 via string substitution.
  5. Compose §6 (question library) via one structured LLM call. The
     subsection skeletons are hardcoded; the LLM only picks *which* entities
     / categories / versions to render into each example query. Cheap
     (~$0.05 on gpt-4.1-mini), one call, fully logged.
  6. Write to <data-dir>/QUERY-GUIDE.md. Policy-driven handling of any prior
     file: back up (default), write alongside with a timestamp, or fail.

Target output size: under 20 KB (self-imposed cap; verified at write time).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import (
    ConfigError,
    RuntimeConfig,
    STRUCTURAL_TYPE_NAMES,
    bootstrap,
    load_entity_types,
    site_paths_from_dir,
    tag_label,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GraphStats:
    nodes_total: int
    edges_total: int
    type_histogram: list[tuple[str, int]]      # (type, count), desc
    top_content_by_degree: list[tuple[str, str, int]]  # (name, type, degree)
    structural_node_count: int                 # for out-of-vocab % math
    content_node_count: int
    out_of_vocab_count: int


@dataclass
class TopicStats:
    topics_total: int
    by_category: list[tuple[str, int]]         # (category, count), desc
    by_version: list[tuple[str, int]]          # (tag, count), desc


@dataclass
class VerbStats:
    pin_verbs: list[tuple[str, int]]           # structural / Pass 1 pins
    content_verbs: list[tuple[str, int]]       # LLM-extracted, stemmed + ranked
    unique_phrase_count: int


@dataclass
class GuideInputs:
    graph: GraphStats
    topics: TopicStats
    verbs: VerbStats
    extraction_model: str
    query_model: str
    vocab: dict
    snapshot_date: str


# ---------------------------------------------------------------------------
# Step 1 — Graphml
# ---------------------------------------------------------------------------

_GRAPHML_NS = {"g": "http://graphml.graphdrawing.org/xmlns"}


def _md_cell(s) -> str:
    """Escape a value for safe inclusion in a markdown table cell.

    Handles characters that break the table layout (`|`, newlines) plus
    those that turn LLM-derived names into unintended markdown (link
    brackets, leading hashes). Anything that just *styles* (asterisks,
    underscores) is left alone — names with `_my_var` look fine.
    """
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\r", "")
        .replace("\n", " ")
    )


def parse_graphml(graphml_path: Path, *, top_n: int = 15) -> GraphStats:
    """Single-pass parser over the graphml. No networkx dependency needed.

    `top_n` is the number of best-connected content entities to surface in
    §4.2. Structural types (lowercase in the graph per `_topic_to_custom_kg`)
    are excluded.
    """
    tree = ET.parse(graphml_path)
    root = tree.getroot()

    # Map graphml `<key id="dN">` back to its `attr.name`.
    key_map: dict[str, str] = {}
    for key in root.findall(".//g:key", _GRAPHML_NS):
        key_map[key.attrib["id"]] = key.attrib.get("attr.name", key.attrib["id"])

    # Nodes: collect id + entity_type.
    nodes: dict[str, str] = {}  # id -> entity_type
    for node in root.findall(".//g:graph/g:node", _GRAPHML_NS):
        nid = node.attrib["id"]
        etype = "UNKNOWN"
        for d in node.findall("g:data", _GRAPHML_NS):
            if key_map.get(d.attrib["key"]) == "entity_type" and d.text:
                etype = d.text
                break
        nodes[nid] = etype

    # Edges: count + degree.
    degree: Counter[str] = Counter()
    edges_total = 0
    for edge in root.findall(".//g:graph/g:edge", _GRAPHML_NS):
        degree[edge.attrib["source"]] += 1
        degree[edge.attrib["target"]] += 1
        edges_total += 1

    type_hist = Counter(nodes.values()).most_common()

    # Structural types in the graph are lowercase; the canonical vocab keeps
    # PascalCase. Compare case-insensitively.
    structural_lower = {t.lower() for t in STRUCTURAL_TYPE_NAMES}
    structural_count = sum(c for t, c in type_hist if t.lower() in structural_lower)

    # Content vocabulary types (from entity_types.json) vs. out-of-vocab
    # fallback buckets (other / UNKNOWN / concept / method / event / …).
    # For the "~X% out of vocab" line in §4.1 we need: of non-structural
    # nodes, how many landed outside the declared content vocabulary.
    # We can't pass the vocab here yet; the field is populated later in
    # `_finalize_out_of_vocab`. For now compute it relative to STRUCTURAL.
    content_count = sum(nodes.values() != "" for _ in [0])  # placeholder — set below

    content_entities: list[tuple[str, str, int]] = []
    for nid, etype in nodes.items():
        if etype.lower() in structural_lower:
            continue
        name = nid.strip('"')
        content_entities.append((name, etype, degree[nid]))
    content_entities.sort(key=lambda t: -t[2])

    return GraphStats(
        nodes_total=len(nodes),
        edges_total=edges_total,
        type_histogram=type_hist,
        top_content_by_degree=content_entities[:top_n],
        structural_node_count=structural_count,
        content_node_count=len(nodes) - structural_count,
        out_of_vocab_count=0,  # filled in by `_finalize_out_of_vocab`
    )


def _finalize_out_of_vocab(graph: GraphStats, vocab: dict) -> None:
    """Second-pass: annotate `out_of_vocab_count` once the vocab is loaded."""
    vocab_lower = {t["name"].lower() for t in vocab.get("types", [])}
    structural_lower = {t.lower() for t in STRUCTURAL_TYPE_NAMES}
    out = 0
    for t, c in graph.type_histogram:
        tl = t.lower()
        if tl in structural_lower:
            continue
        if tl not in vocab_lower:
            out += c
    graph.out_of_vocab_count = out


# ---------------------------------------------------------------------------
# Step 2 — Topics: per-category + per-version coverage
# ---------------------------------------------------------------------------

# Release tags reach us in three spellings across scrape eras: `2025-06`
# (the slug, and what `tag_label` now returns), `2025․06` with U+2024 ONE DOT
# LEADER (Discourse display names before ~2026-08), and `2025.06` with a real
# period (how prose writes it). Accept all three so this regex keeps matching
# whichever form a caller hands it; `tag_label` is what makes the *counts*
# collapse onto one row per release.
_VERSION_TAG_RE = re.compile(r"^20\d\d[-\.․]\d\d$")


def scan_topics(topics_dir: Path) -> TopicStats:
    cat_counter: Counter[str] = Counter()
    ver_counter: Counter[str] = Counter()
    total = 0
    for f in sorted(topics_dir.glob("*.json")):
        try:
            t = json.loads(f.read_text())
        except Exception:
            continue
        total += 1
        cat_counter[t.get("category_name") or "Unknown"] += 1
        for tag in t.get("tags") or []:
            # `tag_label`, not `tag["name"]`: the display name varies by scrape
            # date, so keying on it would list one release as two versions in
            # the guide while the graph holds a single node.
            label = tag_label(tag)
            if label and _VERSION_TAG_RE.match(label):
                ver_counter[label] += 1

    # Versions: sort by count desc, tie-break by lex (stable ordering).
    by_version = sorted(ver_counter.items(), key=lambda x: (-x[1], x[0]))
    return TopicStats(
        topics_total=total,
        by_category=cat_counter.most_common(),
        by_version=by_version,
    )


# ---------------------------------------------------------------------------
# Step 3 — Verb harvesting (stemmed + stop-word-filtered via rel_clusters)
# ---------------------------------------------------------------------------

# Pin keywords come from config.STRUCTURAL_REL_PINS. We classify a raw
# keyword as pin-origin iff it appears verbatim in any pin's csv.
def _pin_keywords() -> set[str]:
    from .config import STRUCTURAL_REL_PINS
    out: set[str] = set()
    for pin in STRUCTURAL_REL_PINS:
        for kw in pin.keywords_csv.split(","):
            out.add(kw.strip().lower())
    return out


def harvest_verbs(graphml_path: Path, *, top_content: int = 12) -> VerbStats:
    """Return pinned vs. content-extracted edge verbs, stemmed & ranked.

    Reuses `rel_clusters.harvest_keywords` + `_tokenize` (the latter is a
    stop-word-aware tokenizer; we stem lightly on top by stripping common
    English suffixes).
    """
    import networkx as nx
    from . import rel_clusters

    G = nx.read_graphml(graphml_path)
    counts = rel_clusters.harvest_keywords(G)
    unique_phrases = len(counts)

    pins = _pin_keywords()
    pin_counter: Counter[str] = Counter()
    stemmed: Counter[str] = Counter()

    for kw, cnt in counts.items():
        if kw in pins:
            # Represent pins by their canonical form (the exact kw).
            pin_counter[kw] += cnt
            continue
        # Tokenize (drops stopwords + punct) then light-stem each token
        # and the full phrase. We prefer a multi-word phrase when it's
        # meaningful ("component inclusion", "issue report") — keep the
        # phrase by joining tokens.
        toks = rel_clusters._tokenize(kw)
        if not toks:
            continue
        phrase = " ".join(_stem(t) for t in toks)
        if not phrase:
            continue
        stemmed[phrase] += cnt

    content_ranked = stemmed.most_common(top_content)
    pin_ranked = pin_counter.most_common()
    return VerbStats(
        pin_verbs=pin_ranked,
        content_verbs=content_ranked,
        unique_phrase_count=unique_phrases,
    )


_SUFFIX_STRIP = ("ing", "ed", "es", "s")


def _stem(tok: str) -> str:
    """Light English stemming. Mirrors the ad-hoc stemmer used in the
    hand-derived v2 guide so counts stay comparable."""
    for suf in _SUFFIX_STRIP:
        if len(tok) > len(suf) + 3 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


# ---------------------------------------------------------------------------
# Step 5 — Template composition (§1–5 + §7–§12)
# ---------------------------------------------------------------------------

def compose_header(inputs: GuideInputs) -> str:
    g, t = inputs.graph, inputs.topics
    return (
        "# Query guide — knowledge graph\n"
        "\n"
        "Tailored reference for asking useful questions against the forum GraphRAG. "
        "Numbers reflect the current index, not architecture in the abstract.\n"
        "\n"
        f"**Scale:** {t.topics_total:,} topics indexed · "
        f"{g.nodes_total:,} nodes · {g.edges_total:,} edges.\n"
        f"**Models** (from `config/.env`): extraction `{inputs.extraction_model}` · "
        f"query-time synthesis `{inputs.query_model}`.\n"
        f"**Snapshot:** numbers reflect the {inputs.snapshot_date} index state. "
        "Regenerate after any re-index — see §12.\n"
    )


_SECTION_1_TO_3 = """\
## 1. What retrieval will + won't do

- **Will** answer *"tell me about X"*, *"summarize what's discussed around Y"*, *"how does X relate to Z"* — if X/Y/Z are named entities or themes in the graph.
- **Won't** count, rank by frequency, enumerate exhaustively, do time-series. Retrieval is `top_k`-bounded; the LLM never sees the whole corpus.

For *how many / which is most / rank*, use `stats` first.

## 2. Pick a mode

| Question shape | Mode |
|---|---|
| "how many / which has most / list all …" | **`stats`** (graph can't count) |
| Centered on one named entity | **`local`** |
| Broad theme, cause/effect across topics | **`global`** |
| Structural reasoning about how components relate | **`hybrid`** (graph-only) |
| Mixed / unsure | **`mix`** (default) |
| Sanity check vs. graph value | **`naive`** (pure vector) |

## 3. Commands

```bash
uv run discourse-explorer query . "your question"                  # mix (default)
uv run discourse-explorer query . "question" --mode local          # entity-anchored
uv run discourse-explorer query . "question" --mode global         # theme / cause-effect
uv run discourse-explorer query . "question" --mode hybrid         # graph-only
uv run discourse-explorer query . "question" --mode naive          # pure vector
uv run discourse-explorer stats --path . <subcmd>                  # counts / SQL / search
```
"""


def compose_section4(inputs: GuideInputs) -> str:
    g, t, vocab = inputs.graph, inputs.topics, inputs.vocab

    # §4.1 — entity vocabulary table from entity_types.json counts in graphml
    structural_names = set(STRUCTURAL_TYPE_NAMES)
    content_types_in_vocab = [
        tt["name"] for tt in vocab.get("types", [])
        if tt["name"] not in structural_names
    ]
    # Map lowercase-graphml-type → count
    type_count_lower = {typ.lower(): c for typ, c in g.type_histogram}

    lines: list[str] = []
    lines.append("## 4. What's actually in this graph")
    lines.append("")
    lines.append("### 4.1 Entity vocabulary")
    lines.append("")
    lines.append(
        f"Structural (auto-emitted from topic JSON): **"
        f"{', '.join(STRUCTURAL_TYPE_NAMES)}**."
    )
    lines.append("")
    lines.append("Content (LLM-extracted, from `config/entity_types.json`):")
    lines.append("")
    lines.append("| Type | Nodes |")
    lines.append("|---|---:|")
    # Sort content types by their graph count (desc) so the most-populated
    # shows first — useful for the reader calibrating which types retrieve well.
    content_rows = sorted(
        ((name, type_count_lower.get(name.lower(), 0)) for name in content_types_in_vocab),
        key=lambda x: -x[1],
    )
    for name, c in content_rows:
        lines.append(f"| {_md_cell(name)} | {c:,} |")
    lines.append("")
    if g.out_of_vocab_count:
        pct = 100 * g.out_of_vocab_count / max(1, g.content_node_count)
        # Show the "other", "UNKNOWN" rows + small free-form buckets so
        # the reader sees the fallback texture, not just a percentage.
        structural_lower = {n.lower() for n in STRUCTURAL_TYPE_NAMES}
        vocab_lower = {name.lower() for name in content_types_in_vocab}
        oov_rows = []
        for typ, c in g.type_histogram:
            tl = typ.lower()
            if tl in structural_lower or tl in vocab_lower:
                continue
            oov_rows.append((typ, c))
        head = ", ".join(f"`{t}` ({c:,})" for t, c in oov_rows[:4])
        lines.append(
            f"**~{pct:.0f}% of non-structural entities fell outside this "
            f"{len(content_types_in_vocab)}-item vocab** — {head}"
            f"{', plus smaller buckets' if len(oov_rows) > 4 else ''}. "
            f"They retrieve normally; they just won't respond to "
            f"`entity_type=X` filters in viz / SQL."
        )
        lines.append("")

    # §4.2 — top content entities by degree
    lines.append(f"### 4.2 Best-connected content entities (top {len(g.top_content_by_degree)} by degree)")
    lines.append("")
    lines.append(
        "High degree = richer retrieval context in `local` mode. Entities "
        "below degree ~10 exist in the graph but retrieve thinly."
    )
    lines.append("")
    lines.append("| Entity | Type | Degree |")
    lines.append("|---|---|---:|")
    for name, etype, deg in g.top_content_by_degree:
        lines.append(f"| {_md_cell(name)} | {_md_cell(etype)} | {deg} |")
    lines.append("")

    # §4.3 — per category
    lines.append("### 4.3 Coverage by category")
    lines.append("")
    lines.append(
        "Query only where there's content. Weak categories (1–2 topics) "
        "will hedge badly — don't scope there."
    )
    lines.append("")
    lines.append("| Category | Topics |")
    lines.append("|---|---:|")
    for cat, n in t.by_category:
        label = cat if cat != "Unknown" else "(uncategorized)"
        lines.append(f"| {_md_cell(label)} | {n:,} |")
    lines.append("")

    # §4.4 — per version
    if t.by_version:
        lines.append("### 4.4 Coverage by version tag")
        lines.append("")
        lines.append("| Version | Topics |")
        lines.append("|---|---:|")
        for ver, n in t.by_version:
            lines.append(f"| {_md_cell(ver)} | {n:,} |")
        lines.append("")
        # Surface the column trap — load-bearing for stats SQL users.
        lines.append(
            "**Column trap for `stats sql`:** filter and group on `tag_label`, "
            "never `tag_name`. `tag_label` is derived from the tag slug and is "
            "stable; `tag_name` is Discourse's display name and varies by scrape "
            "date for the *same* tag — topics fetched before ~2026-08 spell the "
            "separator with the one-dot leader `․` (U+2024) instead of `-`, so "
            "grouping by `tag_name` silently splits one release across two rows "
            "and undercounts each. `tag_label` matches the graph's tag nodes too, "
            "because both derive from the slug via `config.tag_label`."
        )
        lines.append("")
        sample = t.by_version[0][0] if t.by_version else "2024-06"
        lines.append("```bash")
        lines.append(
            f"# WRONG — undercounts; splits on the display-name spelling\n"
            f"stats sql \"SELECT COUNT(*) FROM topic_tags WHERE tag_name = '{sample}'\"\n"
            f"# RIGHT\n"
            f"stats sql \"SELECT COUNT(*) FROM topic_tags WHERE tag_label = '{sample}'\""
        )
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def compose_section5(inputs: GuideInputs) -> str:
    v = inputs.verbs
    lines: list[str] = []
    lines.append("## 5. Relation vocabulary (what verbs the graph supports)")
    lines.append("")
    lines.append(
        f"Extracted edge keywords from the graphml ({v.unique_phrase_count:,} "
        "unique phrases), stemmed + frequency-ranked. Lets you judge which "
        "question verbs will retrieve well."
    )
    lines.append("")
    if v.pin_verbs:
        pin_list = " · ".join(f"`{k}`" for k, _ in v.pin_verbs[:6])
        lines.append(
            f"**Structural edges (Pass 1, always present):** {pin_list}. "
            "Useful for \"who posted in …\" but `stats` usually answers faster."
        )
        lines.append("")
    if v.content_verbs:
        lines.append("**Content-extracted edges (top by frequency):**")
        lines.append("")
        lines.append("| Verb (stemmed) | Count |")
        lines.append("|---|---:|")
        for verb, cnt in v.content_verbs:
            lines.append(f"| {verb} | {cnt:,} |")
        lines.append("")
    lines.append(
        "**Sparsely represented, avoid framing around these:** ownership "
        "(\"who owns …\"), succession (\"who introduced / deprecated …\"), "
        "performance metrics (\"how fast is …\"). Re-frame as composition "
        "or problem-state questions instead."
    )
    return "\n".join(lines)


_SECTION_7_TO_12_TEMPLATE = """\
## 7. Stats + query recipes (for top-N)

```bash
# Which categories have most topics → synthesize over the leaders
uv run discourse-explorer stats --path . sql \\
  "SELECT category, COUNT(*) n FROM topics WHERE category != 'Unknown' GROUP BY category ORDER BY n DESC LIMIT 5"

# Highest-reply topics → pick one → deep-dive
uv run discourse-explorer stats --path . sql \\
  "SELECT title, posts_count, category FROM topics ORDER BY posts_count DESC LIMIT 10"
uv run discourse-explorer query . \\
  "Summarize the thread titled '<title>' — problem, debate, resolution."

# Unanswered / open problems (built-in)
uv run discourse-explorer stats --path . unanswered
```

## 8. When answers feel weak — tuning knobs

Env vars in `<data-dir>/config/.env` (or inline: `TOP_K=100 uv run ...`).

| Knob | Default | Raise when | Lower when |
|---|---:|---|---|
| `TOP_K` | 60 | Answers cite too few topics | Context too large |
| `CHUNK_TOP_K` | 20 | Need more direct quotes | Token budget ceiling |
| `MAX_TOTAL_TOKENS` | 30000 | Truncation visible in answers | Cost reduction |
| `MAX_ENTITY_TOKENS` | 6000 | Entity-heavy truncation | Rebalance to chunks |
| `MAX_RELATION_TOKENS` | 8000 | Relation-heavy truncation | Rebalance to entities |

Model swap: `QUERY_MODEL=<id>`. Reasoning-tier (gpt-5-series) worth slowness for synthesis; `gpt-4.1-mini` fine for narrow lookup.

**Biggest remaining quality lever:** enable `RERANK_PROVIDER=jina` (or cohere / ali). See MANUAL §Rerank.

## 9. Blind spots on THIS graph

- **{oov_pct:.0f}% of content entities** landed outside the declared vocabulary (see §4.1). They retrieve normally; they just won't filter on `entity_type=X`.
- **Extraction quality is topic-dependent.** Check `graphrag/kv_store_doc_status.json` for topics whose Pass 2 failed — those keep only their structural `Topic` node. Questions scoped to those return sparse results.
- **Weak slices to avoid scoping to:** {weak_slice_note}
- **User handles may leak into content entities.** Top-degree `other`-typed entries that look like usernames are forum handles the LLM extracted as content — they compete with real concepts during retrieval. If a top-N result is a handle rather than a concept, discount it.

## 10. Bad-question → good-question rewrites

| Original (hedges) | Better |
|---|---|
| "Top-10 pain points" | "Recurring pain points in the <strong category> category" + `--mode global` |
| "List all bugs" | "Bugs reported in <strong version>" (version scope) |
| "Which topic has most replies" | `stats sql "SELECT title, posts_count FROM topics ORDER BY posts_count DESC LIMIT 10"` |
| "Summarize the forum" | "Summarize main themes in top-3 categories by activity" (after `stats`) |
| "What are users saying?" | Scope by named entity: "…about <top content entity>" |
| "How common is X?" | `stats --path . search "X"`, then synthesize on top |
| "Who owns / introduced X?" | Edges underrepresent this — use `stats` over posts / user_activity |

## 11. References

- `docs/MANUAL.md` — full CLI + env reference, rerank setup, mode semantics.
- `docs/lightrag/ProgramingWithCore.md` — authoritative `QueryParam` reference.
- `logs/INDEX_AND_EMBED-*.md` — per-run findings; include what got indexed and at what quality.
- `logs/CREATE-QUERY-GUIDE-*.md` — per-generation log for this guide.

## 12. Regenerating this guide after re-index

This guide drifts as the graph grows or is re-extracted. Regenerate:

```text
# Claude Code
/create-query-guide <data-dir>

# Codex
$create-query-guide <data-dir>
```

Or run the module directly:

```bash
uv run python -m discourse_explorer.derive_query_guide <data-dir>
```

Sources it reads:

- **Node / edge counts, entity-type distribution, degree ranking** — `graphrag/graph_chunk_entity_relation.graphml`.
- **Edge-verb frequency (§5)** — every edge's `keywords` attribute, stemmed via `rel_clusters._tokenize` + `_stem`.
- **Per-category counts (§4.3)** — `topics/*.json` → `category_name`.
- **Per-version counts (§4.4)** — `topics/*.json` → `config.tag_label(tag)` matching `r'{version_regex}'` (the slug, not the display name, so one release counts once; the class accepts `-`, ASCII `.` and U+2024 `․`).
- **Blind-spot hints (§9)** — skim `graphrag/kv_store_doc_status.json` for non-`processed` entries.

§6 is LLM-authored against these facts; §1–§5 and §7–§12 are template-substituted and change only when the numbers change.
"""


def compose_sections_7_to_12(inputs: GuideInputs) -> str:
    g, t = inputs.graph, inputs.topics
    oov_pct = 100 * g.out_of_vocab_count / max(1, g.content_node_count)

    # Weak slices: categories with <=2 topics + version tags with <20 topics.
    weak_cats = [c for c, n in t.by_category if n <= 2 and c.lower() != "unknown"]
    weak_vers = [v for v, n in t.by_version if n < 20]
    parts = []
    if weak_cats:
        parts.append(f"categories — {', '.join(weak_cats[:6])}")
    if weak_vers:
        parts.append(f"versions — {', '.join(weak_vers)}")
    weak_note = "; ".join(parts) if parts else "none — coverage is balanced."

    return _SECTION_7_TO_12_TEMPLATE.format(
        oov_pct=oov_pct,
        weak_slice_note=weak_note,
        # Interpolated, never re-typed: §12 documents how §4.4 is extracted, and
        # a hand-copied rendering of this regex silently drifted from the real
        # one twice (it kept `tags[].name` after the switch to `tag_label`, and
        # missed the `-` added for slugs). Reading it then reproduced the very
        # double-count §4.4 warns about. One source of truth, by construction.
        version_regex=_VERSION_TAG_RE.pattern,
    )


# ---------------------------------------------------------------------------
# Step 6 — §6 question library via a single LLM call
# ---------------------------------------------------------------------------

# Subsection skeletons. The LLM fills in 2–3 example queries per subsection
# using only entities / categories / versions the guide actually lists.
_SECTION6_SUBSECTIONS: list[tuple[str, str, str, str]] = [
    # (subsection id, title, mode-tag, one-line rationale for the LLM)
    ("6.1", "Troubleshooting / error diagnosis", "local",
     "Quote error codes verbatim — embeddings anchor on them."),
    ("6.2", "Performance investigation", "mix",
     "Spans the slow component and what chains onto it."),
    ("6.3", "Migration / upgrade planning", "global",
     "Scope to the strongest version tags from §4.4."),
    ("6.4", "Architecture understanding", "hybrid",
     "Graph-only, no chunks. Shape-of-graph over prose."),
    ("6.5", "Component / API reference", "local",
     "Forum-as-documentation. Anchor on the named component."),
    ("6.6", "Category-scoped synthesis", "global",
     "Pick from §4.3 strongest categories (>100 topics)."),
    ("6.7", "How-to / pattern recognition", "mix",
     "LLM strength: generalize across retrieved chunks."),
    ("6.8", "Comparative / trade-off", "mix",
     "Needs both entities to have good degree (§4.2)."),
    ("6.9", "Security / auth / compliance", "local",
     "Scope to auth-related entities if present."),
    ("6.10", "Community gaps & unresolved questions", "global",
     "Follow up with `stats --path . unanswered`."),
    ("6.11", "Onboarding / learning path", "mix",
     "Synthesize across experienced posters' recommendations."),
]


def _build_section6_prompt(inputs: GuideInputs) -> str:
    """Compose the single structured prompt for §6."""
    g, t, v = inputs.graph, inputs.topics, inputs.verbs

    top_entities = "\n".join(
        f"  - {name} ({etype}, degree {deg})"
        for name, etype, deg in g.top_content_by_degree
    )
    strong_cats = "\n".join(
        f"  - {cat} ({n} topics)"
        for cat, n in t.by_category
        if n >= 30 and cat.lower() != "unknown"
    ) or "  (none with ≥30 topics)"
    versions = "\n".join(
        f"  - {ver} ({n} topics)"
        for ver, n in t.by_version
    ) or "  (no version tags)"
    content_verbs = ", ".join(f"{k} ({n})" for k, n in v.content_verbs[:10]) or "(none)"

    skeleton = "\n".join(
        f"{sid}. {title} (`{mode}`) — {note}"
        for sid, title, mode, note in _SECTION6_SUBSECTIONS
    )

    return f"""You are drafting §6 of a GraphRAG query guide for a scraped forum.

The guide is corpus-specific. Below are the real entities, categories,
version tags, and relation verbs present in THIS graph. Every example
query you produce MUST reference only names that appear in these lists —
NEVER invent an entity, category, or version.

## Corpus facts

**Top-connected content entities (name · type · degree):**
{top_entities}

**Strongest categories by topic count:**
{strong_cats}

**Version tags:**
{versions}

**Top content-extracted edge verbs (stemmed, count):** {content_verbs}

## Task

For each of the {len(_SECTION6_SUBSECTIONS)} subsections below, render 2–3 example
shell commands in the form:

```bash
uv run discourse-explorer query . \\
  "<question>" {{mode_flag}}
```

where `{{mode_flag}}` is `--mode local|global|hybrid` when the subsection
specifies a mode (never include `--mode mix` — it's the default).

### Subsections to render

{skeleton}

## Output format

Emit the full §6 section as Markdown, starting with:

## 6. Question library (tailored to THIS corpus)

Include a 3-sentence intro that (a) says examples use §4.2 entities and
§4.3–4.4 scope so retrieval lands, (b) teaches "chain broad → narrow"
(start with a `global` category question, harvest named concepts, pivot
to `local`), (c) teaches "scope tightens retrieval" (add at least one
entity/category/version to every question).

Then each subsection as a `### {{sid}} {{title}} ({{mode}})` heading, one
one-line rationale, then the code block.

Do NOT add §6.12 or renumber. Do NOT include anything after the last
subsection. Emit valid Markdown only — no commentary, no explanation.
"""


def _make_llm_func(
    rc: RuntimeConfig,
    model: str | None = None,
) -> tuple[Callable, str]:
    """Mirror `discover_types._make_llm_func` — OpenAI or Ollama."""
    if rc.is_openai:
        from lightrag.llm.openai import openai_complete_if_cache
        chosen = model or "gpt-4.1-mini"
        async def _llm(prompt: str) -> str:
            return await openai_complete_if_cache(chosen, prompt)
        return _llm, chosen
    else:
        from lightrag.llm.ollama import ollama_model_complete
        chosen = model or rc.extraction_model
        ollama_host = rc.ollama_host
        async def _llm(prompt: str) -> str:
            return await ollama_model_complete(
                prompt,
                hashing_kv=type("FakeKV", (), {
                    "global_config": {"llm_model_name": chosen},
                })(),
                host=ollama_host,
                options={"num_ctx": 32768},
            )
        return _llm, chosen


async def compose_section6_llm(
    inputs: GuideInputs,
    rc: RuntimeConfig,
    *,
    model: str | None = None,
) -> tuple[str, str, str]:
    """Return (section6_markdown, prompt_used, model_used)."""
    prompt = _build_section6_prompt(inputs)
    llm, chosen = _make_llm_func(rc, model=model)
    result = await llm(prompt)
    # Minimal cleanup: if the LLM wrapped output in fences, strip them.
    text = result.strip()
    if text.startswith("```"):
        # strip opening + closing fence blocks
        text = re.sub(r"^```\w*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    # Ensure it starts with a heading
    if not text.startswith("## 6."):
        text = "## 6. Question library (tailored to THIS corpus)\n\n" + text
    return text, prompt, chosen


def compose_section6_stub(inputs: GuideInputs) -> str:
    """Fallback used with `--no-section6`. Emits the skeleton only."""
    lines: list[str] = []
    lines.append("## 6. Question library (tailored to THIS corpus)")
    lines.append("")
    lines.append(
        "_Skeleton only — re-run `/create-query-guide` without `--no-section6` "
        "to let the LLM render concrete example queries against this corpus._"
    )
    lines.append("")
    for sid, title, mode, note in _SECTION6_SUBSECTIONS:
        lines.append(f"### {sid} {title} (`{mode}`)")
        lines.append("")
        lines.append(note)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Final composition
# ---------------------------------------------------------------------------

def compose(inputs: GuideInputs, section6: str) -> str:
    parts = [
        compose_header(inputs),
        "",
        _SECTION_1_TO_3,
        compose_section4(inputs),
        compose_section5(inputs),
        "",
        section6,
        "",
        compose_sections_7_to_12(inputs),
    ]
    out = "\n".join(parts).rstrip() + "\n"
    # Normalize runs of blank lines to at most 2.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


# ---------------------------------------------------------------------------
# File I/O with overwrite policy
# ---------------------------------------------------------------------------

def _apply_overwrite_policy(
    guide_path: Path,
    policy: str,
    timestamp: str,
) -> tuple[Path, Path | None]:
    """Return (target_write_path, backup_path_if_any)."""
    if not guide_path.exists():
        return guide_path, None

    if policy == "backup":
        backup = guide_path.with_name(f"QUERY-GUIDE.backup-{timestamp}.md")
        guide_path.rename(backup)
        return guide_path, backup

    if policy == "alongside":
        alongside = guide_path.with_name(f"QUERY-GUIDE-{timestamp}.md")
        return alongside, None

    if policy == "fail":
        raise ConfigError(
            f"QUERY-GUIDE.md already exists at {guide_path}. "
            "Pass --overwrite-policy=backup or --overwrite-policy=alongside."
        )

    raise ValueError(f"unknown overwrite-policy: {policy!r}")


# ---------------------------------------------------------------------------
# Findings log
# ---------------------------------------------------------------------------

def _write_findings_log(
    log_path: Path,
    inputs: GuideInputs,
    *,
    section6_model: str,
    section6_prompt: str | None,
    section6_skipped: bool,
    output_path: Path,
    output_bytes: int,
    backup_path: Path | None,
    elapsed_s: float,
) -> None:
    g = inputs.graph
    lines = [
        f"# CREATE-QUERY-GUIDE run — {inputs.snapshot_date}",
        "",
        f"**Data dir:** `{output_path.parent}`",
        f"**Output:** `{output_path.name}` ({output_bytes:,} bytes)",
        f"**Backup:** `{backup_path.name}`" if backup_path else "**Backup:** none (no prior guide)",
        f"**Elapsed:** {elapsed_s:.1f}s",
        "",
        "## Graphml parse",
        "",
        f"- Nodes: {g.nodes_total:,}",
        f"- Edges: {g.edges_total:,}",
        f"- Structural nodes: {g.structural_node_count:,}",
        f"- Content nodes: {g.content_node_count:,}",
        f"- Out-of-vocab content: {g.out_of_vocab_count:,}",
        "",
        "### Entity-type histogram",
        "",
        "| Type | Count |",
        "|---|---:|",
    ]
    for typ, c in g.type_histogram[:30]:
        lines.append(f"| {_md_cell(typ)} | {c:,} |")
    lines += [
        "",
        "### Top content entities by degree",
        "",
        "| Entity | Type | Degree |",
        "|---|---|---:|",
    ]
    for name, etype, deg in g.top_content_by_degree:
        lines.append(f"| {_md_cell(name)} | {_md_cell(etype)} | {deg} |")

    lines += [
        "",
        "## Topic scan",
        "",
        f"- Topics: {inputs.topics.topics_total:,}",
        f"- Categories: {len(inputs.topics.by_category)}",
        f"- Version tags: {len(inputs.topics.by_version)}",
        "",
        "## Verb harvest",
        "",
        f"- Unique keyword phrases: {inputs.verbs.unique_phrase_count:,}",
        f"- Pin verbs: {len(inputs.verbs.pin_verbs)}",
        f"- Content verbs surfaced in §5: {len(inputs.verbs.content_verbs)}",
        "",
        "## §6 (question library)",
        "",
        f"- LLM model: `{section6_model}`" if not section6_skipped else "- Skipped (`--no-section6`)",
    ]
    if section6_prompt:
        lines += [
            "",
            "### §6 prompt (for reproducibility)",
            "",
            "```",
            section6_prompt,
            "```",
        ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _run(
    data_dir: Path,
    *,
    section6_model: str | None,
    overwrite_policy: str,
    dry_run: bool,
    skip_section6: bool,
) -> int:
    start = time.time()
    rc = bootstrap(data_dir)
    paths = site_paths_from_dir(rc.data_dir)

    # --- Prerequisite checks ---
    if not paths.topics_dir.exists() or not any(paths.topics_dir.glob("*.json")):
        raise ConfigError(
            f"No topics at {paths.topics_dir}. Run the scraper first."
        )

    if not paths.graphml_file.exists():
        raise ConfigError(
            f"No graphml at {paths.graphml_file}. Run `/index-and-embed` first — the "
            "guide needs a built knowledge graph to summarize."
        )
    graphml = paths.graphml_file

    vocab = load_entity_types(rc.data_dir)  # raises ConfigError if missing/invalid

    # --- Extract ---
    print(f"[1/4] Parsing graphml ({graphml.stat().st_size // 1024} KB) …",
          flush=True)
    graph = parse_graphml(graphml)
    _finalize_out_of_vocab(graph, vocab)

    print(f"[2/4] Scanning topics in {paths.topics_dir.name}/ …", flush=True)
    topics = scan_topics(paths.topics_dir)

    print("[3/4] Harvesting edge verbs …", flush=True)
    verbs = harvest_verbs(graphml)

    snapshot_date = time.strftime("%Y-%m-%d")
    extraction_model = rc.default_extraction_model()
    query_model = rc.query_model or extraction_model

    inputs = GuideInputs(
        graph=graph,
        topics=topics,
        verbs=verbs,
        extraction_model=extraction_model,
        query_model=query_model,
        vocab=vocab,
        snapshot_date=snapshot_date,
    )

    # --- §6 ---
    section6_prompt: str | None = None
    if skip_section6:
        print("[4/4] Composing (§6 skipped) …", flush=True)
        section6 = compose_section6_stub(inputs)
        section6_model_used = "(none — skipped)"
    else:
        print(f"[4/4] Composing §6 via LLM "
              f"({'OpenAI' if rc.is_openai else 'Ollama'}) …", flush=True)
        section6, section6_prompt, section6_model_used = \
            await compose_section6_llm(inputs, rc, model=section6_model)

    guide = compose(inputs, section6)
    guide_bytes = guide.encode("utf-8")

    if dry_run:
        sys.stdout.write(guide)
        print(f"\n--- dry-run: would write {len(guide_bytes):,} bytes "
              f"to {paths.data_dir / 'QUERY-GUIDE.md'}", file=sys.stderr)
        return 0

    if len(guide_bytes) > 20_000:
        print(f"warning: generated guide is {len(guide_bytes):,} bytes "
              f"(self-imposed cap is 20,000)", file=sys.stderr)

    # --- Write ---
    ts = time.strftime("%Y%m%d-%H%M%S")
    guide_path = paths.data_dir / "QUERY-GUIDE.md"
    target, backup = _apply_overwrite_policy(guide_path, overwrite_policy, ts)
    target.write_text(guide)

    # --- Log ---
    log_path = paths.data_dir / "logs" / f"CREATE-QUERY-GUIDE-{ts}.md"
    _write_findings_log(
        log_path, inputs,
        section6_model=section6_model_used,
        section6_prompt=section6_prompt,
        section6_skipped=skip_section6,
        output_path=target,
        output_bytes=len(guide_bytes),
        backup_path=backup,
        elapsed_s=time.time() - start,
    )

    print(f"Wrote {target} ({len(guide_bytes):,} bytes).")
    if backup:
        print(f"Backed up previous guide to {backup.name}.")
    print(f"Findings log: {log_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="discourse_explorer.derive_query_guide",
        description="Derive a corpus-grounded QUERY-GUIDE.md from a data dir.",
    )
    parser.add_argument("data_dir", nargs="?", type=Path,
                        help="Data dir. Omit to use DISCOURSE_DATA_DIR.")
    parser.add_argument("--section6-model", default=None,
                        help="LLM model for §6 (default: gpt-4.1-mini on OpenAI, "
                             "EXTRACTION_MODEL on Ollama).")
    parser.add_argument("--overwrite-policy",
                        choices=("backup", "alongside", "fail"),
                        default="backup",
                        help="What to do if QUERY-GUIDE.md already exists.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the composed guide to stdout; don't write.")
    parser.add_argument("--no-section6", action="store_true",
                        help="Skip the LLM call for §6; emit a skeleton placeholder.")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(
            args.data_dir,
            section6_model=args.section6_model,
            overwrite_policy=args.overwrite_policy,
            dry_run=args.dry_run,
            skip_section6=args.no_section6,
        ))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
