"""Top-level subcommand dispatcher for `discourse-explorer`.

Routes `discourse-explorer <subcommand> [args...]` to the matching
module's existing `main()`. Each module continues to be runnable
directly (`python -m discourse_explorer.stats ...`) — the dispatcher is
purely additive.
"""

import sys
from importlib import import_module

# subcommand -> (module path, one-line description)
COMMANDS: dict[str, tuple[str, str]] = {
    "scrape":         ("discourse_explorer.scraper",        "Scrape a Discourse forum"),
    "discover-types": ("discourse_explorer.discover_types", "Discover entity-type vocabulary from scraped data"),
    "stats":          ("discourse_explorer.stats",          "DuckDB analytics over the scraped JSON"),
    "query":          ("discourse_explorer.query",          "GraphRAG index / ask"),
    "visualize":      ("discourse_explorer.visualize",      "Generate the interactive HTML graph"),
}

# user-facing alias -> canonical command name
ALIASES: dict[str, str] = {
    "viz": "visualize",
}


def _print_usage(file=None) -> None:
    # Resolve sys.stdout lazily so tests that patch sys.stdout work.
    if file is None:
        file = sys.stdout
    print("Usage: discourse-explorer <command> [args...]\n", file=file)
    print("Commands:", file=file)
    width = max(len(c) for c in COMMANDS)
    for cmd, (_module, desc) in COMMANDS.items():
        print(f"  {cmd.ljust(width)}  {desc}", file=file)
    if ALIASES:
        print("\nAliases:", file=file)
        for alias, target in ALIASES.items():
            print(f"  {alias.ljust(width)}  → {target}", file=file)
    print("\nRun `discourse-explorer <command> --help` for command-specific help.", file=file)


def main() -> None:
    """Dispatch to the matching module's main() based on argv[1]."""
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        _print_usage()
        return

    raw = sys.argv[1]
    cmd = ALIASES.get(raw, raw)
    if cmd not in COMMANDS:
        print(f"discourse-explorer: unknown command: {raw!r}\n", file=sys.stderr)
        _print_usage(file=sys.stderr)
        sys.exit(2)

    module_path, _ = COMMANDS[cmd]
    # Reshape argv so the inner module's argparse uses a sensible prog name
    # in usage strings and error messages.
    sys.argv = [f"discourse-explorer {cmd}", *sys.argv[2:]]
    import_module(module_path).main()


if __name__ == "__main__":
    main()
