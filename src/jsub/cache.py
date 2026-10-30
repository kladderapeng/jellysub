"""Downloaded-subtitle cache: every fetched .srt is written here immediately,
so a later ASR/align failure never loses the download. Reused on later runs."""

from __future__ import annotations

import hashlib
import logging
import os
import re

log = logging.getLogger("jsub.cache")

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def cache_dir(base: str | None = None) -> str:
    base = base or os.environ.get("JSUB_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "jsub", "subs"
    )
    os.makedirs(base, exist_ok=True)
    return base


def cache_name(source: str, name: str, language: str, moviehash: str | None) -> str:
    safe = SAFE.sub("_", name or "sub")[:80]
    h = hashlib.sha1(f"{source}|{name}|{language}|{moviehash or ''}".encode()).hexdigest()[:10]
    return f"{safe}.{language}.{h}.srt"


def store(source: str, name: str, language: str, data: bytes,
          moviehash: str | None = None, base: str | None = None) -> str:
    path = os.path.join(cache_dir(base), cache_name(source, name, language, moviehash))
    with open(path, "wb") as f:
        f.write(data)
    if moviehash:
        import json

        idx = os.path.join(cache_dir(base), "index.json")
        try:
            with open(idx) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = {}
        entries[f"{moviehash}:{language}"] = path
        with open(idx, "w") as f:
            json.dump(entries, f)
    log.info("cached %s -> %s (%d bytes)", name, path, len(data))
    return path


def lookup(moviehash: str, language: str, base: str | None = None) -> str | None:
    """Return a cached srt for this moviehash+language if present (index sidecar)."""
    import json

    d = cache_dir(base)
    idx = os.path.join(d, "index.json")
    try:
        with open(idx) as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return None
    path = entries.get(f"{moviehash}:{language}")
    if path and os.path.exists(path):
        return path
    return None
