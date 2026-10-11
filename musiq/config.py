"""Shared configuration resolution.

The output (music library) location is resolved in one place so it only has
to be set once. It can live in the repo-local ``.env`` file (``MUSIC_DIR=...``,
the file to edit to move your library), in ``~/.musiq/config`` alongside the
Spotify/Navidrome/Last.fm credentials, or in a ``MUSIC_DIR`` environment
variable.

Priority: environment variable → repo ``.env`` → ``~/.musiq/config`` →
``~/Music``.
"""

from __future__ import annotations

import os
from pathlib import Path

# musiq/config.py -> musiq/ -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_ENV = _REPO_ROOT / ".env"
_USER_CONFIG = Path.home() / ".musiq" / "config"

DEFAULT_MUSIC_DIR = "~/Music"

# Parsed KEY=VALUE files, cached by path and mtime so per-track lookups
# don't re-read the file from disk.
_cache: dict[Path, tuple[float, dict[str, str]]] = {}


def _parse(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file, ignoring blanks and ``#`` comments."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}

    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    cfg: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip("'\"")
    except OSError:
        return {}

    _cache[path] = (mtime, cfg)
    return cfg


def get(key: str, default: str | None = None) -> str | None:
    """Look up a setting: env var, then repo ``.env``, then ``~/.musiq/config``."""
    value = os.environ.get(key)
    if value:
        return value
    for path in (_REPO_ENV, _USER_CONFIG):
        value = _parse(path).get(key)
        if value:
            return value
    return default


def music_dir() -> Path:
    """Resolve the output music library directory.

    ``~`` is expanded against ``Path.home()`` (rather than
    ``os.path.expanduser``) so callers and tests that patch ``Path.home``
    stay consistent.
    """
    raw = (get("MUSIC_DIR") or DEFAULT_MUSIC_DIR).strip()
    if raw == "~":
        return Path.home()
    if raw.startswith("~/"):
        return Path.home() / raw[2:]
    return Path(raw)
