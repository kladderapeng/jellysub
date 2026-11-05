"""Per-directory persistent state: `.jsub-state.json` in a scanned root.

Records, per media file (path relative to the root): the subtitle files that
were fetched/aligned, their source (opensubtitles-api / subliminal:<prov> /
sidecar / jellyfin-plugin), provider file id, fetch date, align stats, and a
user-settable quality rating ("bad" | "poor" | "ok" | "good" | "great" or 1-5).

Reruns skip files that already have a non-bad subtitle; files whose subtitles
are all rated bad get re-fetched with the bad provider ids excluded.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger("jsub.state")

STATE_FILENAME = ".jsub-state.json"
BAD_RATINGS = {"bad", "1", "2"}
GOOD_RATINGS = {"ok", "good", "great", "3", "4", "5"}


@dataclass
class SubEntry:
    file: str                      # path of the .srt (absolute or relative to root)
    source: str                    # opensubtitles-api | subliminal:<provider> | sidecar | jellyfin-plugin
    provider_id: str = ""          # opensubtitles file id / subliminal subtitle id
    language: str = "eng"
    fetched_at: float = field(default_factory=time.time)
    rating: str | None = None      # user-set quality; None = unrated (treated as ok)
    speed: float = 1.0
    offset_s: float = 0.0
    rms_s: float = 0.0
    anchors: int = 0

    def is_bad(self) -> bool:
        return (self.rating or "").lower() in BAD_RATINGS

    def to_dict(self) -> dict:
        return {
            "file": self.file, "source": self.source, "provider_id": self.provider_id,
            "language": self.language, "fetched_at": self.fetched_at, "rating": self.rating,
            "speed": self.speed, "offset_s": self.offset_s, "rms_s": self.rms_s,
            "anchors": self.anchors,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SubEntry":
        return cls(
            file=d["file"], source=d.get("source", ""), provider_id=str(d.get("provider_id", "")),
            language=d.get("language", "eng"), fetched_at=d.get("fetched_at", 0.0),
            rating=d.get("rating"), speed=d.get("speed", 1.0), offset_s=d.get("offset_s", 0.0),
            rms_s=d.get("rms_s", 0.0), anchors=d.get("anchors", 0),
        )


@dataclass
class MediaEntry:
    path: str                      # media file, relative to state root
    size: int = 0
    mtime: float = 0.0
    subtitles: list[SubEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "mtime": self.mtime,
                "subtitles": [s.to_dict() for s in self.subtitles]}

    @classmethod
    def from_dict(cls, d: dict) -> "MediaEntry":
        return cls(path=d["path"], size=d.get("size", 0), mtime=d.get("mtime", 0.0),
                   subtitles=[SubEntry.from_dict(s) for s in d.get("subtitles", [])])


class StateStore:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, STATE_FILENAME)
        self.entries: dict[str, MediaEntry] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.entries = {e["path"]: MediaEntry.from_dict(e) for e in data.get("files", [])}
        except (OSError, ValueError, KeyError):
            self.entries = {}

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "root": self.root,
                       "files": [e.to_dict() for e in self.entries.values()]}, f, indent=2)
        os.replace(tmp, self.path)

    def _rel(self, media_path: str) -> str:
        return os.path.relpath(os.path.abspath(media_path), self.root)

    def get(self, media_path: str) -> MediaEntry | None:
        return self.entries.get(self._rel(media_path))

    def record(self, media_path: str, sub: SubEntry) -> None:
        rel = self._rel(media_path)
        try:
            st = os.stat(media_path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        e = self.entries.get(rel) or MediaEntry(rel, size, mtime)
        e.size, e.mtime = size, mtime
        # replace existing entry for the same srt file, else append
        e.subtitles = [s for s in e.subtitles if s.file != sub.file]
        e.subtitles.append(sub)
        self.entries[rel] = e

    def set_rating(self, media_path: str, rating: str, language: str | None = None) -> int:
        """Rate all (or the language's) subtitles of a media file. Returns count changed."""
        e = self.get(media_path)
        if e is None:
            return 0
        n = 0
        for s in e.subtitles:
            if language is None or s.language == language:
                s.rating = rating
                n += 1
        if n:
            self.save()
        return n

    # ---- decisions used by the pipeline ----

    def has_good_sub(self, media_path: str, language: str) -> SubEntry | None:
        e = self.get(media_path)
        if e is None:
            return None
        for s in e.subtitles:
            if s.language[:2] == language[:2] and not s.is_bad():
                return s
        return None

    def bad_provider_ids(self, media_path: str, language: str) -> set[str]:
        e = self.get(media_path)
        if e is None:
            return set()
        return {s.provider_id for s in e.subtitles
                if s.language[:2] == language[:2] and s.is_bad() and s.provider_id}

    def media_changed(self, media_path: str) -> bool:
        e = self.get(media_path)
        if e is None:
            return True
        try:
            st = os.stat(media_path)
            return st.st_size != e.size or st.st_mtime != e.mtime
        except OSError:
            return True


def find_state_root(start: str) -> str:
    """Walk up from `start` to find an existing state file; else use `start`."""
    d = os.path.abspath(start)
    if os.path.isfile(d):
        d = os.path.dirname(d)
    while True:
        if os.path.exists(os.path.join(d, STATE_FILENAME)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.abspath(start) if os.path.isdir(start) else os.path.dirname(os.path.abspath(start))
        d = parent
