"""Entry point for `python -m sample.seed`.

Thin shim around `cli.main` so the CLI can be invoked as
`uv run python -m sample.seed init …`. Logic lives in `cli.py` so tests can
exercise `main(argv=[…])` without going through process boundaries.
"""

from __future__ import annotations

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
