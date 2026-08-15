"""On-disk JSON cache for LLM-generated post bodies.

Phase 2 — Sit 10. The body generator (`bodies.generate_body`) consults this
cache before invoking the LLM and writes back on every successful generation.
The combination short-circuits the LLM on a re-run with identical inputs
(deterministic structure + cached bodies = bit-for-bit identical forum) and
keeps generation cost paid exactly once per `(product, provider, seed, scale)`
tuple.

Schema
------
Single JSON object on disk, mapping ``"<topic_id>:<post_index>"`` strings to
the body string for that post. ``post_index`` is the post's per-topic
1-indexed position (`Post.post_number` in `generators/posts.py`) — `1` is the
OP, `>=2` are replies. The composite key is human-readable when grepping the
cache file and unambiguous because both components are positive integers.

Naming convention
-----------------
Per the design doc, callers compute the cache filename as
``<product>-<provider>-seed<N>-<scale>.json`` (e.g.
``crown-of-brine-openai-seed42-tiny.json``) so switching credentials or
scales never blends outputs across backends. `Cache` itself only takes a
`Path` — naming is a pipeline concern, not a cache concern.

Write strategy
--------------
Write-through. Every `set` flushes to disk immediately. The seeder's working
sets stay small (low thousands of bodies for `medium`, ~10x more for `large`)
so the per-write JSON dump cost is negligible compared to the dominant LLM
round-trip; in exchange the cache survives an interrupted run with no
explicit `flush` step. Atomic write via tmp + `os.replace` so a crash
mid-dump can't corrupt the file (the prior version stays on disk).

Missing files are treated as an empty cache so a fresh run on a new
`(product, provider, seed, scale)` tuple just works without any seeding step.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional


def _key(topic_id: int, post_index: int) -> str:
    """Compose the dict key for a `(topic_id, post_index)` pair.

    Centralising the format here keeps the on-disk representation in one
    place — if it ever needs to change, only this function (and the docstring
    above) move.
    """
    return f"{topic_id}:{post_index}"


class Cache:
    """JSON-backed body cache keyed by `(topic_id, post_index)`.

    Construction loads any existing JSON at `path`; a missing file yields an
    empty cache. `get`/`set` operate on the in-memory dict; `set` flushes
    write-through to disk so the cache survives interrupted runs.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._entries: dict[str, str] = {}
        if self._path.exists():
            # Empty file → empty cache; non-JSON contents will raise, which is
            # the right behavior — the user should know their cache is wedged
            # rather than silently start over and burn LLM credits.
            text = self._path.read_text(encoding="utf-8")
            if text.strip():
                loaded = json.loads(text)
                if not isinstance(loaded, dict):
                    raise ValueError(
                        f"cache file {self._path} must contain a JSON object, "
                        f"got {type(loaded).__name__}"
                    )
                # Coerce values to str defensively — JSON could in theory
                # round-trip a non-string if a caller hand-edits the file.
                self._entries = {str(k): str(v) for k, v in loaded.items()}

    @property
    def path(self) -> Path:
        """The on-disk path this cache reads from + writes to."""
        return self._path

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, item: object) -> bool:
        # Convenience for tests + callers that want a presence check without
        # paying for a full `get`. Accepts a `(topic_id, post_index)` tuple.
        if isinstance(item, tuple) and len(item) == 2:
            topic_id, post_index = item
            return _key(int(topic_id), int(post_index)) in self._entries
        return False

    def get(self, topic_id: int, post_index: int) -> Optional[str]:
        """Return the cached body, or `None` on miss."""
        return self._entries.get(_key(topic_id, post_index))

    def set(self, topic_id: int, post_index: int, body: str) -> None:
        """Store `body` under `(topic_id, post_index)` and flush to disk.

        Atomic write via tmp + `os.replace` so a crash mid-dump can't corrupt
        the cache file. The parent directory is created on demand — caller
        doesn't need to ensure it exists ahead of time.
        """
        self._entries[_key(topic_id, post_index)] = body
        self._flush()

    def _flush(self) -> None:
        """Write the current entries to disk atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Sorted keys keep the file diff-friendly when committed to git;
        # the seeder's whole point is reproducibility.
        payload = json.dumps(self._entries, indent=2, sort_keys=True)
        # `delete=False` so we own the rename. NamedTemporaryFile in the same
        # directory as the target so `os.replace` is atomic on every POSIX
        # filesystem (cross-device replace falls back to non-atomic copy).
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self._path.parent),
            prefix=self._path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        os.replace(tmp_path, self._path)
