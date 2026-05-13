"""Minimal .env loader. Shared by demo server + eval harness.

Kept dependency-free (no python-dotenv) to avoid adding a tiny external
package for ~15 lines of parsing. Existing environment variables take
precedence — the file fills in defaults, it doesn't override.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(env_path: Path) -> None:
    """Read ``KEY=VAL`` pairs from ``env_path`` into ``os.environ``.

    No-op if the file doesn't exist. Inline ``#`` comments and surrounding
    quotes/whitespace are stripped. Keys already present in the environment
    are left untouched.
    """
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
