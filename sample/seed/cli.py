"""Sample-seeder command-line interface.

Two subcommands:

- `init` — assemble a deterministic forum from a `(seed, scale, product)`
  spec. `--dry-run` mode dumps the bake to JSON without talking to a
  Discourse stack; without `--dry-run` (Sit 14, Phase 3.3) the bake is
  POSTed to a running Discourse instance via the API client.
- `extend` — extend an existing forum with new topics/replies/release-burst
  events. Lands in Phase 4; raises `NotImplementedError` for now (so
  `--help` still surfaces the subcommand and the eventual flags don't come
  out of nowhere).

Usage:

    # Offline JSON dump (Phase 1):
    uv run python -m sample.seed init \\
        --seed 42 --scale tiny \\
        --dry-run --output /tmp/sample-42.json

    # Live push to Discourse (Phase 3.3):
    DISCOURSE_HOST=http://localhost:4200 \\
        uv run --env-file=sample/.env python -m sample.seed init \\
        --seed 42 --scale tiny

The dry-run path runs every Phase-1 generator in dependency order
(categories → tags → users → timeline → topics → posts), bundles them into
a `Forum`, and writes JSON pretty-printed with `indent=2, sort_keys=True`.
Datetime values render as ISO-8601 strings; frozen dataclasses are walked by
`dataclasses.asdict`.

The live path runs the same generator chain, then hands the `Forum` to
`pipeline.push_forum` which POSTs categories / users / topics / replies in
chronological order. Required env vars: `DISCOURSE_HOST` (or
`DISCOURSE_URL`), `DISCOURSE_API_KEY`, `DISCOURSE_API_USERNAME`. Live mode
and `--dry-run` are mutually exclusive — the live path doesn't write JSON
(callers wanting an audit trail can re-run with `--dry-run` to capture
the deterministic structure separately).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from .content import blocklist as blocklist_module
from .content.bodies import generate_body
from .content.cache import Cache
from .content.llm import select_provider
from .discourse_api import DiscourseAPIError, DiscourseClient
from .forum import Forum, ForumExtension
from .pipeline import (
    PushResult,
    build_forum,
    extend_forum,
    push_extension,
    push_forum,
)
from .product import crown_of_brine
from .universe import GenerationSpec

# Map of `--product` CLI value -> imported product module. Centralised so a
# future product (Phase 7+ Hexenwald Saga) only adds one entry. The CLI
# currently only accepts `crown-of-brine` (one product = one choice = no
# ambiguity), but the indirection is cheap and keeps the CLI shape stable.
_PRODUCT_MODULES: dict[str, Any] = {
    "crown-of-brine": crown_of_brine,
}

# Default seed + scale values, exposed as module constants so tests / future
# callers can reference them without re-stating literal magic numbers.
_DEFAULT_SEED = 42
_DEFAULT_SCALE = "tiny"
_DEFAULT_PRODUCT = "crown-of-brine"

# Valid `--scale` choices. Keep in sync with `SCALE_PRESETS` in
# `sample/seed/universe.py` — argparse validates the CLI input against these
# strings; the spec-construction step will catch any drift on top of that.
_SCALE_CHOICES: tuple[str, ...] = ("tiny", "small", "medium", "large")


# Sit 18 — `--mixed` scale-derived defaults for `(add_topics, add_replies)`.
# Tuned to be a meaningful delta over the base bake without dwarfing it.
# Burst-cluster counts are scale-independent (already 10-20 topics + 50-100
# replies, tied to a 7-day calendar window, not corpus size).
_MIXED_SCALE_DEFAULTS: dict[str, tuple[int, int]] = {
    "tiny": (5, 15),
    "small": (15, 50),
    "medium": (30, 100),
    "large": (50, 200),
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser with `init` + `extend`.

    Both subcommands are present even though `extend` is a stub — having them
    both in `--help` from day one means the eventual Phase-4 implementation
    doesn't surprise existing users / scripts.
    """
    parser = argparse.ArgumentParser(
        prog="sample.seed",
        description=(
            "Generate or extend a deterministic synthetic Discourse forum "
            "(see sample/README.md)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="Assemble a fresh forum from (seed, scale, product).",
        description=(
            "Run all Phase-1 generators and either dump the result to JSON "
            "(--dry-run, the only mode in Phase 1) or POST it to a live "
            "Discourse instance (Phase 3+, not implemented yet)."
        ),
    )
    init.add_argument(
        "--seed",
        type=int,
        default=_DEFAULT_SEED,
        help=f"integer seed (default: {_DEFAULT_SEED}).",
    )
    init.add_argument(
        "--scale",
        choices=_SCALE_CHOICES,
        default=_DEFAULT_SCALE,
        help=f"scale preset (default: {_DEFAULT_SCALE}).",
    )
    init.add_argument(
        "--product",
        choices=sorted(_PRODUCT_MODULES),
        default=_DEFAULT_PRODUCT,
        help=f"product universe (default: {_DEFAULT_PRODUCT}).",
    )
    init.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip the Discourse stack; dump the bake to a JSON file. "
            "Currently the only supported mode (Phase 1)."
        ),
    )
    init.add_argument(
        "--output",
        type=Path,
        help=(
            "Path to write JSON to. Required when --dry-run is set. "
            "Parent directory must exist."
        ),
    )
    init.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Skip LLM body generation; every post gets the deterministic "
            "placeholder body. Useful for fast structural checks and for "
            "running the seeder without an OPENAI_API_KEY / Ollama daemon."
        ),
    )

    extend = sub.add_parser(
        "extend",
        help="Extend an existing forum (Phase 4).",
        description=(
            "Add new topics + replies to an existing forum, dated AFTER the "
            "base bake's last timestamp. Sit 15 ships `--add-topics N`; "
            "`--add-replies` and `--release-burst` land in later sits."
        ),
    )
    extend.add_argument(
        "--seed",
        type=int,
        default=_DEFAULT_SEED,
        help=(
            f"BASE seed — must match the original `init` seed so the "
            f"extension reuses the same categories / users / tags "
            f"(default: {_DEFAULT_SEED})."
        ),
    )
    extend.add_argument(
        "--scale",
        choices=_SCALE_CHOICES,
        default=_DEFAULT_SCALE,
        help=(
            f"BASE scale — must match the original `init` scale "
            f"(default: {_DEFAULT_SCALE})."
        ),
    )
    extend.add_argument(
        "--product",
        choices=sorted(_PRODUCT_MODULES),
        default=_DEFAULT_PRODUCT,
        help=f"product universe (default: {_DEFAULT_PRODUCT}).",
    )
    extend.add_argument(
        "--extend-seed",
        type=int,
        required=True,
        help=(
            "Integer seed for the extension's RNG. Distinct from --seed "
            "so the same base can be extended multiple times "
            "deterministically."
        ),
    )
    extend.add_argument(
        "--add-topics",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Number of new topics to generate. Default 0 = skip topic "
            "extension. Combine with --add-replies for mixed mode."
        ),
    )
    extend.add_argument(
        "--add-replies",
        type=int,
        default=0,
        metavar="M",
        help=(
            "Number of replies to append to BASE topics, distributed "
            "weighted by each topic's existing reply count "
            "(Sit 16). Default 0 = skip reply extension. At least one "
            "of --add-topics, --add-replies, or --release-burst must be "
            "active."
        ),
    )
    extend.add_argument(
        "--release-burst",
        type=str,
        default=None,
        metavar="VERSION",
        help=(
            "Generate a release-burst cluster of 10-20 topics + 50-100 "
            "replies within a 7-day window around a fictional release "
            "date, every burst topic tagged with VERSION (one of the "
            "product's `game-version` axis values, e.g. `remaster`). "
            "Sit 17."
        ),
    )
    extend.add_argument(
        "--mixed",
        action="store_true",
        help=(
            "Convenience: enable all three modes with scale-derived "
            "defaults (`--add-topics`, `--add-replies`, and "
            "`--release-burst <latest-version>`). Explicit flags still "
            "override their mixed default. Sit 18."
        ),
    )
    extend.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Skip the Discourse stack; dump the extension to a JSON file. "
            "Without --dry-run (Sit 15.1) the extension is POSTed to a "
            "running Discourse instance, reusing the base bake's already-"
            "pushed categories + users."
        ),
    )
    extend.add_argument(
        "--output",
        type=Path,
        help=(
            "Path to write JSON to. Required when --dry-run is set. "
            "Parent directory must exist."
        ),
    )
    extend.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Skip LLM body generation; every new post gets the deterministic "
            "placeholder body. Same semantics as `init --no-llm`."
        ),
    )

    return parser


