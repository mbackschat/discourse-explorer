#!/usr/bin/env python3
"""
LightRAG-powered query interface for scraped Discourse data.

Uses LightRAG with either OpenAI (cloud, fast) or Ollama (local, free).
Auto-detects: if OPENAI_API_KEY is set, uses OpenAI; otherwise Ollama.

Usage:
    uv run discourse-explorer query <path> --index              # build knowledge graph
    uv run discourse-explorer query <path> --index --clear      # wipe and rebuild
    uv run discourse-explorer query <path> "your question"      # query (mix mode)
    uv run discourse-explorer query <path> "question" --mode local   # entity-focused
    uv run discourse-explorer query <path> "question" --query-model gpt-4o
"""

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

# Show LightRAG progress (extraction stages, document counts, etc.)
logging.basicConfig(
    format="  %(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
# Suppress noisy HTTP-level logs but keep LightRAG's progress
for _name in ("httpx", "httpcore", "ollama", "openai"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from discourse_explorer.config import (
    STRUCTURAL_REL_KEYWORDS,
    STRUCTURAL_TYPE_NAMES,
    ConfigError,
    RuntimeConfig,
    bootstrap,
    content_type_names,
    load_entity_types,
    tag_display,
    tag_label,
)

# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------

import re

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


def _sanitize(text: str) -> str:
    """Strip control characters and surrogates that break JSON serialization."""
    return _CONTROL_CHAR_RE.sub("", text)


# Empirically (1331-topic corpus, 2026-04-23), the only contiguous runs of
# 200+ chars from the [A-Za-z0-9+/=] alphabet are pasted binary payloads —
# base64-encoded PDFs and hex-encoded Java serialization dumps. They waste
# LLM calls and produce tuple-format errors during extraction with zero
# semantic benefit. We elide them at document-build time so scraped JSON
# stays faithful to Discourse while indexing skips the noise. PEM-style
# newline-separated base64 is safe (each line is <=64 chars, below threshold).
_ENCODED_BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")


def _elide_encoded_blobs(text: str) -> str:
    return _ENCODED_BLOB_RE.sub(
        lambda m: f"<{len(m.group())}-char encoded blob elided>", text
    )


# Structural edge keywords now live in config.py (STRUCTURAL_REL_KEYWORDS) so
# both Pass 1 (this module) and the visualize relation-pin layer can read them
# from a single source.


# ---------------------------------------------------------------------------
# Progress reporting + disk-persistence policy
# ---------------------------------------------------------------------------

# Filenames inside <data-dir>/graphrag/ that the persistence policy reasons about.
LLM_CACHE_FILE = "kv_store_llm_response_cache.json"
CACHE_PROVENANCE_FILE = "cache_provenance.json"

# Flush to disk every N documents during Pass 2 instead of after each one.
# Each flush unconditionally rewrites all three Faiss indices (~498MB on a
# 1400-topic corpus) because `FaissVectorDBStorage.index_done_callback` has no
# dirty guard, so the interval is a direct multiplier on bytes written. 200
# keeps a full rebuild in the single-digit GB range; the coarser crash
# resumability it implies costs almost nothing now that the LLM response cache
# survives a re-run (see `_clear_graph_dir`) — a resumed pass re-reads cached
# completions instead of re-billing the API.
PERSIST_EVERY = 200

# Pass 1 checkpoint interval, in topics. Pass 1 calls `_insert_done` once per
# topic, so this is a straight topic count.
#
# Do NOT set this to "never". An earlier version suppressed Pass 1 flushes for
# the whole pass and flushed once at the phase boundary. It measured as a pure
# win — 7.5x faster (8.4 -> 63 topics/min) and ~15GB of Faiss rewrites avoided —
# because only the speedup was measured, never what it cost. What it cost was
# the checkpoint: a SIGKILL cannot run the boundary flush's `finally`, so a kill
# at topic 1200 of 1399 destroyed the whole pass and ~1200 topics of paid
# embeddings (observed 2026-08-14). The previous every-50 behaviour capped that
# loss at 50 topics.
#
# The lesson is to bound the blast radius rather than rely on diagnosing the
# killer: checkpoints must survive signals that no `finally` can catch.
#
# 250 keeps ~5 checkpoints across a 1399-topic pass — loss capped at 250 topics,
# and still only ~5 flushes against the old ~28.
PASS1_CHECKPOINT_EVERY = 250


def _progress_line(
    label: str,
    done: int,
    total: int,
    elapsed: float,
    ok: int | None = None,
    failed: int | None = None,
    skipped: int | None = None,
) -> str:
    """Format a long-pass progress line with throughput and ETA.

    Long passes are the only feedback the operator gets during a multi-hour
    run, and a bare `done/total` forces them to derive throughput by hand from
    file mtimes. Always carry elapsed + ETA.

    A caller that passes `skipped` gets the field on *every* line, including at
    zero. Emitting it only when non-zero is worse than useless: the operator
    cannot tell "nothing was skipped" from "this build doesn't report skips",
    which are very different diagnoses on a resume.
    """
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    counts = ""
    if ok is not None or failed is not None or skipped is not None:
        parts = [f"{ok or 0} ok", f"{failed or 0} failed"]
        if skipped is not None:
            parts.append(f"{skipped} skipped")
        counts = f"  ({', '.join(parts)})"
    return (f"    {label} progress: {done}/{total}{counts}  "
            f"elapsed={elapsed:.0f}s  ETA={eta:.0f}s")


# Storage attribute names LightRAG's `_insert_done` flushes (lightrag.py
# `_insert_done`). Kept as data so `_all_storages` and its test can be checked
# against the library's own list rather than against each other.
_INSERT_DONE_STORAGE_ATTRS = (
    "chunk_entity_relation_graph",
    "entities_vdb",
    "relationships_vdb",
    "chunks_vdb",
    "text_chunks",
    "full_docs",
    "full_entities",
    "full_relations",
    "doc_status",
    "entity_chunks",
    "relation_chunks",
    "llm_response_cache",
)

# Flushed last, so it never claims a document is done before the graph and
# vectors describing it are durable. See `_ordered_insert_done`.
_PROGRESS_LEDGER_ATTR = "doc_status"


def _all_storages(rag) -> list:
    """The storages LightRAG flushes via `_insert_done`, those present on `rag`.

    Used to suppress per-operation `index_done_callback` across a whole pass
    and flush once at the phase boundary. `FaissVectorDBStorage` has no dirty
    guard (unlike `JsonKVStorage`, which checks `storage_updated` first), so
    each flush unconditionally rewrites all three Faiss index files — roughly
    498MB on this corpus. Flush count is the only lever we control.
    """
    return [s for s in (getattr(rag, a, None) for a in _INSERT_DONE_STORAGE_ATTRS)
            if s is not None]


class LedgerFlushError(RuntimeError):
    """A storage failed to persist, so the progress ledger was not advanced."""


@contextmanager
def _defer_ledger_flush(ledger):
    """Silence `doc_status`'s per-upsert self-flush and hand back the real one.

    `JsonDocStatusStorage.upsert` ends with its own `await
    self.index_done_callback()` (`kg/json_doc_status_impl.py:222`), and LightRAG
    marks a document PROCESSED (`lightrag.py:2162`) *before* calling
    `_insert_done` (`lightrag.py:2185`). So the ledger reaches disk the instant a
    document completes — ahead of the entities, edges and vectors it produced.

    That makes "write the ledger last" fiction: by the time `_flush_ledger_last`
    runs, `storage_updated` is already clear and its call is a no-op against a
    file that is durable. Silencing the per-upsert flush for the pass makes the
    ordered flush the only writer, which is what actually restores the ordering.

    Yields the original callback so `_flush_ledger_last` can still reach the real
    writer while the no-op is installed.

    Failure direction is deliberate: if the ledger ends up flushed *less* often
    than intended, an interrupted run re-does documents (cheap — the LLM response
    cache serves them). The opposite error loses them silently and for good.
    """
    original = ledger.index_done_callback

    async def _noop():
        return True

    ledger.index_done_callback = _noop
    try:
        yield original
    finally:
        ledger.index_done_callback = original


async def _flush_ledger_last(rag, pipeline_status=None,
                             pipeline_status_lock=None,
                             ledger_flush=None) -> None:
    """Flush every storage, then the progress ledger, and only if they all won.

    Replaces LightRAG's `_insert_done`, which gathers all twelve storages
    concurrently in no order, **discards the results**, and logs. Two problems
    follow from that, both of which end in a `doc_status` that lies:

    1. *Ordering.* `doc_status` is ~1MB and lands in milliseconds while the
       graphml and three Faiss indices take seconds to tens of seconds. A crash
       inside that window leaves documents marked PROCESSED whose entities were
       never written.
    2. *Swallowed failures.* `NetworkXStorage.index_done_callback` and
       `FaissVectorDBStorage.index_done_callback` catch their own exceptions,
       log, and return `False`. Since the results are discarded, a failed
       graphml or Faiss write is still followed by a successful ledger write.
       On an external volume (a USB/Thunderbolt hiccup, an unmount) that is a
       realistic path to a silently incomplete graph.

    A lying ledger is not self-correcting: a resumed `--index` reads
    `doc_status` and *skips* those documents, so their entities are missing for
    good with no error. Writing the ledger last, and only when every other
    storage reported success, makes the failure mode redundant re-work instead.

    `ledger_flush` is the ledger's real `index_done_callback`, supplied by
    `_defer_ledger_flush` while that context has swapped the live attribute for a
    no-op. Without it this function would call the no-op and never persist the
    ledger at all. Absent the context, the live attribute is the real one.
    """
    ledger = getattr(rag, _PROGRESS_LEDGER_ATTR, None)
    others = [s for s in _all_storages(rag) if s is not ledger]

    results = await asyncio.gather(*[s.index_done_callback() for s in others],
                                   return_exceptions=True)

    # `False` is this API's failure signal; an exception means the storage
    # didn't even reach its own error handler.
    failed = [
        getattr(s, "namespace", type(s).__name__)
        for s, r in zip(others, results)
        if r is False or isinstance(r, BaseException)
    ]
    if failed:
        raise LedgerFlushError(
            f"{len(failed)} storage(s) failed to persist ({', '.join(failed)}); "
            f"leaving {_PROGRESS_LEDGER_ATTR} un-advanced so a resume re-does "
            f"this work instead of skipping it."
        )

    if ledger is not None:
        await (ledger_flush or ledger.index_done_callback)()

    # Same message and logger LightRAG's own `_insert_done` emits, so existing
    # log-scraping and the pipeline_status history stay unchanged.
    _msg = "In memory DB persist to disk"
    logging.getLogger("lightrag").info(_msg)
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            pipeline_status["latest_message"] = _msg
            pipeline_status["history_messages"].append(_msg)


INDEX_LOCK_FILE = ".index.lock"

# Pass 1 skip ledger, inside graph_dir so `--clear` resets it with the graph.
PASS1_HASH_FILE = "pass1_payload_hashes.json"


def _pass1_payload_hash(payload: dict) -> str:
    """Fingerprint of the structural payload `_topic_to_custom_kg` produced.

    Deliberately hashes the *payload*, not the topic file. A topic edit and a
    change in how nodes are built both alter the payload, so both invalidate —
    which means a corpus-wide fix (e.g. re-keying tag nodes onto slugs) still
    propagates to every topic instead of being silently skipped. Hashing the
    source file would have suppressed exactly that.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class Pass1Action(Enum):
    """What Pass 1 must do with one topic, given what the ledger remembers."""

    SKIP = auto()     # payload unchanged; the graph already holds it
    INSERT = auto()   # never seeded; nothing to replace
    RESEED = auto()   # payload changed; the prior documents must go first


def _pass1_doc_ids(payload: dict) -> list[str]:
    """The document ids one Pass-1 payload creates, in chunk order.

    `ainsert_custom_kg` defaults each chunk's `full_doc_id` to its `source_id`
    (`lightrag.py:2407-2409`), and `_topic_to_custom_kg` sets that to `topic-<id>`
    for the first chunk and `topic-<id>-pN` for overflow chunks. Recording all of
    them matters: purging only the primary id would strand every node whose sole
    source was an overflow chunk.
    """
    ids, seen = [], set()
    for chunk in payload.get("chunks") or []:
        doc_id = chunk.get("source_id")
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            ids.append(doc_id)
    return ids


# Structural entity types, lowercased once. `_topic_to_custom_kg` writes these
# in lowercase to match LightRAG's Pass 2 normalization, so comparisons against
# a stored `entity_type` must fold too.
# NOTE: four other sites still recompute this inline; worth collapsing onto this
# constant as its own change.
STRUCTURAL_TYPES_LOWER = frozenset(t.lower() for t in STRUCTURAL_TYPE_NAMES)


def _pass1_rel_pairs(payload: dict) -> list[list[str]]:
    """The structural relations one Pass-1 payload asserts, as ordered pairs.

    Recorded in the ledger because they are the only durable record of what a
    topic used to claim. `adelete_by_doc_id` cannot recover them: it resolves a
    document to graph elements through `full_entities` / `full_relations`, and
    `ainsert_custom_kg` never registers anything there, so after an index run
    those stores hold only `doc-` keys and a `topic-<id>` purge is a no-op
    against the graph.

    Lists rather than tuples so the ledger stays plain JSON.
    """
    pairs, seen = [], set()
    for rel in payload.get("relationships") or []:
        src, tgt = rel.get("src_id"), rel.get("tgt_id")
        if not src or not tgt or (src, tgt) in seen:
            continue
        seen.add((src, tgt))
        pairs.append([src, tgt])
    return pairs


def _stale_structural_relations(prior, payload: dict) -> list[tuple[str, str]]:
    """Relations the topic used to assert and no longer does.

    Direction is significant: `(a, b)` and `(b, a)` are different edges, and
    collapsing them would delete a live edge while leaving the dead one.

    A ledger entry that recorded no relations yields nothing. Guessing would
    mean deleting edges we cannot prove this topic created.
    """
    if not isinstance(prior, dict):
        return []
    old = prior.get("rels")
    if not isinstance(old, list):
        return []
    current = {tuple(p) for p in _pass1_rel_pairs(payload)}
    return [tuple(p) for p in old
            if isinstance(p, list) and len(p) == 2 and tuple(p) not in current]


def _pass1_plan(prior, payload_hash: str) -> tuple[Pass1Action, list[str]]:
    """Decide this topic's fate, and which prior documents to purge first.

    Split out of the Pass 1 loop so the decision is testable on its own. A
    malformed `prior` degrades to INSERT, never SKIP: re-seeding costs time,
    while skipping work that was never done leaves the graph missing nodes with
    no error at all.

    Returns only what the ledger actually recorded. A v1 entry has no recorded
    ids, so this returns `[]` and leaves deriving a fallback to the caller,
    which knows the topic id and can rebuild `topic-<id>` from it.
    """
    if not isinstance(prior, dict) or not isinstance(prior.get("hash"), str):
        return Pass1Action.INSERT, []
    if prior["hash"] == payload_hash:
        return Pass1Action.SKIP, []
    docs = prior.get("docs")
    return Pass1Action.RESEED, list(docs) if isinstance(docs, list) else []


def _pass2_doc_id(topic: dict) -> str:
    """The id Pass 2's `ainsert` will give this topic's document.

    Pass 2 inserts `topic_to_document(t)` for every topic and keys dedupe on
    `compute_mdhash_id(sanitize_text_for_encoding(text), prefix="doc-")`
    (`lightrag.py:1424-1430`). Recording it during Pass 1 is what lets a later
    run purge the *previous* text's document: once the topic changes, the old
    text is gone and its id can no longer be recomputed from anything on disk.

    The sanitizer runs before the hash. Skipping it yields a plausible id that
    matches no document, so the purge would delete nothing and report success.
    """
    from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding

    return compute_mdhash_id(
        sanitize_text_for_encoding(topic_to_document(topic)), prefix="doc-")


async def _cites_only_dead_chunks(rag, node) -> bool:
    """True when every chunk this node cites has been deleted.

    Such a node is stale by construction: the text it was derived from is gone
    from the corpus. Returns False when it cites no chunks at all, so a node of
    unknown provenance is kept rather than guessed away.
    """
    cited = [c for c in (node.get("source_id") or "").split("<SEP>")
             if c.startswith("chunk-")]
    if not cited:
        return False
    for chunk_id in cited:
        if await rag.text_chunks.get_by_id(chunk_id):
            return False
    return True


async def _purge_stale_structure(rag, stale_rels, tid) -> tuple[int, int]:
    """Retract the structural claims a changed topic no longer makes.

    Two steps, in order. Relations first, because they are topic-scoped and so
    can be deleted exactly. Then any endpoint left with no edges at all — but
    only structural ones: a tag or user is shared across topics and may only go
    once nothing references it, and a content entity is never ours to remove.

    Checking the live degree rather than reasoning about it matters, because
    the same tag is usually reachable from other topics; the node must survive
    those.

    RULE #2: `adelete_by_relation` and `adelete_by_entity` each flush five
    storages per call, so this only runs inside an active suppression context.

    Returns `(edges_deleted, nodes_deleted)`.
    """
    graph = rag.chunk_entity_relation_graph
    endpoints, edges = set(), 0
    for src, tgt in stale_rels:
        try:
            await rag.adelete_by_relation(src, tgt)
            edges += 1
            endpoints.update((src, tgt))
        except Exception as e:
            print(f"  Topic id={tid}: could not retract {src!r} -> {tgt!r} "
                  f"({type(e).__name__}: {e}); continuing.", flush=True)

    nodes = 0
    for name in sorted(endpoints):
        try:
            node = await graph.get_node(name)
            if not node:
                continue
            if (node.get("entity_type") or "").strip().lower() not in STRUCTURAL_TYPES_LOWER:
                continue
            # Two ways a structural node is now dead. No edges at all is the
            # obvious one. The subtler one is a node whose every cited chunk
            # has been deleted: it describes text that is no longer in the
            # corpus, so any edge it still carries describes it too. A renamed
            # tag hits exactly this case, because Pass 2 had extracted content
            # edges from the old text that outlive the tag edge.
            if await graph.get_node_edges(name) and not await _cites_only_dead_chunks(rag, node):
                continue
            await rag.adelete_by_entity(name)
            nodes += 1
        except Exception as e:
            print(f"  Topic id={tid}: could not drop orphaned {name!r} "
                  f"({type(e).__name__}: {e}); continuing.", flush=True)
    return edges, nodes


async def _purge_prior_docs(rag, doc_ids, tid) -> int:
    """Delete the documents a topic produced last time. Returns how many went.

    CLAUDE.md RULE #2: `adelete_by_doc_id` flushes **all twelve** storages
    **twice** per call, so this is only safe inside an active suppression
    context. Pass 1 already runs suppressed; `PurgeWriteBatchingTests` is what
    keeps that true.

    Failures are per-document and non-fatal. A recorded id may already be gone
    (a prior run purged it, then died before rewriting the ledger), and aborting
    there would strand the remaining ids and kill a multi-hour run over a
    document that is already in the state we want.
    """
    purged = 0
    for doc_id in doc_ids:
        try:
            await rag.adelete_by_doc_id(doc_id)
            purged += 1
        except Exception as e:
            print(f"  Topic id={tid}: could not purge prior doc {doc_id!r} "
                  f"({type(e).__name__}: {e}); continuing.", flush=True)
    return purged


def _load_pass1_hashes(graph_dir: Path) -> dict:
    """Previously-seeded payload hashes; empty on anything unreadable.

    Degrading to empty means "re-seed everything", which is slow but correct.
    The opposite failure — trusting a damaged ledger — would skip work that was
    never done and leave the graph missing nodes with no error.

    Normalizes the v1 shape (`{tid: hash}`) to v2 (`{tid: {hash, docs}}`). v1
    ledgers predate document purging, so they carry no ids to purge; the
    migration is read-side only, and a v1 file is rewritten as v2 on the next
    checkpoint.
    """
    try:
        data = json.loads((graph_dir / PASS1_HASH_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {tid: {"hash": entry, "docs": []} if isinstance(entry, str) else entry
            for tid, entry in data.items()}


async def _checkpoint_pass1(rag, graph_dir: Path, seen: dict, dirty: bool,
                            ledger_only: bool = False) -> bool:
    """Persist the graph, then advance the Pass-1 skip ledger. Returns new `dirty`.

    Module-level rather than a closure so the invariant below can actually be
    tested. The invariant is: **the ledger never advances without a verified
    flush of the state it describes.**

    The ledger's entire claim is "this topic's payload is durably in the graph".
    An earlier version saved it on the topic index while the flush ran off
    LightRAG's `_insert_done` counter — which only advances when a topic is
    genuinely inserted. On a resume that skipped 1,314 of 1,399 topics the
    counter never reached its interval, so the ledger recorded ~80 topics as
    seeded while not one byte of graphml or Faiss had been written. A kill there
    made the next resume skip those topics permanently, silently, with no error.

    `dirty=False` short-circuits the whole thing: nothing was inserted, so there
    is nothing to persist and nothing new to record. That guard is also what
    keeps a mostly-skipped resume from rewriting Faiss's ~500MB of indices for no
    reason — `FaissVectorDBStorage.index_done_callback` has no dirty guard of its
    own and rewrites in full on every call.

    `_flush_ledger_last` raises `LedgerFlushError` if any storage failed, which
    propagates past `_save_pass1_hashes` and leaves those topics un-recorded —
    redundant re-work next run instead of silent loss.
    """
    # One save call, and the flush textually precedes it, so both coupling
    # guards in `tests/test_index_safety_guards.py` still hold.
    #
    # `ledger_only` covers entries for topics that were *skipped*: the hash
    # matched, so the payload is already durable and the ledger only gains a
    # fuller description of state that did not change. That is the single case
    # where saving without a flush is sound — there is nothing to attest to
    # that is not already on disk.
    if not dirty and not ledger_only:
        return False
    if dirty:
        await _flush_ledger_last(rag)
    _save_pass1_hashes(graph_dir, seen)
    return False


def _save_pass1_hashes(graph_dir: Path, hashes: dict) -> None:
    graph_dir.mkdir(parents=True, exist_ok=True)
    tmp = graph_dir / f"{PASS1_HASH_FILE}.tmp"
    tmp.write_text(json.dumps(hashes, sort_keys=True))
    tmp.replace(graph_dir / PASS1_HASH_FILE)


class IndexLockError(RuntimeError):
    """Another indexing run already holds this data dir."""


@contextmanager
def index_lock(data_dir: Path):
    """Refuse to index a data dir that another process is already indexing.

    Two concurrent indexers on one `graphrag/` corrupt it. LightRAG's storages
    are process-local in-memory copies flushed wholesale, so the second writer
    does not merge — it overwrites with its own view. Observed 2026-08-14: three
    overlapping runs took a graph from 15,756 nodes to 4,566, and the resulting
    lock contention and reload-on-`storage_updated` churn looked like unexplained
    process deaths, wedged embedding pools, and network faults for hours.

    That happened because a liveness check used `pgrep -f
    "discourse_explorer.query"`, which cannot match the console entry point's
    command line (`discourse-explorer query`, hyphen and space). Every "the run
    died" reading was wrong, and a fresh run was launched on top each time.
    Hence a lock rather than a better process check: correctness must not depend
    on getting a pattern right.

    `flock` is released automatically when the process exits for any reason,
    including SIGKILL, so a crashed run never leaves the dir permanently locked.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / INDEX_LOCK_FILE
    handle = path.open("a+")
    held = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError as exc:
            # Only EWOULDBLOCK/EAGAIN/EACCES means "someone else holds it". A
            # filesystem without flock support raises ENOTSUP/EOPNOTSUPP, and
            # reporting that as a live holder would refuse every run on such a
            # volume and send the operator straight back into the process-hunting
            # dead end this lock exists to end. Warn and proceed instead: no
            # mutual exclusion is the status quo ante, a permanent refusal is not.
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                handle.seek(0)
                holder = handle.read().strip() or "unknown"
                raise IndexLockError(
                    f"another indexing run is already active on {data_dir} "
                    f"({holder}).\n"
                    f"  Check it:  pgrep -fl 'discourse-explorer query|discourse_explorer.query'\n"
                    f"  A stale lock file is harmless — the OS drops the lock when "
                    f"that process exits."
                ) from None
            print(f"  WARNING: {data_dir} does not support flock "
                  f"({exc.strerror}); proceeding WITHOUT mutual exclusion. "
                  f"Confirm no other indexer is running:\n"
                  f"    pgrep -fl 'discourse-explorer query|discourse_explorer.query'",
                  flush=True)

        if held:
            handle.seek(0)
            handle.truncate()
            handle.write(
                f"pid={os.getpid()} "
                f"started={datetime.now().isoformat(timespec='seconds')}\n")
            handle.flush()
        yield
    finally:
        try:
            # Only unlock what we actually locked; LOCK_UN on an unheld fd can
            # raise on the very filesystems that refused the lock, which would
            # mask whatever exception the body was already propagating.
            if held:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _should_flush(counter: int, every: int, suppressed: bool) -> bool:
    """Whether the `_insert_done` call at `counter` should really persist.

    `suppressed` is set while a pass has swapped every storage's
    `index_done_callback` for a no-op (see `_suppress_index_done`) and will
    flush once at its own phase boundary instead. Without this gate the batched
    flush still fires on schedule, gathers a set of no-ops, observes them all
    succeed, and logs "In memory DB persist to disk" for a persist that never
    touched the disk — a log line asserting durability that does not exist.
    """
    if suppressed:
        return False
    return counter % every == 0


def _read_provenance(path: Path) -> str | None:
    """Extraction model recorded in a provenance file, or None if unreadable."""
    try:
        return json.loads(path.read_text()).get("extraction_model")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _read_cache_model(graph_dir: Path) -> str | None:
    """Extraction model that produced the on-disk LLM response cache, if known."""
    return _read_provenance(graph_dir / CACHE_PROVENANCE_FILE)


def _write_cache_model(graph_dir: Path, extraction_model: str) -> None:
    graph_dir.mkdir(parents=True, exist_ok=True)
    # Atomic: a torn provenance file reads as unknown provenance, which discards
    # a perfectly good ~100MB cache on the next `--clear`.
    dest = graph_dir / CACHE_PROVENANCE_FILE
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps({"extraction_model": extraction_model}, indent=2) + "\n")
    tmp.replace(dest)


def _cache_is_parseable(cache: Path) -> bool:
    """Whether the on-disk LLM cache is valid JSON.

    LightRAG writes it non-atomically (`utils.py::write_json` is a plain
    `open(w)` + `json.dump`) and reads it without catching `JSONDecodeError`, so
    a run killed mid-write leaves a truncated file that makes every subsequent
    startup raise. Preserving those bytes across `--clear` would turn the one
    command that used to recover the data dir into the thing that perpetuates
    the corruption.
    """
    try:
        json.loads(cache.read_text())
        return True
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        print(f"  {cache.name} is unreadable or truncated — discarding it.",
              flush=True)
        return False


def _ensure_cache_provenance(graph_dir: Path, extraction_model: str) -> None:
    """Drop the LLM response cache unless it provably came from this model.

    `lightrag/utils.py::generate_cache_key` builds keys as
    `mode:cache_type:hash(prompt)` with **no model component**, so a cache
    written by model A will happily serve hits to model B and silently poison
    the extraction. An unlabelled cache is treated as unknown provenance and
    discarded — refusing to trust it is cheaper than a wrong graph.
    """
    cache = graph_dir / LLM_CACHE_FILE
    if cache.exists() and _read_cache_model(graph_dir) != extraction_model:
        prior = _read_cache_model(graph_dir) or "unknown"
        print(f"  LLM response cache was built by {prior!r}, not "
              f"{extraction_model!r} — discarding it to avoid cross-model hits.",
              flush=True)
        cache.unlink()
    _write_cache_model(graph_dir, extraction_model)


def _slug_for_filename(value: str) -> str:
    """Filename-safe rendering of a model name, for the cache-stash suffix."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value) or "unknown"


def _clear_graph_dir(graph_dir: Path, extraction_model: str) -> bool:
    """Wipe the graph for a `--clear` rebuild, keeping the LLM response cache.

    Everything derived (graphml, Faiss indices, KV stores, doc_status) is
    rebuilt from scratch, but cached LLM completions are model-deterministic:
    re-extracting an unchanged chunk with the same model yields the same
    answer, so paying for it twice is pure waste. Only reused when provenance
    confirms the same extraction model — see `_ensure_cache_provenance`.

    Returns True iff the cache survived.

    Safe to call when `graph_dir` does not exist: a run killed between the
    `rmtree` and the `mkdir` leaves exactly that state with a stash beside it,
    and an early return would strand the stash forever.
    """
    # The stash carries its provenance IN ITS FILENAME rather than in a
    # companion file. Two files meant two renames, and a kill between them left
    # the provenance stashed while the cache was still in the tree — after which
    # the next `--clear` read "unknown provenance" and deleted a perfectly good
    # ~100MB cache. One rename has no such window.
    prefix = f".{LLM_CACHE_FILE}."
    stash = graph_dir.parent / f"{prefix}{_slug_for_filename(extraction_model)}.keep"

    cache = graph_dir / LLM_CACHE_FILE
    orphans = sorted(p for p in graph_dir.parent.glob(f"{prefix}*.keep"))

    # An in-tree cache is always at least as fresh as a stash left behind by an
    # earlier crash, so it wins outright and the orphans are dropped. Letting a
    # stale stash override a live cache would silently roll the cache backwards.
    recovered = False
    if orphans and not cache.exists():
        match = next((p for p in orphans if p == stash), None)
        if match is not None and _cache_is_parseable(match):
            print(f"  Recovering orphaned cache stash {match.name} from an "
                  f"interrupted earlier run.", flush=True)
            recovered = True
        for p in orphans:
            if not (recovered and p == stash):
                print(f"  Discarding unusable cache stash {p.name}.", flush=True)
                p.unlink(missing_ok=True)
    else:
        for p in orphans:
            p.unlink(missing_ok=True)

    if not graph_dir.exists():
        graph_dir.mkdir(parents=True)
        if recovered:
            stash.replace(graph_dir / LLM_CACHE_FILE)
        _write_cache_model(graph_dir, extraction_model)
        return recovered

    reusable = recovered or (cache.exists()
                             and _read_cache_model(graph_dir) == extraction_model
                             and _cache_is_parseable(cache))
    if cache.exists() and not reusable:
        prior = _read_cache_model(graph_dir) or "unknown"
        print(f"  LLM response cache was built by {prior!r}, not "
              f"{extraction_model!r} — not carrying it across the wipe.",
              flush=True)

    # Move the cache out of the tree rather than copying its bytes into memory:
    # the file runs to tens of MB and grows with every run, and a copy would
    # make the in-process buffer the only surviving copy for the duration of
    # the rmtree.
    if reusable and not recovered:
        cache.replace(stash)

    shutil.rmtree(graph_dir)
    graph_dir.mkdir(parents=True)

    if reusable:
        stash.replace(graph_dir / LLM_CACHE_FILE)
    _write_cache_model(graph_dir, extraction_model)
    return reusable


# ---------------------------------------------------------------------------
# RAG factory
# ---------------------------------------------------------------------------


_RERANK_PROVIDERS = {"jina", "cohere", "ali"}


def _build_rerank_func(rc: RuntimeConfig):
    """Return a `rerank_model_func` callable for LightRAG, or None if no
    rerank provider is configured.

    Provider value comes from `RERANK_PROVIDER` env ∈ {jina, cohere, ali}.
    Each shipped LightRAG rerank helper takes a Cohere/Jina/Ali API URL;
    for a self-hosted BAAI/bge-reranker served via HuggingFace TEI (which
    exposes a Cohere-compatible endpoint) set `RERANK_PROVIDER=cohere`
    and point `RERANK_BASE_URL` at the local server. Same for llama.cpp
    `llama-server --rerank` if you wrap its response shape.
    """
    provider = rc.rerank_provider
    if not provider:
        return None
    if provider not in _RERANK_PROVIDERS:
        raise ConfigError(
            f"RERANK_PROVIDER={provider!r} is not supported. "
            f"Valid: {sorted(_RERANK_PROVIDERS)}, or unset to disable rerank."
        )

    if provider == "jina":
        from lightrag.rerank import jina_rerank as _impl
    elif provider == "cohere":
        from lightrag.rerank import cohere_rerank as _impl
    else:  # "ali"
        from lightrag.rerank import ali_rerank as _impl

    # Capture env values at bind time so the closure doesn't re-read later.
    _model = rc.rerank_model or None
    _base_url = rc.rerank_base_url or None
    _api_key = rc.rerank_api_key or None

    async def _rerank(query, documents, top_n=None, **kwargs):
        call_kwargs = {"api_key": _api_key, "top_n": top_n}
        if _model is not None:
            call_kwargs["model"] = _model
        if _base_url is not None:
            call_kwargs["base_url"] = _base_url
        # Propagate any extra kwargs LightRAG passes through (e.g. future knobs).
        call_kwargs.update(kwargs)
        return await _impl(query=query, documents=documents, **call_kwargs)

    return _rerank


def _get_rag(
    rc: RuntimeConfig,
    model_name: str = None,
    gleaning: int | None = None,
    llm_model_max_async: int | None = None,
    max_parallel_insert: int | None = None,
):
    """Create a LightRAG instance. Auto-selects OpenAI or Ollama.

    `gleaning` controls entity_extract_max_gleaning. If None, falls back to
    the per-run config value.

    `llm_model_max_async` / `max_parallel_insert` control LightRAG concurrency.
    If None, fall back to RuntimeConfig, then to provider-specific defaults
    (OpenAI: 8/4, Ollama: 1/_unspecified_). Tier 3 OpenAI accounts can safely
    push `llm_model_max_async` to ~13 per `--detect-limits`.
    """
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc

    paths = rc.paths()
    graph_dir = paths.graphrag_dir
    graph_dir.mkdir(parents=True, exist_ok=True)

    llm_model = model_name or rc.default_extraction_model()
    gleaning_passes = rc.gleaning if gleaning is None else gleaning

    # Resolve concurrency: CLI override > env (via rc) > provider default.
    _async = llm_model_max_async if llm_model_max_async is not None else rc.llm_model_max_async
    _parallel = max_parallel_insert if max_parallel_insert is not None else rc.max_parallel_insert
    _resolved_async = _async or (8 if rc.is_openai else 1)
    _resolved_parallel = _parallel or 4

    # Rerank: None = LightRAG emits a warning at query time unless
    # QueryParam.enable_rerank is also flipped to False (handled in query_graph).
    _rerank_func = _build_rerank_func(rc)

    # Pass 2 (LLM extraction) may only produce content types; structural types
    # are owned by Pass 1 and must not be re-extracted to avoid overwriting
    # deterministic typing on name merge.
    vocab = load_entity_types(rc.data_dir)
    content_types = content_type_names(vocab)

    if rc.is_openai:
        from lightrag.llm.openai import openai_complete_if_cache, openai_embed

        # openai_complete_if_cache takes (model, prompt, ...) but LightRAG
        # calls llm_model_func as (prompt, ...). Bind the model name.
        async def _openai_llm(prompt, system_prompt=None, history_messages=[], **kwargs):
            return await openai_complete_if_cache(
                llm_model, prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                **kwargs,
            )

        openai_embed_model = rc.openai_embed_model

        async def _safe_openai_embed(texts):
            """Sanitize texts before sending to OpenAI to avoid JSON parse errors."""
            clean = [_sanitize(t) if t else t for t in texts]
            return await openai_embed.func(clean, model=openai_embed_model)

        return LightRAG(
            working_dir=str(graph_dir),
            llm_model_func=_openai_llm,
            llm_model_name=llm_model,
            # Concurrency knobs are now env/CLI-configurable via
            # LLM_MODEL_MAX_ASYNC / MAX_PARALLEL_INSERT (see bootstrap());
            # the defaults below match the historical hardcoded values.
            llm_model_max_async=_resolved_async,
            # Passed explicitly rather than left to the ambient
            # FORCE_LLM_SUMMARY_ON_MERGE env var LightRAG would otherwise read,
            # so the summarization cascade cannot depend on how the run was
            # launched. See config.SUMMARY_ON_MERGE_DEFAULT.
            force_llm_summary_on_merge=rc.force_llm_summary_on_merge,
            embedding_func=EmbeddingFunc(
                embedding_dim=rc.openai_embed_dim,
                max_token_size=8192,
                func=_safe_openai_embed,
            ),
            embedding_func_max_async=_resolved_async,
            max_parallel_insert=_resolved_parallel,
            entity_extract_max_gleaning=gleaning_passes,
            enable_llm_cache=True,
            # Faiss stores normalized float32 vectors in a binary index file
            # (`faiss_index_<namespace>.index` + `.meta.json`), replacing the
            # JSON-text `vdb_<namespace>.json` of NanoVectorDB. Expect ~3-5x
            # smaller on-disk footprint at 3072d. Switching storage backends
            # requires `--index --clear` (vectors aren't portable between them).
            vector_storage="FaissVectorDBStorage",
            vector_db_storage_cls_kwargs={
                "cosine_better_than_threshold": 0.2,
            },
            rerank_model_func=_rerank_func,
            addon_params={
                "language": "English",
                "entity_types": content_types,
            },
        )
    else:
        from lightrag.llm.ollama import ollama_embed, ollama_model_complete

        ollama_host = rc.ollama_host
        embed_model = rc.embed_model

        return LightRAG(
            working_dir=str(graph_dir),
            llm_model_func=ollama_model_complete,
            llm_model_name=llm_model,
            llm_model_max_async=_resolved_async,
            # See the OpenAI branch above: explicit, not ambient env.
            force_llm_summary_on_merge=rc.force_llm_summary_on_merge,
            llm_model_kwargs={
                "host": ollama_host,
                "options": {"num_ctx": 32768},
            },
            embedding_func=EmbeddingFunc(
                embedding_dim=rc.ollama_embed_dim,
                max_token_size=8192,
                func=lambda texts: ollama_embed.func(
                    texts, embed_model=embed_model, host=ollama_host,
                ),
            ),
            entity_extract_max_gleaning=gleaning_passes,
            enable_llm_cache=True,
            # See the OpenAI branch above for Faiss rationale.
            vector_storage="FaissVectorDBStorage",
            vector_db_storage_cls_kwargs={
                "cosine_better_than_threshold": 0.2,
            },
            rerank_model_func=_rerank_func,
            addon_params={
                "language": "English",
                "entity_types": content_types,
            },
        )


# ---------------------------------------------------------------------------
# Document preparation (domain-specific, RAG-library-agnostic)
# ---------------------------------------------------------------------------


# Tag identity lives in config.py so the graph, the stats views and
# QUERY-GUIDE.md all derive it from one place. Aliased here because this module
# is where it is consumed most (Pass 1 node names + the LLM-visible header).
#
# Note it does *not* unify tag nodes with version entities the LLM extracts from
# prose, which use a real period (`2025.06`, 211 mentions vs 13 for the hyphen
# form). That's a separate aliasing concern and belongs in the Pass 4
# canonicalization chain, where it can be fixed without a re-index.
_tag_label = tag_label


def topic_to_document(topic: dict) -> str:
    """Convert a topic JSON to a plain-text document for ingestion."""
    category = topic.get("category_name", "General")
    title = topic.get("title", "")
    # Deliberately the display `name`, NOT `tag_label`'s slug — unlike the graph
    # nodes in `_topic_to_custom_kg`.
    #
    # This string is part of the document text, and LightRAG keys its
    # doc-dedupe on md5 of that text. Switching it to the slug changed the
    # header for 1,018 of 1,399 topics on the production corpus, which made every
    # one of them look like a brand-new document: an 85-document incremental
    # update became a 1,099-document re-extraction, ~$6 instead of ~$0.45,
    # measured 2026-08-14. The retrieval benefit of normalizing tags lives in
    # the graph node names, which this does not affect.
    tags = ", ".join(
        label for label in (tag_display(t) for t in topic.get("tags", [])) if label
    )
    created = topic.get("created_at", "")[:10]
    status = []
    if topic.get("closed"):
        status.append("closed")
    if topic.get("archived"):
        status.append("archived")
    status_str = f" [{', '.join(status)}]" if status else ""

    header = f"[{category}] {title}{status_str}"
    if tags:
        header += f" (tags: {tags})"
    header += f" ({created})"

    parts = [header, ""]
    for post in topic.get("posts", []):
        author = post.get("username", "unknown")
        text = _elide_encoded_blobs(post.get("plain_text", "").strip())
        if not text:
            continue
        parts.append(f"{author}:")
        reply_to = post.get("reply_to_post_number")
        if reply_to:
            parts.append(f"(in reply to post #{reply_to})")
        parts.append(text)
        parts.append("")

    return "\n".join(parts)


# OpenAI embedding APIs cap input at 8192 tokens; 8000 leaves a safety buffer
# for tokenizer-vs-server drift. Applies to all OpenAI embedding models.
_EMBED_TOKEN_LIMIT = 8000
_embed_tokenizer = None


def _get_embed_tokenizer():
    global _embed_tokenizer
    if _embed_tokenizer is None:
        import tiktoken
        _embed_tokenizer = tiktoken.get_encoding("cl100k_base")
    return _embed_tokenizer


def _split_for_embedding(text: str, max_tokens: int = _EMBED_TOKEN_LIMIT) -> list[str]:
    """Split text so each piece fits under the embedding-API input cap.

    ainsert_custom_kg feeds the chunks we provide straight to the embedder
    without re-chunking; long Discourse threads exceed the 8192-token cap, so
    we pre-split here. Uses cl100k_base (matches OpenAI text-embedding-3-*).
    """
    enc = _get_embed_tokenizer()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return [text]
    return [enc.decode(tokens[i:i + max_tokens]) for i in range(0, len(tokens), max_tokens)]


def _topic_to_custom_kg(topic: dict) -> dict:
    """Build a custom KG payload from a topic's structured fields.

    This bypasses LLM extraction for facts that are already explicit in the JSON:
    the category, the tags, and which users posted in the topic. The LLM still
    extracts content-level entities from the post bodies via the parallel
    ainsert() call, and entities sharing names will merge by the standard
    LightRAG merge logic.

    Long topics (20+ posts) can exceed the 8192-token embedding-API cap, so the
    topic document is split into multiple chunks here. Entities/relationships
    reference `source_id=tid` which resolves to the first chunk; subsequent
    chunks use `tid-p1`, `tid-p2`, ... so they don't collide.
    """
    tid = f"topic-{topic['id']}"
    cat = topic.get("category_name", "General")
    title = topic.get("title", f"Topic {topic['id']}")

    # Entity types are lowercase to match LightRAG's Pass 2 normalization
    # (`operate.py::_handle_single_entity_extraction` lowercases all LLM-extracted
    # types). Keeping Pass 1 lowercase too means the Counter-based merge at
    # `operate.py::_merge_nodes_then_upsert` can actually see them as the same
    # label — without the case fold, PascalCase + lowercase never agree and
    # Pass 1 loses every merge tie.
    entities = [
        {"entity_name": cat, "entity_type": "category",
         "description": f"Forum category: {cat}", "source_id": tid},
        {"entity_name": title, "entity_type": "topic",
         "description": f"Discussion thread in the {cat} category", "source_id": tid},
    ]
    relationships = [
        {"src_id": title, "tgt_id": cat,
         "description": f"Topic posted in the {cat} category",
         "keywords": STRUCTURAL_REL_KEYWORDS["topic_in_category"],
         "weight": 1.0, "source_id": tid},
    ]


    seen_users: set[str] = set()
    for tag in topic.get("tags", []):
        name = _tag_label(tag)
        if not name:
            continue
        entities.append({"entity_name": name, "entity_type": "tag",
                         "description": f"Forum tag: {name}", "source_id": tid})
        relationships.append({"src_id": title, "tgt_id": name,
                              "description": f"Topic tagged with {name}",
                              "keywords": STRUCTURAL_REL_KEYWORDS["topic_tagged"],
                              "weight": 0.8, "source_id": tid})

    for post in topic.get("posts", []):
        u = post.get("username")
        if not u or u in seen_users:
            continue
        seen_users.add(u)
        entities.append({"entity_name": u, "entity_type": "user",
                         "description": f"Forum user: {u}", "source_id": tid})
        relationships.append({"src_id": u, "tgt_id": title,
                              "description": f"{u} posted in this topic",
                              "keywords": STRUCTURAL_REL_KEYWORDS["user_posted"],
                              "weight": 0.5, "source_id": tid})

    doc_text = topic_to_document(topic)
    chunk_parts = _split_for_embedding(doc_text)
    chunks = [
        {
            "content": part,
            # First chunk keeps the canonical tid (entities/rels resolve against it);
            # overflow chunks get suffixed so their source_ids stay unique.
            "source_id": tid if i == 0 else f"{tid}-p{i}",
            "file_path": f"topic-{topic['id']}.json",
        }
        for i, part in enumerate(chunk_parts)
    ]

    return {
        "chunks": chunks,
        "entities": entities,
        "relationships": relationships,
    }


def _pass3_storages(rag) -> list:
    """Storages that `aedit_entity` flushes via `_persist_graph_updates`.

    See `lightrag/utils_graph.py:507-513` in the installed package —
    `aedit_entity` calls `_persist_graph_updates` with exactly these five
    storages (plus optional entity/relation chunks). Listing them here lets
    us suppress per-edit `index_done_callback` during Pass 3 and flush once
    at the end.
    """
    return [
        s for s in [
            getattr(rag, "entities_vdb", None),
            getattr(rag, "relationships_vdb", None),
            getattr(rag, "chunk_entity_relation_graph", None),
            getattr(rag, "entity_chunks", None),
            getattr(rag, "relation_chunks", None),
        ] if s is not None
    ]


@contextmanager
def _suppress_index_done(storages: list):
    """Swap each storage's `index_done_callback` to an async no-op for the
    duration of the context. Callers MUST flush once explicitly (via
    `_flush_storages`) after the context exits to persist the accumulated
    mutations.

    Safe per LightRAG's deferred-persistence contract (see
    `NetworkXStorage.upsert_node:136-138`: *"Changes will be persisted to
    disk during the next index_done_callback"*). Not persisting between ops
    is the documented model; per-op flushing is a conservative default
    inside the CRUD helpers, not a correctness requirement.

    Eliminates two problems during Pass 3 on large graphs: (1) ~1800×
    redundant graphml writes (each ~20MB), and (2) per-edit storage locks
    that serialize the concurrent `asyncio.gather` worker pool.
    """
    async def _noop():
        return True
    originals = [(s, s.index_done_callback) for s in storages]
    for s in storages:
        s.index_done_callback = _noop
    try:
        yield
    finally:
        for s, orig in originals:
            s.index_done_callback = orig


@asynccontextmanager
async def batched_graph_writes(rag):
    """The ONLY sanctioned way to mutate the graph in bulk. See CLAUDE.md RULE #2.

    Every LightRAG graph helper persists on **every call**, and the cost is not
    proportional to the edit:

    | call                                        | flushes | scope        |
    |---------------------------------------------|---------|--------------|
    | `aedit_entity` / `amerge_entities`          | 1       | 5 storages   |
    | `aedit_relation` / `adelete_by_relation`    | 1       | 5 storages   |
    | `adelete_by_entity` / `acreate_*`           | 1       | 5 storages   |
    | `ainsert_custom_kg`                         | 1       | all 12       |
    | `adelete_by_doc_id`                         | **2**   | all 12       |

    `FaissVectorDBStorage.index_done_callback` has no dirty guard, so each flush
    rewrites ~500MB of index files whether or not they changed, plus a ~20MB
    graphml. A bare loop of N edits therefore costs N x ~520MB: 54 edits is
    ~27GB, and deleting an 85-topic delta via `adelete_by_doc_id` is ~88GB.

    Suppression covers **all** storages, not just the five `_persist_graph_updates`
    touches, so it also neutralises the `_insert_done` path that
    `ainsert_custom_kg` and `adelete_by_doc_id` use. The single flush on exit is
    `_flush_ledger_last`, so it is ordered (ledger last) and result-checked
    (raises `LedgerFlushError` rather than silently swallowing a failed write).

        async with batched_graph_writes(rag):
            for src, tgt in doomed:
                await rag.adelete_by_relation(src, tgt)
        # exactly one write per file, here

    Deliberately not a no-op-on-error: if the body raises, the flush is skipped
    and the in-memory mutations are dropped. That is the safe direction, since a
    half-applied bulk edit persisted to disk is worse than one not applied.
    """
    stores = _all_storages(rag)
    with _suppress_index_done(stores):
        yield stores
    await _flush_ledger_last(rag)


async def _flush_storages(storages: list) -> None:
    """Explicit one-shot flush of the storage set. Pairs with
    `_suppress_index_done` to replace the suppressed per-op flushes with a
    single batched one at phase boundaries.

    Raises `LedgerFlushError` if any storage failed. This is not optional
    bookkeeping: `NetworkXStorage.index_done_callback` and
    `FaissVectorDBStorage.index_done_callback` both catch their own write errors,
    log, and return `False`. A bare `gather` therefore reports success for a
    graphml write that never happened, and the caller goes on to record progress
    against a graph that does not contain it. Since these are the *only* flush
    points for a suppressed pass, a swallowed failure here loses the entire
    pass's work with nothing in the exit status to show for it.
    """
    results = await asyncio.gather(*[s.index_done_callback() for s in storages],
                                   return_exceptions=True)
    failed = [
        getattr(s, "namespace", type(s).__name__)
        for s, r in zip(storages, results)
        if r is False or isinstance(r, BaseException)
    ]
    if failed:
        raise LedgerFlushError(
            f"{len(failed)} storage(s) failed to persist ({', '.join(failed)})."
        )


async def _enrich_structural_types(
    rag,
    topics: list[dict],
    concurrency: int = 8,
    force_rewrite: bool = False,
) -> tuple[int, int]:
    """Force-reassert structural entity types after Pass 2 may have overwritten them.

    Walks every topic, recomputes the structural (name, type) pairs via
    `_topic_to_custom_kg`, deduplicates across topics, and calls
    `rag.aedit_entity(name, {"entity_type": type})` for each one. `aedit_entity`
    is a direct write — it bypasses the Counter-based merge, so this re-assertion
    survives any future Pass 2 re-extractions that share names.

    Runs the re-assertions concurrently, bounded by `concurrency`, to match the
    OpenAI rate limits that govern the re-embedding call inside `aedit_entity`.
    Without this, the sequential loop dominates wall-clock on large corpora
    (~12s per call × ~1800 uniques ≈ 6h; at concurrency=13 it drops to ~30min).

    Returns `(succeeded, attempted)`.
    """
    from discourse_explorer.config import STRUCTURAL_TYPE_NAMES
    structural_lower = {t.lower() for t in STRUCTURAL_TYPE_NAMES}

    # Deduplicate (name, type) across topics: a category like "Data Services"
    # appears in many topics but only needs one re-assertion.
    seen: set[tuple[str, str]] = set()
    for topic in topics:
        kg = _topic_to_custom_kg(topic)
        for ent in kg.get("entities", []):
            etype = ent.get("entity_type", "").lower()
            if etype not in structural_lower:
                continue
            seen.add((ent["entity_name"], etype))

    total = len(seen)
    sem = asyncio.Semaphore(concurrency)
    completed = 0
    completed_lock = asyncio.Lock()
    succeeded = 0
    skipped_ok = 0

    async def _enrich_one(name: str, etype: str) -> bool:
        nonlocal completed, succeeded, skipped_ok
        async with sem:
            # Skip-if-already-correct (normal-mode only): an `aedit_entity`
            # call that doesn't change the stored type still pays the full
            # cost (re-embed + graph write + Faiss upsert). A cheap
            # `get_node` read bypasses that for entities already at the
            # target type — the majority of cases after a successful full
            # index. In `force_rewrite=True` mode (set by `--enrich-only`),
            # we skip the gate: the user's intent is *specifically* to
            # refresh embeddings even when the graph type already matches.
            if force_rewrite:
                was_skip = False
            else:
                try:
                    existing = await rag.chunk_entity_relation_graph.get_node(name)
                except Exception:
                    existing = None
                was_skip = bool(existing and existing.get("entity_type") == etype)

            if was_skip:
                ok = True
            else:
                try:
                    await rag.aedit_entity(
                        name,
                        updated_data={"entity_type": etype},
                        allow_rename=False,
                    )
                    ok = True
                except Exception as e:
                    # Entity may not exist if Pass 1 insertion silently failed, or
                    # the description merge dropped the node. Log and continue; a
                    # partial enrichment is still strictly better than none.
                    print(f"  Enrichment: skip {name!r} ({type(e).__name__}: {e})",
                          file=sys.stderr, flush=True)
                    ok = False
        async with completed_lock:
            completed += 1
            if ok:
                succeeded += 1
                if was_skip:
                    skipped_ok += 1
            if completed % 100 == 0:
                print(f"  Pass 3 progress: {completed}/{total} "
                      f"({succeeded} ok of which {skipped_ok} already-correct skips, "
                      f"{completed - succeeded} failed)", flush=True)
        return ok

    await asyncio.gather(*[_enrich_one(name, etype) for name, etype in sorted(seen)])
    print(f"  (of {succeeded} successes, {skipped_ok} were already-correct "
          f"skips; {succeeded - skipped_ok} required an aedit_entity write)",
          flush=True)
    return succeeded, total


# ---------------------------------------------------------------------------
# Pass 4: entity-name canonicalization (case + paraphrase dupes)
# ---------------------------------------------------------------------------


# `^User ` and ` Person$` are the two prefix/suffix patterns Pass 2 LLM
# extraction uses when paraphrasing a user reference (`User jdoe`,
# `Jdoe Person`). Whitespace inside the residue is also collapsed so
# the LLM's whitespace paraphrases (`J Doe` → `JDoe`) fold to the seed.
_USER_PREFIX_RE = re.compile(r"^User\s+")
_PERSON_SUFFIX_RE = re.compile(r"\s+Person$")


def _strip_user_paraphrase_affixes(name: str) -> str:
    """Strip the LLM's user-paraphrase prefixes/suffixes and collapse
    inner whitespace. Conservative — `^User ` requires a trailing space
    (so `UserStory` is left intact), and bare `User` / `Person` with
    nothing on the other side are not stripped.
    """
    out = _USER_PREFIX_RE.sub("", name)
    out = _PERSON_SUFFIX_RE.sub("", out)
    return re.sub(r"\s+", "", out)


def _pick_canonical_for_case_bucket(
    variants: list[str], pass1_seed_names: set[str], entity_type: str = ""
) -> str:
    """Pick the canonical form among equivalent variants.

    Priority:
      1) For tags with more than one seed: the slug spelling (see below).
      2) The Pass-1 seed if one is present in the bucket — preserves the
         authoritative entity (Discourse `username`, topic title, category
         name) regardless of case. This is the load-bearing rule: without
         it, an LLM-extracted variant like `"How To Use X"` (typed `issue`
         or `other`) can win over the Pass-1 seed `"How to use X"` (typed
         `topic`) and the merged result loses the structural type.
      3) The fully-lowercase variant if any (covers seedless buckets like
         `[ACME9, Acme9, acme9]` — pick `acme9`).
      4) Alphabetical first (deterministic tie-break).

    Rule 1 exists because rule 2 cannot break a tie it never anticipated.
    `pass1_seed_names` is derived from the live graph by structural type, so
    **every** spelling of a tag is a seed, and "is a Pass-1 seed" tells the
    picker nothing. It then returned whichever variant iteration reached first.
    Observed 2026-08-15 on the reference corpus: all ten slug nodes were deleted
    and the U+2024 display-name spellings absorbed their edges, which silently
    inverted the graph-to-SQL join contract, since `stats.py`'s `tag_label`
    column and `config.tag_label` both produce the slug.
    """
    seeds = [v for v in variants if v in pass1_seed_names]
    if seeds:
        if entity_type == "tag" and len(seeds) > 1:
            # The slug is the variant that survives separator normalization
            # unchanged. That is exactly what `config.tag_label` emits.
            slugs = sorted(v for v in seeds if v.translate(_TAG_SEPARATORS) == v)
            if slugs:
                return slugs[0]
        return seeds[0]
    lowers = sorted(v for v in variants if v == v.casefold())
    if lowers:
        return lowers[0]
    return sorted(variants)[0]


# Discourse slug separators. Folded together only for `tag`-typed nodes.
_TAG_SEPARATORS = str.maketrans({
    "․": "-",   # ONE DOT LEADER, how Discourse renders a release display name
    "‧": "-",   # HYPHENATION POINT
    "·": "-",   # MIDDLE DOT
    ".": "-",
    "_": "-",
})


def _canonical_bucket_key(nid: str, entity_type: str) -> str:
    """Key grouping entity-name variants that must collapse into one node.

    Case folding alone is not enough for tags. Discourse renders a release tag's
    *display name* as `2025․06` with U+2024 ONE DOT LEADER but slugs it
    `2025-06`, so a corpus scraped across the change in how tag nodes are named
    carries both spellings as separate nodes, each holding roughly half the
    edges. Measured on the reference corpus: `2023․06` at degree 345 sitting
    beside `2023-06` at degree 346.

    Deliberately **not** a general Unicode confusables fold, which would not fix
    it: U+2024 normalizes to `.`, yielding `2025.06`, which still is not
    `2025-06`. The equivalence that matters is Discourse's slug separators.

    Scoped to `tag` because Discourse slugs cannot contain `.` or `_`, so within
    tags these are always the same tag. Outside tags the fold would be unsafe:
    `foo.bar` and `foo-bar` can be genuinely distinct component names.
    """
    key = nid.casefold()
    if entity_type == "tag":
        key = key.translate(_TAG_SEPARATORS)
    return key


async def _canonicalize_case_dupes(rag, pass1_seed_names: set[str]) -> int:
    """Pass 4a — collapse case-fold-equivalent entity-name dupes.

    Tags additionally fold slug-separator variants; see `_canonical_bucket_key`.

    `pass1_seed_names` is the set of node IDs that originated from
    `_topic_to_custom_kg` (any `user` / `topic` / `category` / `tag`
    typed node). Used to bias canonical selection toward the Pass-1
    seed when the bucket contains one.

    Returns the number of buckets actually merged (≥2 variants).
    Idempotent — re-running on a clean graph produces 0 merges.
    """
    nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
    buckets: dict[str, list[str]] = defaultdict(list)
    types: dict[str, str] = {}
    for node in nodes:
        nid = node.get("id", "")
        if not nid:
            continue
        etype = (node.get("entity_type") or "").lower()
        types[nid] = etype
        buckets[_canonical_bucket_key(nid, etype)].append(nid)

    # Materialize the work list up-front so we can report total + ETA.
    work = [variants for variants in buckets.values() if len(variants) >= 2]
    total = len(work)
    print(f"  Pass 4a: {total} case-fold buckets to merge "
          f"({sum(len(v) - 1 for v in work)} source nodes will be collapsed)",
          flush=True)

    merged = 0
    failed = 0
    start = time.time()
    structural_lower = {t.lower() for t in STRUCTURAL_TYPE_NAMES}
    skipped_collisions = 0

    for variants in work:
        # Two DIFFERENT structural types in one bucket are two different
        # entities that merely share a name, so do not merge them at all.
        #
        # A forum can have a category `Client` and a tag `client`; Pass 1 seeds
        # both on every run. Both are Pass-1 seeds, so the picker's "prefer the
        # seed" rule cannot choose, and the merge produced one node typed
        # `category` whose description read `Forum tag: client<SEP>Forum
        # category: Client`, conflating 203 topics in the category with 13
        # carrying the tag. Eight tags were lost that way on the reference
        # corpus.
        #
        # Only *structural* types are protected. An LLM-typed variant collapsing
        # into its structural seed (`"How To Use X"` typed `issue` into the
        # `topic` seed `"How to use X"`) is exactly what this pass is for, and
        # `issue` / `other` are not structural, so those buckets still merge.
        structural_here = {types.get(v, "") for v in variants} & structural_lower
        if len(structural_here) > 1:
            skipped_collisions += 1
            print(f"  Pass 4a: skip bucket {sorted(variants)!r} — distinct "
                  f"structural types {sorted(structural_here)!r} share a name; "
                  f"merging would conflate them.", flush=True)
            continue

        # A bucket counts as a tag bucket if any member is tag-typed, so the
        # slug preference below applies even when a stray variant is typed
        # `other` by an LLM extraction.
        bucket_type = "tag" if any(types.get(v) == "tag" for v in variants) else ""
        canonical = _pick_canonical_for_case_bucket(
            variants, pass1_seed_names, bucket_type)
        sources = [v for v in variants if v != canonical]
        # Lock the canonical's type explicitly. Without target_entity_data
        # the default `keep_first` strategy could promote a source's type
        # over the canonical's (the iteration order is a LightRAG
        # implementation detail we don't want to depend on).
        canonical_type = types.get(canonical, "")
        try:
            await rag.amerge_entities(
                source_entities=sources,
                target_entity=canonical,
                target_entity_data={"entity_type": canonical_type} if canonical_type else None,
            )
            merged += 1
        except Exception as e:
            failed += 1
            print(
                f"  Pass 4a: skip bucket {canonical!r} "
                f"({type(e).__name__}: {e})",
                file=sys.stderr,
                flush=True,
            )
        # Per-merge progress is dominated by edge-rerouting on hub buckets;
        # report every 10 merges so the user can see the run is alive even
        # when individual merges take 30+ seconds.
        done = merged + failed
        if done % 10 == 0 or done == total:
            print(_progress_line("Pass 4a", done, total, time.time() - start,
                                 ok=merged, failed=failed), flush=True)
    return merged


@contextmanager
def _defer_pass4_writes(rag):
    """Defer all VDB writes during Pass 4 — upserts and deletes — into
    in-memory buffers that the post-Pass-4 apply step flushes in bulk.
    Per-merge work becomes pure NetworkX graph mutations, eliminating the
    per-merge Faiss `_remove_faiss_ids` O(index_size) churn that dominates
    wall-clock on hub buckets.

    Buffered ops:
      - `entities_vdb.upsert`     → `ent_upserts`  (vdb_id → payload)
      - `entities_vdb.delete`     → `ent_deletes`  (set of vdb_ids)
      - `relationships_vdb.upsert`→ `rel_upserts`  (vdb_id → payload)
      - `relationships_vdb.delete`→ `rel_deletes`  (set of vdb_ids)

    Note: `embedding_func` is intentionally NOT replaced. The only callers
    of `embedding_func` inside `amerge_entities` are the VDB upserts
    themselves (Faiss embeds `content` at upsert time). Since both upserts
    are buffered, no embedding call is reached during the merge phase.
    Earlier versions stubbed `embedding_func.func` to a zero-vector
    fallback, but doing so corrupted LightRAG's internal embedding worker
    pool — the pool initialized lazily during the apply phase against a
    stale func reference and the first real OpenAI batch hung past the
    60s worker timeout. Leaving the func untouched fixes the hang.

    Conflict handling: a buffered upsert that's later deleted gets popped
    from the upsert buffer (LightRAG does this naturally — e.g. an edge
    rerouted then later folded). At apply time, deletes happen first to
    free Faiss IDs; upserts follow with real embeddings.

    Net impact on the canonical 1.3K-topic corpus: per-merge ~9.5s →
    ~50ms; total Pass 4 wall time ~100min → ~1-2min (plus the bulk
    apply pass, which Faiss internal-batches at ~10 contents per OpenAI
    call → ~5-10 min for the edge VDB on a 14k-edge reroute set).
    """
    ent_upserts: dict[str, dict] = {}
    rel_upserts: dict[str, dict] = {}
    ent_deletes: set[str] = set()
    rel_deletes: set[str] = set()

    real_ent_upsert = rag.entities_vdb.upsert
    real_ent_delete = rag.entities_vdb.delete
    real_rel_upsert = rag.relationships_vdb.upsert
    real_rel_delete = rag.relationships_vdb.delete

    async def _buffered_ent_upsert(payload):
        # Latest write wins per id; ent_deletes for this id is implicitly
        # superseded.
        ent_upserts.update(payload)
        for k in payload:
            ent_deletes.discard(k)

    async def _buffered_rel_upsert(payload):
        rel_upserts.update(payload)
        for k in payload:
            rel_deletes.discard(k)

    async def _buffered_ent_delete(ids):
        for i in ids:
            ent_deletes.add(i)
            ent_upserts.pop(i, None)

    async def _buffered_rel_delete(ids):
        for i in ids:
            rel_deletes.add(i)
            rel_upserts.pop(i, None)

    rag.entities_vdb.upsert = _buffered_ent_upsert
    rag.entities_vdb.delete = _buffered_ent_delete
    rag.relationships_vdb.upsert = _buffered_rel_upsert
    rag.relationships_vdb.delete = _buffered_rel_delete
    try:
        yield ent_upserts, rel_upserts, ent_deletes, rel_deletes
    finally:
        rag.entities_vdb.upsert = real_ent_upsert
        rag.entities_vdb.delete = real_ent_delete
        rag.relationships_vdb.upsert = real_rel_upsert
        rag.relationships_vdb.delete = real_rel_delete


async def _apply_pass4_writes(
    rag,
    ent_upserts: dict[str, dict],
    rel_upserts: dict[str, dict],
    ent_deletes: set[str],
    rel_deletes: set[str],
) -> dict[str, int]:
    """Flush the buffers from `_defer_pass4_writes` to the real VDBs.

    Order: deletes first (frees Faiss IDs so upserts don't trip over
    stale ids), then upserts. The upserts run through the (real,
    restored) `embedding_func`, which Faiss's `upsert` internal-batches
    at `_max_batch_size` (~32 contents per OpenAI call). For the canonical
    corpus this is ~30 OpenAI calls total instead of ~3000-6000 per-merge
    calls in the unbuffered path.

    Returns a dict of operation counts for logging.
    """
    counts = {
        "ent_deleted": len(ent_deletes),
        "rel_deleted": len(rel_deletes),
        "ent_upserted": len(ent_upserts),
        "rel_upserted": len(rel_upserts),
    }
    if ent_deletes:
        print(f"  Pass 4 apply: deleting {len(ent_deletes)} stale entity vectors...",
              flush=True)
        await rag.entities_vdb.delete(list(ent_deletes))
    if rel_deletes:
        print(f"  Pass 4 apply: deleting {len(rel_deletes)} stale edge vectors...",
              flush=True)
        await rag.relationships_vdb.delete(list(rel_deletes))
    if ent_upserts:
        print(f"  Pass 4 apply: re-embedding {len(ent_upserts)} entities "
              f"(Faiss internal-batches at {getattr(rag.entities_vdb, '_max_batch_size', '?')} per OpenAI call)...",
              flush=True)
        await rag.entities_vdb.upsert(ent_upserts)
    if rel_upserts:
        print(f"  Pass 4 apply: re-embedding {len(rel_upserts)} edges...",
              flush=True)
        await rag.relationships_vdb.upsert(rel_upserts)
    return counts


async def _canonicalize_user_paraphrases(
    rag, user_seed_names: set[str]
) -> int:
    """Pass 4b — collapse `^User ` / ` Person$` / whitespace paraphrases
    of `user`-typed Pass-1 seeds.

    Conditional strip: a node is only merged if its stripped + casefolded
    form lands on an entry in `user_seed_names`. This prevents false
    positives like `User Story` → `Story` when no `story` user seed exists.

    Returns the number of buckets actually merged.
    """
    if not user_seed_names:
        return 0

    seeds_lc = {n.casefold() for n in user_seed_names}
    seed_lc_to_canonical = {n.casefold(): n for n in user_seed_names}

    nodes = await rag.chunk_entity_relation_graph.get_all_nodes()
    target_to_sources: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        nid = node.get("id", "")
        if not nid:
            continue
        if nid in user_seed_names:
            continue  # the seed itself — don't try to merge it into itself
        stripped = _strip_user_paraphrase_affixes(nid)
        if stripped == nid.replace(" ", ""):
            # No paraphrase prefix/suffix removed AND no inner whitespace —
            # nothing for this pass to do. (Pass 4a already handled pure
            # case dupes.)
            if not (_USER_PREFIX_RE.search(nid) or _PERSON_SUFFIX_RE.search(nid) or " " in nid):
                continue
        key = stripped.casefold()
        if key in seeds_lc:
            target_to_sources[seed_lc_to_canonical[key]].append(nid)

    work = [(t, s) for t, s in target_to_sources.items() if s]
    total = len(work)
    print(f"  Pass 4b: {total} user-paraphrase buckets to merge "
          f"({sum(len(s) for _, s in work)} source nodes will be collapsed)",
          flush=True)

    merged = 0
    failed = 0
    start = time.time()
    for target, sources in work:
        try:
            await rag.amerge_entities(
                source_entities=sources,
                target_entity=target,
                target_entity_data={"entity_type": "user"},
            )
            merged += 1
        except Exception as e:
            failed += 1
            print(
                f"  Pass 4b: skip target {target!r} "
                f"({type(e).__name__}: {e})",
                file=sys.stderr,
                flush=True,
            )
        done = merged + failed
        if done % 10 == 0 or done == total:
            print(_progress_line("Pass 4b", done, total, time.time() - start,
                                 ok=merged, failed=failed), flush=True)
    return merged


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


async def index_topics(
    rc: RuntimeConfig,
    clear: bool = False,
    extraction_model: str = None,
    gleaning: int | None = None,
    limit: int | None = None,
    llm_model_max_async: int | None = None,
    max_parallel_insert: int | None = None,
    enrich_only: bool = False,
    canonicalize_only: bool = False,
):
    """Build the knowledge graph from all scraped topics.

    `gleaning` overrides rc.gleaning for this run only. `limit` restricts
    to the first N topic JSON files (after sort) — useful for sample-size
    validation runs. None = process all topics.

    `llm_model_max_async` / `max_parallel_insert` override the per-run
    concurrency knobs (see `_get_rag`). None = fall through to env + defaults.

    `enrich_only=True` skips Pass 1 (custom_kg) and Pass 2 (LLM extraction)
    and runs only Pass 3 (`_enrich_structural_types`) against the existing
    graph. Use it to refresh structural-entity types/embeddings after Pass 3
    timeouts without paying Pass 1's re-upsert cost. Incompatible with
    `clear=True` (enrichment needs an existing graph to work against).

    `canonicalize_only=True` skips Passes 1-3 and runs only Pass 4
    (`_canonicalize_case_dupes` + `_canonicalize_user_paraphrases`) against
    the existing graph. Use it to collapse case + paraphrase entity-name
    dupes (`jdoe` / `Jdoe` / `JDoe` / `User Jdoe`) without re-indexing.
    Zero LLM cost. Destructive — `amerge_entities` rewrites the graph in
    place; back up `<data-dir>/graphrag/` first. Incompatible with
    `clear=True` and with `enrich_only=True`.
    """
    paths = rc.paths()
    topics_dir = paths.topics_dir
    graph_dir = paths.graphrag_dir

    if not topics_dir.exists():
        print(f"Error: No scraped data found at {topics_dir}")
        print("  Run the scraper first.")
        sys.exit(1)

    if enrich_only and clear:
        print("Error: --enrich-only is incompatible with --clear (needs an existing graph).")
        sys.exit(1)
    if enrich_only and not graph_dir.exists():
        print(f"Error: --enrich-only needs an existing graph at {graph_dir}.")
        print("  Run `--index` (without --enrich-only) first to build it.")
        sys.exit(1)
    if canonicalize_only and clear:
        print("Error: --canonicalize-only is incompatible with --clear (needs an existing graph).")
        sys.exit(1)
    if canonicalize_only and enrich_only:
        print("Error: --canonicalize-only and --enrich-only are mutually exclusive.")
        sys.exit(1)
    if canonicalize_only and not graph_dir.exists():
        print(f"Error: --canonicalize-only needs an existing graph at {graph_dir}.")
        print("  Run `--index` first to build it.")
        sys.exit(1)

    model = extraction_model or rc.default_extraction_model()

    # NOT gated on `graph_dir.exists()`: a run killed between the rmtree and the
    # mkdir leaves no graph dir but a cache stash beside it, and skipping the
    # call in exactly that state is what stranded the stash with no recovery
    # path. `_clear_graph_dir` handles the missing-dir case itself.
    if clear:
        print(f"Clearing existing graph at {graph_dir}...")
        if _clear_graph_dir(graph_dir, model):
            print(f"  Kept {LLM_CACHE_FILE} (same extraction model {model!r}) — "
                  f"unchanged chunks will hit cache instead of re-billing the API.")
    elif not (canonicalize_only or enrich_only):
        # Only stamp/validate provenance on paths that actually extract.
        # `--canonicalize-only` and `--enrich-only` make no completion calls, so
        # they must not judge a cache against their own (possibly defaulted)
        # model — doing so would delete a valid cache built by an explicit
        # `--extraction-model` and force a full re-bill on the next real run.
        _ensure_cache_provenance(graph_dir, model)

    gleaning_passes = rc.gleaning if gleaning is None else gleaning
    provider = "OpenAI" if rc.is_openai else "Ollama"
    topic_files = sorted(topics_dir.glob("*.json"))
    total_files = len(topic_files)
    if limit is not None and limit > 0:
        topic_files = topic_files[:limit]
        print(f"(--limit {limit}: processing first {len(topic_files)} of {total_files} topics)")

    print(f"Indexing {len(topic_files)} topics into LightRAG...")
    print(f"  Provider: {provider}")
    print(f"  LLM: {model}")
    print(f"  Gleaning passes: {gleaning_passes}  "
          f"(0=cheap/baseline recall, 1=recommended, 2+=diminishing returns)")
    if rc.is_openai:
        print(f"  Embeddings: OpenAI/{rc.openai_embed_model} ({rc.openai_embed_dim}d)  "
              f"(override via OPENAI_EMBED_MODEL in <data-dir>/config/.env; "
              f"OPENAI_EMBED_DIM for non-standard models)")
        print(f"  Concurrency: 8 parallel requests")
    else:
        print(f"  Embeddings: Ollama/{rc.embed_model} ({rc.ollama_embed_dim}d)  "
              f"(override via EMBED_MODEL / OLLAMA_EMBED_DIM in <data-dir>/config/.env)")
        print(f"  Concurrency: 1 (local GPU)")
    print(f"  Graph dir: {graph_dir}")
    print()

    topics = []
    for tf in topic_files:
        try:
            topics.append(json.loads(tf.read_text()))
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Skipping {tf.name}: {e}")

    print(f"Preparing ingest of {len(topics)} topics...", flush=True)

    rag = _get_rag(
        rc, model_name=model, gleaning=gleaning_passes,
        llm_model_max_async=llm_model_max_async,
        max_parallel_insert=max_parallel_insert,
    )
    await rag.initialize_storages()

    # Batch disk persistence: only flush every PERSIST_EVERY documents
    # instead of after each one (default LightRAG behaviour). Covers the
    # `self._insert_done` code path used by `ainsert` (Pass 2, `lightrag.py:1346`)
    # and `ainsert_custom_kg` (Pass 1, `lightrag.py:2553`). Does NOT cover
    # `aedit_entity`'s direct `_persist_graph_updates` calls — Pass 3 uses the
    # `_suppress_index_done` context manager below instead. Pass 1 suppresses
    # this path entirely (it has no LLM cost, so mid-pass durability buys
    # nothing) and flushes once at its phase boundary.
    _persist_counter = 0
    # Whether the counter-driven flush is currently disabled. Pass 1 sets it for
    # the whole pass and checkpoints on the topic index instead. Held in a dict
    # so the nested flush closure reads current state without `nonlocal`.
    _flush_state = {"suppressed": False}

    def _reset_persist_counter():
        """Zero the flush counter at a pass boundary.

        The counter is shared by every `_insert_done` caller, so without this
        Pass 1's ~1400 suppressed calls would leave it mid-cycle and Pass 2's
        first real flush would land at an arbitrary offset rather than after
        PERSIST_EVERY documents.
        """
        nonlocal _persist_counter
        _persist_counter = 0

    # Set by `_defer_ledger_flush` while doc_status's own callback is a no-op, so
    # the ordered flush can still reach the real writer. None outside that window.
    _deferred_ledger_flush = {"fn": None}

    async def _ordered_insert_done(pipeline_status=None, pipeline_status_lock=None):
        await _flush_ledger_last(rag, pipeline_status, pipeline_status_lock,
                                 ledger_flush=_deferred_ledger_flush["fn"])

    async def _batched_insert_done(pipeline_status=None, pipeline_status_lock=None):
        nonlocal _persist_counter
        _persist_counter += 1
        if _should_flush(_persist_counter, PERSIST_EVERY,
                         _flush_state["suppressed"]):
            await _ordered_insert_done(pipeline_status, pipeline_status_lock)
        else:
            # Log that we skipped the flush
            logging.getLogger("lightrag").debug(
                "Skipping disk persist (%d/%d until next flush)",
                _persist_counter % PERSIST_EVERY, PERSIST_EVERY,
            )

    rag._insert_done = _batched_insert_done

    try:
        if canonicalize_only:
            # Canonicalize-only mode: skip Passes 1-3 entirely, drop straight
            # to Pass 4. Useful as a one-shot cleanup against an existing
            # graph; no LLM cost.
            print("Canonicalize-only mode: skipping Passes 1-3.", flush=True)
        elif enrich_only:
            # Enrich-only mode: skip Pass 1 (custom_kg seeds) and Pass 2 (LLM
            # extraction), drop straight to Pass 3. Useful when a prior run
            # completed structurally but some `aedit_entity` calls hit embedding
            # timeouts, leaving those entities with stale VDB embeddings (types
            # are already correct in the graph — only the embeddings are stale).
            # Cost: ~$0.02 + a few minutes at `llm_model_max_async` concurrency.
            print("Enrich-only mode: skipping Pass 1 + Pass 2.", flush=True)
        else:
            # Pass 1: structural KG (deterministic, no LLM) — seeds Category/Topic/Tag/User
            # nodes and their edges from the scraped JSON before the LLM runs. Each
            # topic is wrapped in a retry loop so a single transient failure (timeout,
            # rate limit) doesn't abort the entire run — we log and move on.
            print(f"Pass 1: Inserting structural KG for {len(topics)} topics...", flush=True)
            pass1_ok = 0
            pass1_failed: list = []
            _pass1_start = time.time()
            # Pass 1 makes no *completion* calls, but it is not free: every Faiss
            # upsert embeds inline (`kg/faiss_impl.py` calls
            # `self.embedding_func(batch)`), so a full pass is ~15.6K vectors /
            # ~1.2M embedding tokens of real spend.
            #
            # Checkpoint every PASS1_CHECKPOINT_EVERY topics rather than once at
            # the phase boundary. The boundary-only version was faster but
            # uncheckpointed, and a SIGKILL — which no `finally` can catch — then
            # costs the whole pass. The `finally` below still flushes the tail for
            # catchable exits.
            #
            # The checkpoint runs off the topic index, and LightRAG's own
            # `_insert_done` is suppressed for the whole pass. Driving it off the
            # `_insert_done` counter instead would decouple it from the ledger:
            # that counter only advances when a topic is actually inserted, so a
            # resume that skipped 1,314 of 1,399 topics never reached the
            # interval and flushed nothing at all, while the ledger below saved
            # on schedule regardless.
            _reset_persist_counter()
            _flush_state["suppressed"] = True
            # Skip topics whose structural payload is byte-identical to the one
            # seeded last run. On a `--resume` against a populated graph each
            # insert merges against every existing node and each Faiss upsert of
            # a present id takes the remove-then-re-add path — measured at 10.6
            # topics/min on a 15,756-node graph versus 63/min on an empty one, so
            # a refresh spent ~2 hours re-seeding nodes that already existed.
            # Keyed on the payload, so a change to how nodes are built
            # invalidates every affected topic and still propagates corpus-wide.
            _pass1_hashes = _load_pass1_hashes(graph_dir)
            _pass1_seen = dict(_pass1_hashes)
            pass1_skipped = 0
            pass1_purged = 0
            pass1_retracted = 0
            pass1_backfilled = 0
            # Ledger-only changes. They describe state that is already
            # durable, so they need no storage flush — which is what keeps
            # a no-change resume from rewriting ~500MB of Faiss.
            _pass1_ledger_dirty = False
            pass1_dropped = 0
            # True iff an insert has landed since the last checkpoint. Gates the
            # checkpoint so a resume that skips everything writes nothing:
            # `FaissVectorDBStorage.index_done_callback` has no dirty guard and
            # rewrites its full ~500MB index whether or not anything changed.
            _pass1_dirty = False

            async def _pass1_checkpoint() -> None:
                nonlocal _pass1_dirty, _pass1_ledger_dirty
                _pass1_dirty = await _checkpoint_pass1(
                    rag, graph_dir, _pass1_seen, _pass1_dirty,
                    ledger_only=_pass1_ledger_dirty)
                _pass1_ledger_dirty = False

            try:
                for idx, topic in enumerate(topics, start=1):
                    tid = topic.get("id", "?")
                    # Building the payload is fallible and must not abort the
                    # pass: `_topic_to_custom_kg` raises KeyError on a topic with
                    # no `id`, and `_pass1_payload_hash` raises UnicodeEncodeError
                    # on a lone surrogate (which `json.loads` happily produces
                    # from a `\ud800` escape, and nothing strips this early).
                    # One malformed file out of thousands should cost that topic,
                    # not the whole multi-hour run.
                    action, purge_ids = Pass1Action.INSERT, []
                    try:
                        payload = _topic_to_custom_kg(topic)
                        payload_hash = _pass1_payload_hash(payload)
                        action, purge_ids = _pass1_plan(
                            _pass1_hashes.get(str(tid)), payload_hash)
                    except Exception as e:
                        print(f"  Topic id={tid} (idx {idx}/{len(topics)}): "
                              f"malformed, skipping: {type(e).__name__}: {e}",
                              flush=True)
                        pass1_failed.append(tid)
                        payload = payload_hash = None

                    if payload is None:
                        pass  # already counted as failed above
                    elif action is Pass1Action.SKIP:
                        pass1_skipped += 1
                        # The hash matched, so this payload *is* what the graph
                        # holds. An entry written before `docs`/`rels` existed
                        # can therefore be completed from it without re-seeding
                        # anything. Without this, a corpus that never changes
                        # never migrates, and the first edit to each topic gets
                        # the degraded v1 path.
                        entry = _pass1_seen.get(str(tid)) or {}
                        if not entry.get("docs") or not entry.get("rels"):
                            _pass1_seen[str(tid)] = {
                                "hash": payload_hash,
                                "docs": [*_pass1_doc_ids(payload),
                                         _pass2_doc_id(topic)],
                                "rels": _pass1_rel_pairs(payload),
                            }
                            pass1_backfilled += 1
                            _pass1_ledger_dirty = True
                    else:
                        if action is Pass1Action.RESEED:
                            # The topic changed. Its prior documents must go
                            # first, or Pass 1 merges the new payload *beside*
                            # the old nodes instead of replacing them — which is
                            # how a renamed tag and two departed users were left
                            # stranded in the graph (found 2026-08-15).
                            #
                            # A v1 ledger recorded no ids. `topic-<id>` is
                            # derivable and covers the common single-chunk
                            # topic; overflow chunks and the Pass 2 document are
                            # not recoverable, so those stay until the topic
                            # changes again under a v2 entry.
                            if not purge_ids:
                                purge_ids = [f"topic-{tid}"]
                                print(f"  Topic id={tid}: v1 ledger entry, "
                                      f"purging {purge_ids[0]!r} only.", flush=True)
                            pass1_purged += await _purge_prior_docs(
                                rag, purge_ids, tid)
                            # The document purge cannot reach Pass 1's own
                            # nodes, so retract the structural claims directly
                            # from what the ledger recorded.
                            stale = _stale_structural_relations(
                                _pass1_hashes.get(str(tid)), payload)
                            if stale:
                                e, n = await _purge_stale_structure(rag, stale, tid)
                                pass1_retracted += e
                                pass1_dropped += n
                            _pass1_dirty = True
                        for attempt in range(3):
                            try:
                                await rag.ainsert_custom_kg(payload)
                                pass1_ok += 1
                                # Record only after the insert actually lands, so an
                                # interrupted run re-does rather than skips.
                                # Both id families, because both accrete: Pass 1's
                                # chunk docs and the document Pass 2 will insert.
                                _pass1_seen[str(tid)] = {
                                    "hash": payload_hash,
                                    "docs": [*_pass1_doc_ids(payload),
                                             _pass2_doc_id(topic)],
                                    "rels": _pass1_rel_pairs(payload),
                                }
                                _pass1_dirty = True
                                break
                            except Exception as e:
                                if attempt == 2:
                                    print(f"  Topic id={tid} (idx {idx}/{len(topics)}): "
                                          f"failed after 3 attempts: {type(e).__name__}: {e}",
                                          flush=True)
                                    pass1_failed.append(tid)
                                else:
                                    wait = 2 ** attempt
                                    print(f"  Topic id={tid} attempt {attempt+1}/3 failed "
                                          f"({type(e).__name__}), retrying in {wait}s...",
                                          flush=True)
                                    await asyncio.sleep(wait)
                    if idx % PASS1_CHECKPOINT_EVERY == 0:
                        await _pass1_checkpoint()
                    if idx % 100 == 0 or idx == len(topics):
                        print(_progress_line(
                            "Pass 1", idx, len(topics),
                            time.time() - _pass1_start,
                            ok=pass1_ok, failed=len(pass1_failed),
                            skipped=pass1_skipped),
                            flush=True)
            finally:
                # Flush whatever accumulated since the last checkpoint. Runs on
                # KeyboardInterrupt / CancelledError too; a SIGKILL skips it, which
                # is why the pass checkpoints as it goes rather than relying on this.
                _flush_state["suppressed"] = False
                print("Pass 1: flushing remaining state to disk...", flush=True)
                await _pass1_checkpoint()
                _reset_persist_counter()
            print(f"Pass 1 complete: {pass1_ok}/{len(topics)} seeded, "
                  f"{pass1_skipped} unchanged (skipped), "
                  f"{pass1_purged} stale doc(s) purged, "
                  f"{pass1_retracted} stale edge(s) retracted, "
                  f"{pass1_dropped} orphan(s) dropped, "
                  f"{pass1_backfilled} ledger entr(ies) backfilled, "
                  f"{len(pass1_failed)} failed "
                  f"({time.time() - _pass1_start:.0f}s).", flush=True)
            if pass1_failed:
                shown = pass1_failed[:20]
                more = f" (+{len(pass1_failed) - 20} more)" if len(pass1_failed) > 20 else ""
                print(f"  Failed topic ids: {shown}{more}", flush=True)

            # Pass 2: LLM extraction over post bodies. Content-level entities merge
            # with the structural seeds by name under LightRAG's standard merge logic.
            # LightRAG handles per-document errors internally via its doc_status tracker,
            # so a single bad document won't abort the batch.
            # `file_paths` carries source provenance through extraction so entity
            # source_ids trace back to `topic-<id>.json` instead of `unknown_source`,
            # aligning with the chunk file_paths set in _topic_to_custom_kg.
            print(f"Pass 2: LLM extraction over {len(topics)} topic bodies...", flush=True)
            documents = [topic_to_document(t) for t in topics]
            file_paths = [f"topic-{t['id']}.json" for t in topics]
            # Suppress doc_status's per-upsert self-flush for the pass, so the
            # batched ordered flush is the only thing that writes the ledger.
            # Without this the ledger is durable per-document, ahead of the
            # entities each document produced, and the ordering below buys
            # nothing. See `_defer_ledger_flush`.
            _ledger = getattr(rag, _PROGRESS_LEDGER_ATTR, None)
            with _defer_ledger_flush(_ledger) if _ledger is not None else nullcontext(None) as _real:
                _deferred_ledger_flush["fn"] = _real
                try:
                    await rag.ainsert(documents, file_paths=file_paths)
                    # Final flush for any remaining unpersisted state. Ordered so
                    # the doc_status ledger is written after the state it describes.
                    await _ordered_insert_done()
                finally:
                    _deferred_ledger_flush["fn"] = None

        if not canonicalize_only:
            # Pass 3 (structural enrichment): Pass 2's LLM extraction may have
            # produced entities sharing names with Pass 1's structural nodes
            # (category names / topic titles frequently appear in post prose).
            # LightRAG's merge uses a Counter vote that tie-breaks in favor of the
            # incoming batch, so Pass 2 consistently overwrites Pass 1's type.
            # This final pass re-asserts the structural type via `aedit_entity`,
            # which is a direct write (no vote). See docs/lightrag/LIGHTRAG_KNOWHOW.md
            # #18 and #19 for the investigation trail.
            # Resolve Pass 3 concurrency the same way Pass 2's LLM concurrency
            # resolved above (CLI override > env > provider default). aedit_entity
            # internally hits the embedding API, which shares the OpenAI tier
            # budget with the extraction calls, so the same cap applies.
            _pass3_async = llm_model_max_async if llm_model_max_async is not None else rc.llm_model_max_async
            _pass3_resolved = _pass3_async or (8 if rc.is_openai else 1)
            print(f"Pass 3: Re-asserting structural entity types "
                  f"(concurrency={_pass3_resolved})...", flush=True)

            # Suppress per-edit `index_done_callback` during Pass 3 so concurrent
            # `aedit_entity` workers don't serialize on the shared storage lock
            # (each flush was a full ~20MB graphml write under lock, converting
            # concurrency=13 into single-threaded throughput). One explicit
            # flush at the end captures all accumulated mutations.
            _pass3_stores = _pass3_storages(rag)
            with _suppress_index_done(_pass3_stores):
                enriched, attempted = await _enrich_structural_types(
                    rag, topics,
                    concurrency=_pass3_resolved,
                    # --enrich-only forces re-embed even when the graph type is
                    # already correct, because that's the whole point of the
                    # flag: refresh Faiss embeddings after prior Pass 3 timeouts
                    # (where the type-write succeeded but the re-embed failed).
                    force_rewrite=enrich_only,
                )
            await _flush_storages(_pass3_stores)
            print(f"Pass 3 complete: {enriched}/{attempted} structural entities "
                  f"re-asserted to deterministic types.", flush=True)

        # Pass 4 (entity-name canonicalization): collapse case-fold dupes
        # (`jdoe` / `Jdoe` / `JDoe`) and `^User ` / ` Person$` paraphrases of
        # known user seeds. LightRAG keys nodes by exact name string, so this
        # axis is invisible to Pass 3's type-axis fix.
        # Embedding-cache trick: amerge_entities re-embeds the merged target
        # AND every rerouted edge — ~3000-6000 OpenAI calls on a 1.3K-topic
        # corpus, dominating wall clock. We stub the embedding func to zeros
        # for the merge phase, track which entries got placeholders, and
        # refresh them in one bulk OpenAI batch afterward (Faiss internal
        # batching keeps it to ~20 actual API calls). Net: ~7h → ~2 min.
        # Skipped when --enrich-only is set (that mode targets only the type
        # re-assertion, not the broader name-merge step).
        if not enrich_only:
            print("Pass 4: Canonicalizing entity-name dupes (case + paraphrase)...",
                  flush=True)
            # Identify Pass-1 seeds from the live graph by structural type —
            # same vocabulary `_topic_to_custom_kg` wrote in Pass 1 and Pass 3
            # re-asserted. Avoids re-walking the topic JSON.
            from discourse_explorer.config import STRUCTURAL_TYPE_NAMES
            structural_lower = {t.lower() for t in STRUCTURAL_TYPE_NAMES}
            all_nodes_for_seeds = await rag.chunk_entity_relation_graph.get_all_nodes()
            pass1_seed_names = {
                n.get("id", "")
                for n in all_nodes_for_seeds
                if (n.get("entity_type") or "").lower() in structural_lower
            }
            pass1_seed_names.discard("")
            user_seed_names = {
                n.get("id", "")
                for n in all_nodes_for_seeds
                if (n.get("entity_type") or "").lower() == "user"
            }
            user_seed_names.discard("")

            # Same suppression pattern as Pass 3 — `amerge_entities` flushes
            # graphml + VDBs per call, which would otherwise serialize behind
            # the storage lock and dominate wall-clock on hundreds of merges.
            _pass4_stores = _pass3_storages(rag)
            with _suppress_index_done(_pass4_stores), \
                    _defer_pass4_writes(rag) as (ent_ups, rel_ups, ent_dels, rel_dels):
                merged_case = await _canonicalize_case_dupes(rag, pass1_seed_names)
                merged_para = await _canonicalize_user_paraphrases(rag, user_seed_names)
            print(f"Pass 4 merges complete: {merged_case} case-fold + "
                  f"{merged_para} user-paraphrase buckets "
                  f"(buffered: {len(ent_ups)} ent-upserts, {len(rel_ups)} rel-upserts, "
                  f"{len(ent_dels)} ent-deletes, {len(rel_dels)} rel-deletes).",
                  flush=True)
            # Apply the buffered VDB writes in bulk (real embeddings).
            counts = await _apply_pass4_writes(
                rag, ent_ups, rel_ups, ent_dels, rel_dels,
            )
            print(f"Pass 4 apply complete: {counts}", flush=True)
            await _flush_storages(_pass4_stores)
            print(f"Pass 4 complete (graphml + VDBs flushed to disk).", flush=True)
    finally:
        await rag.finalize_storages()

    # Post-index verification: did we actually get every topic into the graph?
    try:
        import networkx as nx
        graphml_path = paths.graphml_file
        if graphml_path.exists():
            G = nx.read_graphml(graphml_path)
            types = [d.get("entity_type", "").lower() for _, d in G.nodes(data=True)]
            n_topic = sum(1 for t in types if t == "topic")
            n_cat = sum(1 for t in types if t == "category")
            n_tag = sum(1 for t in types if t == "tag")
            n_user = sum(1 for t in types if t == "user")
            print(f"\nPost-index verification")
            print(f"  Topics in graph    : {n_topic} (source: {len(topics)})")
            print(f"  Categories in graph: {n_cat}")
            print(f"  Tags in graph      : {n_tag}")
            print(f"  Users in graph     : {n_user}")
            print(f"  Total nodes        : {G.number_of_nodes()}")
            print(f"  Total edges        : {G.number_of_edges()}")
            if n_topic < len(topics):
                print(f"  WARNING: {len(topics) - n_topic} topics missing from graph "
                      f"(indexing may be incomplete)")
    except Exception as e:
        print(f"  (Post-index verification failed: {type(e).__name__}: {e})")

    print(f"\nIndexing complete. Graph stored in: {graph_dir}")


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------


async def query_graph(
    rc: RuntimeConfig,
    question: str,
    mode: str = "mix",
    query_model: str = None,
    extraction_model: str = None,
):
    """Query the knowledge graph."""
    from lightrag import QueryParam

    paths = rc.paths()

    if not paths.graphml_file.exists():
        print("Error: Knowledge graph not built yet. Run with --index first.")
        sys.exit(1)

    rag = _get_rag(rc, model_name=extraction_model)
    await rag.initialize_storages()

    param = QueryParam(mode=mode)

    # QueryParam.enable_rerank defaults to True. LightRAG emits a warning on
    # every query when rerank is enabled but no `rerank_model_func` is wired
    # in. Flip it off when the user hasn't configured `RERANK_PROVIDER` — keeps
    # logs clean without changing retrieval quality (rerank was no-op anyway).
    if not rc.rerank_provider:
        param.enable_rerank = False

    # Override query-time model if different from extraction model
    emodel = extraction_model or rc.default_extraction_model()
    qmodel = query_model or rc.query_model or emodel
    if qmodel != emodel:
        if rc.is_openai:
            from lightrag.llm.openai import openai_complete_if_cache as _oai_complete

            async def _query_llm(prompt, system_prompt=None, history_messages=[], **kwargs):
                return await _oai_complete(
                    qmodel, prompt, system_prompt=system_prompt,
                    history_messages=history_messages, **kwargs,
                )
        else:
            from lightrag.llm.ollama import ollama_model_complete as _ollama_complete

            async def _query_llm(prompt, system_prompt=None, history_messages=[], **kwargs):
                if "hashing_kv" in kwargs and hasattr(kwargs["hashing_kv"], "global_config"):
                    kwargs["hashing_kv"].global_config["llm_model_name"] = qmodel
                return await _ollama_complete(
                    prompt, system_prompt=system_prompt,
                    history_messages=history_messages, **kwargs,
                )

        param.model_func = _query_llm

    provider = "OpenAI" if rc.is_openai else "Ollama"
    print(f"Querying ({mode} mode) via {provider}/{qmodel}...")
    print(f"  Q: {question}")
    print()

    try:
        result = await rag.aquery(question, param=param)
        print(result)
    finally:
        await rag.finalize_storages()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Query scraped Discourse data using LightRAG."
    )
    parser.add_argument(
        "path", nargs="?", default=None, type=Path,
        help="Path to scraped data directory. Falls back to DISCOURSE_DATA_DIR in the project-root .env.",
    )
    parser.add_argument(
        "question", nargs="?", default=None,
        help="Question to ask the knowledge graph.",
    )
    parser.add_argument(
        "--index", action="store_true",
        help="Build/rebuild the knowledge graph from scraped JSON.",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Wipe the existing graph and rebuild from scratch (use with --index).",
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Use local search mode (shorthand for --mode local).",
    )
    parser.add_argument(
        "--mode", default=None,
        choices=["local", "global", "hybrid", "mix", "naive"],
        help="Search mode (default: mix). local=entity-focused, global=synthesis, "
             "hybrid=local+global, mix=KG+vector, naive=basic vector search.",
    )
    parser.add_argument(
        "--extraction-model", default=None,
        help="LLM for entity extraction during --index. Default: EXTRACTION_MODEL from <data-dir>/config/.env, "
             "or the provider's built-in default (OpenAI: gpt-4.1-mini; Ollama: qwen2.5:14b). "
             "Examples: gpt-4o-mini, gpt-4.1-mini, gpt-5-mini, gpt-5.2, llama3.3:70b. "
             "Avoid gpt-5-series for indexing — reasoning overhead makes the extraction "
             "cascade ~5× slower for constrained-vocab classification.",
    )
    parser.add_argument(
        "--query-model", default=None,
        help="LLM for queries. Default: QUERY_MODEL from <data-dir>/config/.env, else same as extraction model. "
             "Useful for indexing with a strong model and querying with a cheaper one.",
    )
    parser.add_argument(
        "--gleaning", type=int, default=None, metavar="N",
        help="Entity-extraction gleaning passes during --index. "
             "0=extract once (cheap, baseline recall), "
             "1=one extra 'what did you miss?' pass (recommended, ~+50%% cost, meaningfully higher recall), "
             "2+=diminishing returns. Default: GLEANING from <data-dir>/config/.env (1 if unset).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Sample-run cap: process only the first N topics (after sort). "
             "Useful for validating config changes before committing to a full run. "
             "Default: all topics.",
    )
    parser.add_argument(
        "--llm-concurrency", type=int, default=None, metavar="N",
        help="LightRAG llm_model_max_async (parallel LLM calls) during --index. "
             "Default: LLM_MODEL_MAX_ASYNC from <data-dir>/config/.env; falls through "
             "to 8 (OpenAI) / 1 (Ollama). Tier 3 OpenAI accounts can safely use ~13 "
             "per `--detect-limits`.",
    )
    parser.add_argument(
        "--parallel-insert", type=int, default=None, metavar="N",
        help="LightRAG max_parallel_insert (parallel document inserts) during --index. "
             "Default: MAX_PARALLEL_INSERT from <data-dir>/config/.env; falls through "
             "to 4. Tier 3 OpenAI probe typically recommends 3.",
    )
    parser.add_argument(
        "--enrich-only", action="store_true",
        help="Skip Pass 1 (structural seeds) and Pass 2 (LLM extraction); run only "
             "Pass 3 (structural re-assertion via aedit_entity) against the existing "
             "graph. Use to refresh embeddings after Pass 3 timeouts without re-indexing "
             "from scratch. Requires an existing graphrag/; incompatible with --clear. "
             "Cost: ~$0.02 + minutes.",
    )
    parser.add_argument(
        "--canonicalize-only", action="store_true",
        help="Skip Passes 1-3; run only Pass 4 (entity-name canonicalization via "
             "amerge_entities) against the existing graph. Collapses case-fold dupes "
             "(jdoe / Jdoe / JDoe) and ^User / Person$ paraphrases of known user seeds. "
             "Zero LLM cost; runs in seconds. DESTRUCTIVE — back up <data-dir>/graphrag/ "
             "before first use. Requires an existing graphrag/; incompatible with --clear "
             "and with --enrich-only. See docs/analysis/entity-name-canonicalization.md.",
    )
    parser.add_argument(
        "--detect-limits", action="store_true",
        help="Probe OpenAI rate limits for the chosen extraction model and print "
             "a recommended concurrency setting. No index work; exits after printing.",
    )

    args = parser.parse_args()

    try:
        rc = bootstrap(args.path)
    except ConfigError as e:
        parser.error(str(e))

    if args.detect_limits:
        if not rc.is_openai:
            parser.error("--detect-limits only applies to the OpenAI provider (OPENAI_API_KEY unset).")
        from discourse_explorer._openai_tier import probe_and_recommend
        model = args.extraction_model or rc.default_extraction_model()
        rec = probe_and_recommend(model)
        print(f"Model              : {rec['model']}")
        print(f"RPM ceiling        : {rec['rpm']:,}")
        print(f"TPM ceiling        : {rec['tpm']:,}")
        print(f"Tier hint          : {rec['tier_hint']}")
        print(f"Recommended config :")
        print(f"  llm_model_max_async   = {rec['recommended']['llm_model_max_async']}")
        print(f"  max_parallel_insert   = {rec['recommended']['max_parallel_insert']}")
        return

    if args.index:
        # Serialize on the data dir. Two concurrent indexers overwrite each
        # other's graph rather than merging; see `index_lock`.
        try:
            with index_lock(rc.data_dir):
                asyncio.run(index_topics(
                    rc, clear=args.clear,
                    extraction_model=args.extraction_model,
                    gleaning=args.gleaning,
                    limit=args.limit,
                    llm_model_max_async=args.llm_concurrency,
                    max_parallel_insert=args.parallel_insert,
                    enrich_only=args.enrich_only,
                    canonicalize_only=args.canonicalize_only,
                ))
        except IndexLockError as e:
            print(f"\nRefusing to start: {e}", file=sys.stderr)
            sys.exit(2)
        # Force clean exit. LightRAG's finalize_storages can leave dangling
        # async resources (httpx AsyncClient pool, Faiss OpenMP workers,
        # lingering thread pools) that keep the interpreter alive
        # indefinitely past our actual work. All persistent state
        # (graphml, Faiss indexes, doc_status) is flushed before the final
        # "Indexing complete" print inside index_topics, so exiting here is
        # safe. Observed 2026-04-23: zombie process stayed at 0% CPU for
        # 6+ hours post-completion, only released by SIGTERM. `sys.exit(0)`
        # raises SystemExit so atexit + stdout flushing still run; if it's
        # ever observed to hang too, escalate to `os._exit(0)`.
        sys.exit(0)
    elif args.question:
        mode = args.mode or ("local" if args.local else "mix")
        asyncio.run(query_graph(
            rc, args.question, mode=mode,
            query_model=args.query_model,
            extraction_model=args.extraction_model,
        ))
    else:
        parser.error("Provide a question or use --index to build the knowledge graph.")


if __name__ == "__main__":
    main()
