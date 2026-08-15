"""Tests for Pass 4 entity-name canonicalization (case + paraphrase dupes).

Covers the helpers + orchestration in `discourse_explorer.query`:

  - `_strip_user_paraphrase_affixes(name)` — pure string transform
  - `_pick_canonical_for_case_bucket(variants, seeds_lc)` — pure picker
  - `_canonicalize_case_dupes(rag, pass1_seed_names)` — case-fold merge
  - `_canonicalize_user_paraphrases(rag, user_seed_names)` — affix-aware merge

Run via:
    uv run python -m unittest tests.test_canonicalize_name_dupes
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.query import (  # noqa: E402
    _apply_pass4_writes,
    _canonicalize_case_dupes,
    _canonicalize_user_paraphrases,
    _defer_pass4_writes,
    _pick_canonical_for_case_bucket,
    _strip_user_paraphrase_affixes,
)


class _FakeGraph:
    """Minimal stand-in for `chunk_entity_relation_graph` covering the
    surface Pass 4 reads: `get_all_nodes` + `get_node` + `has_node`."""

    def __init__(self, nodes: dict[str, dict]):
        self._nodes = dict(nodes)

    async def get_all_nodes(self) -> list[dict]:
        return [{**v, "id": k} for k, v in self._nodes.items()]

    async def get_node(self, node_id: str) -> dict | None:
        return self._nodes.get(node_id)

    async def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes


class _FakeRag:
    """Records `amerge_entities` calls and applies a minimal in-memory
    collapse so the test can assert on post-merge graph state too."""

    def __init__(self, nodes: dict[str, dict]):
        self.chunk_entity_relation_graph = _FakeGraph(nodes)
        self.merge_calls: list[dict] = []

    async def amerge_entities(
        self,
        *,
        source_entities,
        target_entity,
        target_entity_data=None,
        **kwargs,
    ):
        g = self.chunk_entity_relation_graph
        target_data = g._nodes.get(target_entity, {}).copy()
        if target_entity_data:
            target_data.update(target_entity_data)
        for s in source_entities:
            g._nodes.pop(s, None)
        g._nodes[target_entity] = target_data
        self.merge_calls.append(
            {
                "sources": tuple(source_entities),
                "target": target_entity,
                "td": target_entity_data or {},
            }
        )


# --- pure helpers --------------------------------------------------------


class StripUserAffixTests(unittest.TestCase):
    def test_strip_user_prefix(self):
        self.assertEqual(_strip_user_paraphrase_affixes("User jdoe"), "jdoe")
        self.assertEqual(_strip_user_paraphrase_affixes("User  Jdoe"), "Jdoe")

    def test_strip_person_suffix(self):
        self.assertEqual(_strip_user_paraphrase_affixes("Jdoe Person"), "Jdoe")
        self.assertEqual(_strip_user_paraphrase_affixes("Jdoe   Person"), "Jdoe")

    def test_strip_both(self):
        self.assertEqual(_strip_user_paraphrase_affixes("User Jdoe Person"), "Jdoe")

    def test_no_strip_when_absent(self):
        self.assertEqual(_strip_user_paraphrase_affixes("jdoe"), "jdoe")
        # No trailing space → not the prefix we mean
        self.assertEqual(_strip_user_paraphrase_affixes("UserStory"), "UserStory")
        # Bare "User" with nothing after is left alone
        self.assertEqual(_strip_user_paraphrase_affixes("User"), "User")
        # Bare "Person" with nothing before is left alone
        self.assertEqual(_strip_user_paraphrase_affixes("Person"), "Person")

    def test_collapses_inner_whitespace(self):
        # 'J Doe' is the LLM's whitespace paraphrase of jdoe — collapse to 'JDoe'
        self.assertEqual(_strip_user_paraphrase_affixes("J Doe"), "JDoe")
        self.assertEqual(_strip_user_paraphrase_affixes("J  Doe"), "JDoe")


class PickCanonicalTests(unittest.TestCase):
    def test_pass1_seed_wins_when_lowercase(self):
        # Pass-1 user seed (lowercase from Discourse `username`) in bucket → wins
        chosen = _pick_canonical_for_case_bucket(
            variants=["Jdoe", "JDoe", "jdoe"],
            pass1_seed_names={"jdoe"},
        )
        self.assertEqual(chosen, "jdoe")

    def test_pass1_seed_wins_when_titlecase(self):
        # Regression test: Pass-1 topic seed (`"How to use X"`, type `topic`)
        # must beat the LLM's title-cased Pass-2 variant (`"How To Use X"`,
        # type `issue` or `other`). Without this preference, the merged
        # canonical loses its `topic` type and the post-index verifier
        # reports "topics missing from graph". This was a real bug discovered
        # when 76 of 1331 topic-titled nodes vanished from the canonical
        # 1.3K-topic corpus on the first Pass 4 run (2026-04-26).
        chosen = _pick_canonical_for_case_bucket(
            variants=["How To Use X", "How to use X"],
            pass1_seed_names={"How to use X"},
        )
        self.assertEqual(chosen, "How to use X")

    def test_no_seed_falls_back_to_lowercase_member(self):
        chosen = _pick_canonical_for_case_bucket(
            variants=["ACME9", "Acme9", "acme9", "acmE9"],
            pass1_seed_names=set(),
        )
        self.assertEqual(chosen, "acme9")

    def test_no_seed_no_lowercase_member_falls_back_alphabetical(self):
        # Neither a seed nor a fully-lowercase member exists → deterministic alphabetical
        chosen = _pick_canonical_for_case_bucket(
            variants=["ACME9", "Acme9", "AcmE9"],
            pass1_seed_names=set(),
        )
        self.assertEqual(chosen, "ACME9")


# --- _canonicalize_case_dupes -------------------------------------------


class CanonicalizeCaseDupesTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_graph(self):
        rag = _FakeRag({})
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names=set())
        self.assertEqual(merged, 0)
        self.assertEqual(rag.merge_calls, [])

    async def test_no_dupes_no_merge(self):
        rag = _FakeRag(
            {
                "alpha": {"entity_type": "user"},
                "beta": {"entity_type": "topic"},
                "gamma": {"entity_type": "category"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names={"alpha"})
        self.assertEqual(merged, 0)
        self.assertEqual(rag.merge_calls, [])

    async def test_case_collapse_with_pass1_seed_preserves_user_type(self):
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "Jdoe": {"entity_type": "other"},
                "JDoe": {"entity_type": "other"},
                "unrelated": {"entity_type": "topic"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names={"jdoe"})
        self.assertEqual(merged, 1)
        call = rag.merge_calls[0]
        self.assertEqual(call["target"], "jdoe")
        self.assertEqual(set(call["sources"]), {"Jdoe", "JDoe"})
        # target_entity_data must lock the user type so the LightRAG default
        # `keep_first` can't accidentally adopt the source nodes' "other".
        self.assertEqual(call["td"], {"entity_type": "user"})

    async def test_case_collapse_no_seed_picks_lowercase(self):
        rag = _FakeRag(
            {
                "ACME9": {"entity_type": "other"},
                "Acme9": {"entity_type": "other"},
                "acme9": {"entity_type": "other"},
                "acmE9": {"entity_type": "other"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names=set())
        self.assertEqual(merged, 1)
        call = rag.merge_calls[0]
        self.assertEqual(call["target"], "acme9")
        self.assertEqual(set(call["sources"]), {"ACME9", "Acme9", "acmE9"})

    async def test_multiple_buckets(self):
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "Jdoe": {"entity_type": "other"},
                "ACME9": {"entity_type": "other"},
                "acme9": {"entity_type": "other"},
                "lonely": {"entity_type": "topic"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names={"jdoe"})
        self.assertEqual(merged, 2)
        targets = {c["target"] for c in rag.merge_calls}
        self.assertEqual(targets, {"jdoe", "acme9"})

    async def test_idempotent_on_clean_graph(self):
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "alpha": {"entity_type": "topic"},
            }
        )
        await _canonicalize_case_dupes(rag, pass1_seed_names={"jdoe"})
        rag.merge_calls.clear()
        again = await _canonicalize_case_dupes(rag, pass1_seed_names={"jdoe"})
        self.assertEqual(again, 0)
        self.assertEqual(rag.merge_calls, [])


# --- _canonicalize_user_paraphrases -------------------------------------


class CanonicalizeUserParaphrasesTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_prefix_merged_into_seed(self):
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "User jdoe": {"entity_type": "other"},
                "User Jdoe": {"entity_type": "other"},
            }
        )
        merged = await _canonicalize_user_paraphrases(rag, user_seed_names={"jdoe"})
        self.assertEqual(merged, 1)
        call = rag.merge_calls[0]
        self.assertEqual(call["target"], "jdoe")
        self.assertEqual(set(call["sources"]), {"User jdoe", "User Jdoe"})
        self.assertEqual(call["td"], {"entity_type": "user"})

    async def test_person_suffix_merged_into_seed(self):
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "Jdoe Person": {"entity_type": "other"},
            }
        )
        merged = await _canonicalize_user_paraphrases(rag, {"jdoe"})
        self.assertEqual(merged, 1)
        self.assertEqual(rag.merge_calls[0]["sources"], ("Jdoe Person",))

    async def test_whitespace_paraphrase_merged(self):
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "J Doe": {"entity_type": "other"},
            }
        )
        merged = await _canonicalize_user_paraphrases(rag, {"jdoe"})
        self.assertEqual(merged, 1)

    async def test_only_strips_when_match_lands_in_user_seeds(self):
        # "User Story" must NOT be stripped to "Story" because no `story` user seed exists.
        rag = _FakeRag(
            {
                "User Story": {"entity_type": "other"},
                "alpha": {"entity_type": "user"},
            }
        )
        merged = await _canonicalize_user_paraphrases(rag, {"alpha"})
        self.assertEqual(merged, 0)
        self.assertEqual(rag.merge_calls, [])

    async def test_no_user_seeds_is_noop(self):
        rag = _FakeRag(
            {
                "User foo": {"entity_type": "other"},
                "Jdoe Person": {"entity_type": "other"},
            }
        )
        merged = await _canonicalize_user_paraphrases(rag, user_seed_names=set())
        self.assertEqual(merged, 0)


# --- _stub_pass4_embeddings + _refresh_pass4_embeddings ----------------


class _FakeVDB:
    """Minimal VDB stand-in: tracks upsert + delete calls and stores payloads."""

    def __init__(self):
        self.upsert_calls: list[dict] = []
        self.delete_calls: list[list] = []
        self.stored: dict[str, dict] = {}

    async def upsert(self, payload: dict):
        self.upsert_calls.append(payload)
        self.stored.update(payload)

    async def delete(self, ids: list):
        self.delete_calls.append(list(ids))
        for i in ids:
            self.stored.pop(i, None)


class _FakeEmbeddingFunc:
    def __init__(self, dim=8):
        self.embedding_dim = dim

        async def real(texts):
            # Deterministic non-zero vectors so tests can detect "real" vs stub.
            return [[float(len(t)) / 10.0] * dim for t in texts]

        self.func = real


class _FakeRagWithVDBs(_FakeRag):
    """Extends _FakeRag with `entities_vdb`, `relationships_vdb`, and an
    `embedding_func` so the stub-context + refresh path can run end-to-end.
    Adds `get_edge` for the refresh helper."""

    def __init__(self, nodes, edges=None):
        super().__init__(nodes)
        self.entities_vdb = _FakeVDB()
        self.relationships_vdb = _FakeVDB()
        self.embedding_func = _FakeEmbeddingFunc()
        self._edges: dict[tuple[str, str], dict] = dict(edges or {})
        self.chunk_entity_relation_graph.get_edge = self._get_edge  # bind

    async def _get_edge(self, src, tgt):
        return self._edges.get((src, tgt))


class DeferAndApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_defer_buffers_vdb_writes(self):
        rag = _FakeRagWithVDBs({"jdoe": {"entity_type": "user"}})
        with _defer_pass4_writes(rag) as (ent_ups, rel_ups, ent_dels, rel_dels):
            # Buffered upserts don't call through to real VDB.
            await rag.entities_vdb.upsert({
                "ent-x": {"entity_name": "jdoe", "content": "x"},
            })
            await rag.relationships_vdb.upsert({
                "rel-y": {"src_id": "jdoe", "tgt_id": "topic-1", "content": "..."},
            })
            # Buffered deletes also don't call through.
            await rag.entities_vdb.delete(["ent-other"])
            await rag.relationships_vdb.delete(["rel-other"])

        # Buffers populated.
        self.assertIn("ent-x", ent_ups)
        self.assertIn("rel-y", rel_ups)
        self.assertIn("ent-other", ent_dels)
        self.assertIn("rel-other", rel_dels)
        # Real VDBs untouched during the deferred phase.
        self.assertEqual(rag.entities_vdb.upsert_calls, [])
        self.assertEqual(rag.entities_vdb.delete_calls, [])
        self.assertEqual(rag.relationships_vdb.upsert_calls, [])
        self.assertEqual(rag.relationships_vdb.delete_calls, [])

    async def test_defer_does_not_touch_embedding_func(self):
        # Regression: an earlier version stubbed `embedding_func.func` to a
        # zero-vector fallback, which corrupted LightRAG's lazily-initialized
        # embedding worker pool — workers initialized against the stubbed
        # ref, then hung past the 60s timeout when the apply phase asked
        # for real embeddings. Leaving the func untouched fixes the hang.
        rag = _FakeRagWithVDBs({"jdoe": {"entity_type": "user"}})
        original_embed = rag.embedding_func.func
        with _defer_pass4_writes(rag):
            self.assertIs(rag.embedding_func.func, original_embed)
        self.assertIs(rag.embedding_func.func, original_embed)

    async def test_defer_restores_originals_after_context(self):
        rag = _FakeRagWithVDBs({"jdoe": {"entity_type": "user"}})
        with _defer_pass4_writes(rag):
            pass
        # Real VDB methods restored (test by behavior — unbuffered call).
        await rag.entities_vdb.upsert({"ent-y": {"entity_name": "x", "content": "c"}})
        self.assertEqual(len(rag.entities_vdb.upsert_calls), 1)

    async def test_defer_conflict_delete_after_upsert(self):
        # Buffered upsert then a delete on the same id → upsert dropped,
        # delete kept (the entity is gone net-net).
        rag = _FakeRagWithVDBs({})
        with _defer_pass4_writes(rag) as (ent_ups, _, ent_dels, _):
            await rag.entities_vdb.upsert({"ent-x": {"entity_name": "tmp", "content": "c"}})
            self.assertIn("ent-x", ent_ups)
            await rag.entities_vdb.delete(["ent-x"])
            self.assertNotIn("ent-x", ent_ups)
            self.assertIn("ent-x", ent_dels)

    async def test_defer_conflict_upsert_after_delete(self):
        # Buffered delete then an upsert on the same id → delete dropped,
        # upsert kept (the entity is back, with fresh content).
        rag = _FakeRagWithVDBs({})
        with _defer_pass4_writes(rag) as (ent_ups, _, ent_dels, _):
            await rag.entities_vdb.delete(["ent-x"])
            self.assertIn("ent-x", ent_dels)
            await rag.entities_vdb.upsert({"ent-x": {"entity_name": "back", "content": "c"}})
            self.assertNotIn("ent-x", ent_dels)
            self.assertIn("ent-x", ent_ups)

    async def test_apply_flushes_in_order(self):
        rag = _FakeRagWithVDBs({})
        ent_ups = {"ent-a": {"entity_name": "a", "content": "c"}}
        rel_ups = {"rel-b": {"src_id": "a", "tgt_id": "b", "content": "c"}}
        ent_dels = {"ent-old"}
        rel_dels = {"rel-old"}
        counts = await _apply_pass4_writes(rag, ent_ups, rel_ups, ent_dels, rel_dels)
        self.assertEqual(counts, {
            "ent_deleted": 1, "rel_deleted": 1,
            "ent_upserted": 1, "rel_upserted": 1,
        })
        # Each VDB had exactly one delete + one upsert call.
        self.assertEqual(len(rag.entities_vdb.delete_calls), 1)
        self.assertEqual(len(rag.entities_vdb.upsert_calls), 1)
        self.assertEqual(len(rag.relationships_vdb.delete_calls), 1)
        self.assertEqual(len(rag.relationships_vdb.upsert_calls), 1)
        self.assertEqual(rag.entities_vdb.delete_calls[0], ["ent-old"])
        self.assertEqual(rag.entities_vdb.upsert_calls[0], ent_ups)

    async def test_apply_noop_on_empty_buffers(self):
        rag = _FakeRagWithVDBs({})
        counts = await _apply_pass4_writes(rag, {}, {}, set(), set())
        self.assertEqual(counts, {
            "ent_deleted": 0, "rel_deleted": 0,
            "ent_upserted": 0, "rel_upserted": 0,
        })
        self.assertEqual(rag.entities_vdb.upsert_calls, [])
        self.assertEqual(rag.entities_vdb.delete_calls, [])


if __name__ == "__main__":
    unittest.main()


class TagSeparatorVariantTests(unittest.IsolatedAsyncioTestCase):
    """Pass 4a must collapse Discourse slug-separator variants of a tag.

    Discourse renders a release tag's *display name* with U+2024 ONE DOT LEADER
    (`2025․06`) but slugs it `2025-06`. A corpus scraped across the change in
    how tag nodes are named therefore carries both spellings as separate nodes,
    each holding roughly half the edges. Measured on the reference corpus:
    `2023․06` at degree 345 beside `2023-06` at degree 346.

    Case folding alone cannot see them as equal, and neither can a general
    Unicode confusables fold: U+2024 normalizes to `.`, giving `2025.06`, which
    still is not `2025-06`.
    """

    async def test_dot_leader_tag_collapses_onto_the_slug_seed(self):
        """BOTH spellings are typed `tag`, so both are structural Pass-1 seeds.

        This is the realistic case and the one that matters. An earlier version
        of this test seeded only the slug, which made "is a Pass-1 seed"
        disambiguate the bucket. In a real graph it cannot, and the picker
        silently chose the dot-leader form, deleting every slug node and
        breaking the documented graph<->SQL join contract.
        """
        rag = _FakeRag(
            {
                "2025․06": {"entity_type": "tag"},  # legacy display name, first
                "2025-06": {"entity_type": "tag"},   # slug form
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"2025-06", "2025․06"})
        self.assertEqual(merged, 1)
        self.assertEqual(len(rag.merge_calls), 1)
        call = rag.merge_calls[0]
        self.assertEqual(call["target"], "2025-06",
                         "the Pass-1 slug seed must win, not the display name")
        self.assertIn("2025․06", call["sources"])

    async def test_period_and_underscore_variants_also_collapse(self):
        rag = _FakeRag(
            {
                "2024.06": {"entity_type": "tag"},   # non-slug FIRST on purpose
                "2024_06": {"entity_type": "tag"},
                "2024-06": {"entity_type": "tag"},
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"2024-06", "2024.06", "2024_06"})
        self.assertEqual(merged, 1)
        self.assertEqual(rag.merge_calls[0]["target"], "2024-06")
        self.assertEqual(
            sorted(rag.merge_calls[0]["sources"]),
            ["2024.06", "2024_06"])

    async def test_rule_is_scoped_to_tags_and_leaves_content_entities_alone(self):
        """`foo.bar` and `foo-bar` may be genuinely distinct component names.

        Discourse slugs cannot contain `.`, so the equivalence is safe within
        tags and unsafe outside them.
        """
        rag = _FakeRag(
            {
                "foo-bar": {"entity_type": "component"},
                "foo.bar": {"entity_type": "component"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names=set())
        self.assertEqual(merged, 0, "separator rule must not apply to non-tags")
        self.assertEqual(rag.merge_calls, [])

    async def test_distinct_tags_are_not_merged(self):
        rag = _FakeRag(
            {
                "2024-06": {"entity_type": "tag"},
                "2025-06": {"entity_type": "tag"},
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"2024-06", "2025-06"})
        self.assertEqual(merged, 0)

    async def test_case_and_separator_fold_together(self):
        rag = _FakeRag(
            {
                "Release․2025": {"entity_type": "tag"},  # non-slug FIRST
                "release-2025": {"entity_type": "tag"},
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"release-2025", "Release․2025"})
        self.assertEqual(merged, 1)
        self.assertEqual(rag.merge_calls[0]["target"], "release-2025")


class DistinctStructuralTypesTests(unittest.IsolatedAsyncioTestCase):
    """Two different STRUCTURAL types in one bucket are two different entities.

    A Discourse forum can have a category `Client` and a tag `client`. Pass 1
    seeds both on every run, and case folding puts them in one bucket where both
    are Pass-1 seeds, so "prefer the Pass-1 seed" cannot choose. The merge then
    produced a single node typed `category` whose description literally read
    `Forum tag: client<SEP>Forum category: Client`, conflating 203 topics in the
    category with 13 topics carrying the tag. Measured on the reference corpus:
    8 tags lost this way.

    The fix is not to pick better, it is not to merge. Only DIFFERENT structural
    types are protected; an LLM-typed variant collapsing into its structural
    seed must keep working, since that is what Pass 4a exists for.
    """

    async def test_tag_and_category_of_the_same_name_do_not_merge(self):
        rag = _FakeRag(
            {
                "client": {"entity_type": "tag"},
                "Client": {"entity_type": "category"},
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"client", "Client"})
        self.assertEqual(merged, 0, "a tag and a category are distinct entities")
        self.assertEqual(rag.merge_calls, [])
        g = rag.chunk_entity_relation_graph._nodes
        self.assertIn("client", g)
        self.assertIn("Client", g)

    async def test_user_and_topic_of_the_same_name_do_not_merge(self):
        rag = _FakeRag(
            {
                "Migration": {"entity_type": "topic"},
                "migration": {"entity_type": "user"},
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"Migration", "migration"})
        self.assertEqual(merged, 0)

    async def test_llm_typed_variant_still_collapses_into_its_structural_seed(self):
        """The load-bearing case Pass 4a exists for. `other` is not structural,
        so only one structural type is present and the merge must proceed."""
        rag = _FakeRag(
            {
                "jdoe": {"entity_type": "user"},
                "Jdoe": {"entity_type": "other"},
                "JDoe": {"entity_type": "issue"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names={"jdoe"})
        self.assertEqual(merged, 1)
        self.assertEqual(rag.merge_calls[0]["target"], "jdoe")

    async def test_same_structural_type_still_merges(self):
        """Tonight's separator fix must keep working: both are `tag`."""
        rag = _FakeRag(
            {
                "2025․06": {"entity_type": "tag"},
                "2025-06": {"entity_type": "tag"},
            }
        )
        merged = await _canonicalize_case_dupes(
            rag, pass1_seed_names={"2025-06", "2025․06"})
        self.assertEqual(merged, 1)
        self.assertEqual(rag.merge_calls[0]["target"], "2025-06")

    async def test_seedless_bucket_of_one_structural_type_still_merges(self):
        rag = _FakeRag(
            {
                "ACME9": {"entity_type": "other"},
                "acme9": {"entity_type": "other"},
            }
        )
        merged = await _canonicalize_case_dupes(rag, pass1_seed_names=set())
        self.assertEqual(merged, 1)
