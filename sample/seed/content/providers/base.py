"""Shared provider interface re-export.

Hosted in `llm.py` as the canonical Protocol; this module re-exports it so
provider implementations import from a peer (`.base`) rather than reaching
back into the package root.
"""

from __future__ import annotations

from sample.seed.content.llm import Provider

__all__ = ["Provider"]