def _serialize_value(value: Any) -> Any:
    """Recursive default for `json.dumps` — handles dataclasses + datetime.

    `dataclasses.asdict` walks frozen dataclasses fine on its own, but it
    doesn't touch `datetime` (it leaves them in the dict it returns). This
    function is the `default=` hook for `json.dumps`, so it only fires on
    types `json` doesn't natively know — `datetime` is the only one we need
    to translate. Everything else falls through to a `TypeError` so an
    unexpected type is loud rather than silently coerced.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(
        f"object of type {type(value).__name__} is not JSON serialisable"
    )


def _forum_to_dict(forum: Forum) -> dict[str, Any]:
    """Convert a `Forum` to a plain dict ready for `json.dumps`.

    `dataclasses.asdict` walks the nested User / Topic / Post dataclasses
    automatically. Datetime values come through as `datetime` instances and
    are translated by `_serialize_value` at dump time.
    """
    return dataclasses.asdict(forum)


# Cache files live next to the content modules so a `git status` shows
# them alongside the generators that produced them. Naming follows the
# design doc: `<product>-<provider>-seed<N>-<scale>.json`.
_CACHE_DIR = Path(__file__).resolve().parent / "content" / "cache"


def _provider_slug(provider: object) -> str:
    """Reduce a provider class name to a short cache-filename token.

    `OpenAIProvider` -> `openai`, `OllamaProvider` -> `ollama`. Lowercased
    + `Provider` suffix stripped. Stable across the two providers shipped
    today; future additions inherit the same convention.
    """
    name = type(provider).__name__.lower()
    if name.endswith("provider"):
        name = name[: -len("provider")]
    return name or "unknown"


def _cache_path_for(
    product_name: str, provider: object, seed: int, scale: str
) -> Path:
    """Compose the design-doc cache filename for this `(spec, provider)`."""
    return _CACHE_DIR / (
        f"{product_name}-{_provider_slug(provider)}-seed{seed}-{scale}.json"
    )


def _make_body_provider(
    product_name: str, seed: int, scale: str, spec: GenerationSpec
):
    """Build a body-provider closure with a fresh provider + cache.

    Per-call rng draws are taken from a single `spec.rng("bodies")` stream
    captured in the closure. Sit 10's `generate_body` doesn't currently
    consume the rng but does accept it; threading one shared stream keeps
    the call deterministic across runs and matches the per-component
    derived-stream rule from `sample/CLAUDE.md`.
    """
    provider = select_provider()
    cache_path = _cache_path_for(product_name, provider, seed, scale)
    cache = Cache(cache_path)
    bodies_rng = spec.rng("bodies")

    def _provider_callable(topic, post) -> str:
        return generate_body(
            topic,
            post,
            rng=bodies_rng,
            llm=provider,
            cache=cache,
            blocklist=blocklist_module,
        )

    return _provider_callable


def _build_forum(
    seed: int, scale: str, product_name: str, *, no_llm: bool = False
) -> Forum:
    """Run every Phase-1 generator and assemble the `Forum` aggregate.

    Generators run in fixed dependency order: categories -> tags -> users ->
    timeline -> topics -> posts. Same `(seed, scale, product_name)` tuple
    yields a bit-for-bit identical `Forum` when `no_llm=True` (placeholder
    bodies). With `no_llm=False` the body strings come from the LLM
    provider (cached per `(product, provider, seed, scale)`); structure
    stays deterministic regardless.
    """
    if product_name not in _PRODUCT_MODULES:
        # Defensive: argparse already restricts --product to known values, so
        # this fires only if a programmatic caller bypasses argparse. Better
        # to surface a clear error than to AttributeError on a missing key.
        raise ValueError(
            f"unknown product {product_name!r}; "
            f"expected one of {sorted(_PRODUCT_MODULES)}"
        )
    product = _PRODUCT_MODULES[product_name]
    spec = GenerationSpec(seed=seed, scale=scale, product=product)

    body_provider = (
        None
        if no_llm
        else _make_body_provider(product_name, seed, scale, spec)
    )

    return build_forum(
        spec,
        body_provider=body_provider,
        product_name=product_name,
    )


def _build_client_from_env() -> DiscourseClient:
    """Construct a `DiscourseClient` from env vars; raise if any are missing.

    Required env: `DISCOURSE_HOST` or `DISCOURSE_URL` (full base URL),
    `DISCOURSE_API_KEY`, `DISCOURSE_API_USERNAME`. We accept either
    `DISCOURSE_HOST` or `DISCOURSE_URL` because the project's `.env.example`
    uses the former while many Discourse self-host docs reference the
    latter — supporting both is a small kindness.
    """
    base_url = os.environ.get("DISCOURSE_URL") or os.environ.get(
        "DISCOURSE_HOST"
    )
    # `sample/.env` separates DISCOURSE_HOST (just the hostname — also fed
    # to the bitnami container as its public hostname) from DISCOURSE_PORT
    # (the host-side port mapping). Combine them when HOST has no scheme +
    # no `:port` so the seeder can use the same `.env` the compose stack
    # consumes.
    discourse_port = os.environ.get("DISCOURSE_PORT")
    if (
        base_url
        and "://" not in base_url
        and ":" not in base_url
        and discourse_port
    ):
        base_url = f"{base_url}:{discourse_port}"
    api_key = os.environ.get("DISCOURSE_API_KEY")
    api_username = os.environ.get("DISCOURSE_API_USERNAME")

    missing: list[str] = []
    if not base_url:
        missing.append("DISCOURSE_HOST (or DISCOURSE_URL)")
    if not api_key:
        missing.append("DISCOURSE_API_KEY")
    if not api_username:
        missing.append("DISCOURSE_API_USERNAME")
    if missing:
        raise RuntimeError(
            "live Discourse mode requires the following env vars: "
            + ", ".join(missing)
            + " — set them in sample/.env and source via "
            "`uv run --env-file=sample/.env`"
        )

    # `base_url` from .env may be a bare host like `localhost:4200`; the
    # client itself doesn't care, but Discourse REST is HTTP-only on the
    # bitnami test stack, so we add a scheme if absent.
    assert base_url is not None  # narrow for the type checker
    if "://" not in base_url:
        base_url = f"http://{base_url}"

    assert api_key is not None and api_username is not None
    return DiscourseClient(
        base_url=base_url,
        api_key=api_key,
        api_username=api_username,
    )


def _print_push_summary(
    result: PushResult, *, base_url: Optional[str] = None
) -> None:
    """Print a concise summary of what landed on the live forum.

    Goes to stdout so a script can grep / parse it. Errors (if any) go to
    stderr so a non-empty error list doesn't pollute the JSON-friendly
    counts on stdout.
    """
    print(
        f"pushed: {len(result.category_ids)} categories, "
        f"{len(result.user_ids)} users, "
        f"{len(result.topic_ids)} topics, "
        f"{result.post_count} posts"
    )
    if base_url:
        print(f"view at: {base_url}")
    if result.errors:
        print(
            f"  (with {len(result.errors)} non-fatal issue(s))",
            file=sys.stderr,
        )
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)


def _run_init(args: argparse.Namespace) -> int:
    """Handle `init`. Returns the process exit code."""
    if args.dry_run:
        if args.output is None:
            print(
                "error: --output PATH is required when --dry-run is set.",
                file=sys.stderr,
            )
            return 2

        output_path: Path = args.output
        if output_path.parent and not output_path.parent.exists():
            # Argparse can't validate parent existence ahead of time; we
            # surface it explicitly so a typo in the path doesn't fail
            # mid-write with a less-friendly OSError trace.
            print(
                f"error: parent directory {output_path.parent} does not exist.",
                file=sys.stderr,
            )
            return 2

        forum = _build_forum(
            seed=args.seed,
            scale=args.scale,
            product_name=args.product,
            no_llm=args.no_llm,
        )
        payload = _forum_to_dict(forum)
        output_path.write_text(
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                default=_serialize_value,
            )
            + "\n",
            encoding="utf-8",
        )
        # One line of stdout so a script can pipe the path through without
        # parsing JSON. Stays minimal — the JSON file is the real output.
        print(str(output_path))
        return 0

    # Live mode (Sit 14). Live + --output is rejected: keeping the two
    # paths exclusive is simpler, and a caller wanting both can run the
    # tool twice (the bake is deterministic, so the JSON dump on a second
    # `--dry-run` invocation is a faithful record).
    if args.output is not None:
        print(
            "error: --output is only valid with --dry-run; "
            "re-run with --dry-run to capture the bake JSON.",
            file=sys.stderr,
        )
        return 2

    try:
        client = _build_client_from_env()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    forum = _build_forum(
        seed=args.seed,
        scale=args.scale,
        product_name=args.product,
        no_llm=args.no_llm,
    )

    try:
        result = push_forum(forum, client)
    except DiscourseAPIError as exc:
        print(f"error: Discourse push failed: {exc}", file=sys.stderr)
        return 1

    _print_push_summary(result, base_url=client.base_url)
    return 0


def _build_extension(
    *,
    base_seed: int,
    base_scale: str,
    product_name: str,
    extend_seed: int,
    add_topics_n: int,
    add_replies_n: int,
    release_burst_version: Optional[str],
    no_llm: bool,
) -> ForumExtension:
    """Run `extend_forum` with the right body provider.

    Mirrors `_build_forum`'s shape — argparse-bound entry that constructs a
    `GenerationSpec`, optionally wires an LLM body provider, and delegates
    to `pipeline.extend_forum`. The body-provider closure uses the SAME
    cache-naming convention as `init` so `extend --no-llm` runs are
    reproducible offline; an `init` cache is NOT shared with extension
    bodies because the extension's `(topic, post_number)` keys can collide
    with init keys (extension topic ids start above the base's max id, so
    in practice they don't, but the cache file is namespaced separately
    for clarity).
    """
    if product_name not in _PRODUCT_MODULES:
        raise ValueError(
            f"unknown product {product_name!r}; "
            f"expected one of {sorted(_PRODUCT_MODULES)}"
        )
    product = _PRODUCT_MODULES[product_name]
    base_spec = GenerationSpec(seed=base_seed, scale=base_scale, product=product)

    body_provider = (
        None
        if no_llm
        else _make_extension_body_provider(
            product_name, base_seed, base_scale, extend_seed, base_spec
        )
    )

    return extend_forum(
        base_spec,
        add_topics_n=add_topics_n,
        add_replies_n=add_replies_n,
        release_burst_version=release_burst_version,
        extend_seed=extend_seed,
        body_provider=body_provider,
        base_product_name=product_name,
    )


def _make_extension_body_provider(
    product_name: str,
    base_seed: int,
    base_scale: str,
    extend_seed: int,
    spec: GenerationSpec,
):
    """Mirror `_make_body_provider` for the extension path.

    Filename convention: `<product>-<provider>-extend-<base-seed>-<extend-seed>
    -<scale>.json`. Per the Sit-15 plan: extension body cache lives in its
    own namespace so `init` and `extend` runs don't share `(topic_id,
    post_number)` keys (and a future deletion of one cache doesn't
    invalidate the other).
    """
    provider = select_provider()
    cache_path = _CACHE_DIR / (
        f"{product_name}-{_provider_slug(provider)}-extend-"
        f"{base_seed}-{extend_seed}-{base_scale}.json"
    )
    cache = Cache(cache_path)
    bodies_rng = spec.rng("bodies")

    def _provider_callable(topic, post) -> str:
        return generate_body(
            topic,
            post,
            rng=bodies_rng,
            llm=provider,
            cache=cache,
            blocklist=blocklist_module,
        )

    return _provider_callable


def _run_extend(args: argparse.Namespace) -> int:
    """Handle `extend`. Sits 15-18 dry-run; Sit 15.1 live push."""
    # `--dry-run` and live mode are mutually exclusive (same shape as
    # `init`). Live + `--output` is rejected so the JSON-dump audit
    # trail is opt-in via a separate `--dry-run` invocation.
    if args.dry_run and args.output is None:
        print(
            "error: --output PATH is required when --dry-run is set.",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run and args.output is not None:
        print(
            "error: --output is only valid with --dry-run; "
            "re-run with --dry-run to capture the extension JSON.",
            file=sys.stderr,
        )
        return 2

    if args.add_topics < 0 or args.add_replies < 0:
        print(
            "error: --add-topics and --add-replies must be non-negative "
            f"(got --add-topics={args.add_topics}, "
            f"--add-replies={args.add_replies}).",
            file=sys.stderr,
        )
        return 2

    # Sit 18 — `--mixed` populates per-mode defaults for any flag the
    # user did NOT explicitly set. Explicit `--add-topics 0` / explicit
    # absence of `--release-burst` are indistinguishable from the
    # default at the argparse level (both look like `0` / `None`), so
    # `--mixed` overwrites the zero/None defaults rather than treating
    # them as "user said no". A user who wants to suppress one of the
    # mixed sub-flows can simply not pass `--mixed` and instead pass
    # the two flags they want.
    add_topics_n = args.add_topics
    add_replies_n = args.add_replies
    release_burst_version = args.release_burst
    if args.mixed:
        default_topics, default_replies = _MIXED_SCALE_DEFAULTS[args.scale]
        if add_topics_n == 0:
            add_topics_n = default_topics
        if add_replies_n == 0:
            add_replies_n = default_replies
        if release_burst_version is None:
            # Latest game-version tag — `crown_of_brine` orders the axis
            # chronologically so the last entry is the freshest release.
            product = _PRODUCT_MODULES[args.product]
            game_versions = product.TAG_POOL_BY_AXIS.get("game-version", [])
            if not game_versions:
                print(
                    f"error: product {args.product!r} has no game-version "
                    "tag axis — cannot pick a default --release-burst "
                    "for --mixed.",
                    file=sys.stderr,
                )
                return 2
            release_burst_version = game_versions[-1]

    has_burst = bool(release_burst_version)
    if add_topics_n + add_replies_n <= 0 and not has_burst:
        print(
            "error: at least one of --add-topics, --add-replies, or "
            "--release-burst must be active — an extension with none of "
            "them produces nothing. (Pass --mixed for scale-derived "
            "defaults across all three.)",
            file=sys.stderr,
        )
        return 2

    if has_burst:
        product = _PRODUCT_MODULES[args.product]
        valid_versions = product.TAG_POOL_BY_AXIS.get("game-version", [])
        if release_burst_version not in valid_versions:
            print(
                f"error: --release-burst {release_burst_version!r} is not in "
                f"the {args.product} game-version axis "
                f"{sorted(valid_versions)}.",
                file=sys.stderr,
            )
            return 2

    if args.dry_run:
        output_path: Path = args.output
        if output_path.parent and not output_path.parent.exists():
            print(
                f"error: parent directory {output_path.parent} does not exist.",
                file=sys.stderr,
            )
            return 2

        extension = _build_extension(
            base_seed=args.seed,
            base_scale=args.scale,
            product_name=args.product,
            extend_seed=args.extend_seed,
            add_topics_n=add_topics_n,
            add_replies_n=add_replies_n,
            release_burst_version=release_burst_version,
            no_llm=args.no_llm,
        )

        payload = dataclasses.asdict(extension)
        output_path.write_text(
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
                default=_serialize_value,
            )
            + "\n",
            encoding="utf-8",
        )
        print(str(output_path))
        return 0

    # Live mode (Sit 15.1). Build the extension + base forum offline, then
    # push the new artefacts via `push_extension`. The base forum is
    # re-baked from `(base_seed, base_scale, product)` so the live push
    # has the same `topic_id → title` map the extension was built
    # against — `push_extension` uses titles to resolve appended-reply
    # base topics on the live forum.
    try:
        client = _build_client_from_env()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    extension = _build_extension(
        base_seed=args.seed,
        base_scale=args.scale,
        product_name=args.product,
        extend_seed=args.extend_seed,
        add_topics_n=add_topics_n,
        add_replies_n=add_replies_n,
        release_burst_version=release_burst_version,
        no_llm=args.no_llm,
    )
    base_forum = _build_forum(
        seed=args.seed,
        scale=args.scale,
        product_name=args.product,
        no_llm=True,  # base re-bake never needs bodies — we don't push base posts
    )

    try:
        result = push_extension(extension, base_forum, client)
    except DiscourseAPIError as exc:
        print(f"error: Discourse extension push failed: {exc}", file=sys.stderr)
        return 1

    _print_push_summary(result, base_url=client.base_url)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return _run_init(args)
    if args.command == "extend":
        return _run_extend(args)
    # argparse marks `command` as required, so this is unreachable in normal
    # use — keep the explicit branch for type-narrowness.
    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; appeases the type checker
