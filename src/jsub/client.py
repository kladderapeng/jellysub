"""Jellyfin HTTP API client (stdlib only)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

MEDIA_TYPES = ("Movie", "Episode")

ISO639_2_TO_3 = {
    "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
    "pt": "por", "nl": "nld", "pl": "pol", "ru": "rus", "ja": "jpn",
    "ko": "kor", "zh": "zho", "ar": "ara", "hi": "hin", "sv": "swe",
    "da": "dan", "fi": "fin", "no": "nor", "cs": "ces", "tr": "tur",
    "el": "ell", "he": "heb", "hu": "hun", "ro": "ron", "uk": "ukr",
    "vi": "vie", "th": "tha", "id": "ind", "bg": "bul", "hr": "hrv",
    "sk": "slk", "sl": "slv", "et": "est", "lv": "lav", "lt": "lit",
}


ISO639_3_TO_2 = {v: k for k, v in ISO639_2_TO_3.items()}


def iso639_2_to_3(code: str) -> str:
    return ISO639_2_TO_3.get(code.strip().lower(), code.strip().lower())


def language_aliases(code: str) -> set[str]:
    """All spellings (2- and 3-letter) that match the given language code."""
    code = code.strip().lower()
    return {code, iso639_2_to_3(code), ISO639_3_TO_2.get(code, code)}


class JellyfinError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class MediaStream:
    index: int
    type: str
    language: str | None = None
    codec: str | None = None
    is_external: bool = False
    is_forced: bool = False
    is_hearing_impaired: bool = False
    title: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "MediaStream":
        return cls(
            index=int(d.get("Index", -1)),
            type=str(d.get("Type", "")),
            language=d.get("Language"),
            codec=d.get("Codec"),
            is_external=bool(d.get("IsExternal", False)),
            is_forced=bool(d.get("IsForced", False)),
            is_hearing_impaired=bool(d.get("IsHearingImpaired", False)),
            title=d.get("Title"),
        )


@dataclass
class Item:
    id: str
    name: str
    type: str
    path: str | None = None
    runtime_ticks: int | None = None
    provider_ids: dict = field(default_factory=dict)
    media_streams: list[MediaStream] = field(default_factory=list)
    media_source_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        sources = d.get("MediaSources") or []
        source_id = sources[0].get("Id") if sources else None
        return cls(
            id=str(d.get("Id", "")),
            name=str(d.get("Name", "")),
            type=str(d.get("Type", "")),
            path=d.get("Path"),
            runtime_ticks=d.get("RunTimeTicks"),
            provider_ids=dict(d.get("ProviderIds") or {}),
            media_streams=[
                MediaStream.from_dict(s)
                for s in (d.get("MediaStreams") or [])
            ],
            media_source_id=source_id,
        )

    @property
    def runtime_ms(self) -> int | None:
        if self.runtime_ticks is None:
            return None
        return int(self.runtime_ticks / 10_000)

    def subtitle_streams(self) -> list[MediaStream]:
        return [s for s in self.media_streams if s.type == "Subtitle"]

    def has_subtitle(self, language: str) -> bool:
        wanted = language_aliases(language)
        return any(
            (s.language or "").lower() in wanted for s in self.subtitle_streams()
        )


@dataclass
class RemoteSubtitle:
    id: str
    name: str = ""
    language: str = ""
    format: str = ""
    provider_name: str = ""
    score: float | None = None
    download_count: int | None = None
    community_rating: float | None = None
    is_hearing_impaired: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "RemoteSubtitle":
        lang = (
            d.get("Language")
            or d.get("LanguageCode")
            or d.get("ThreeLetterISOLanguageName")
            or ""
        )
        return cls(
            id=str(d.get("Id", "")),
            name=str(d.get("Name", "")),
            language=str(lang),
            format=str(d.get("Format", "")).lower(),
            provider_name=str(d.get("ProviderName", "")),
            score=d.get("Score"),
            download_count=d.get("DownloadCount"),
            community_rating=d.get("CommunityRating"),
            is_hearing_impaired=bool(d.get("IsHearingImpaired", False)),
        )


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes]:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "Accept": "application/json",
        }
        data = raw_body
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:500].decode("utf-8", "replace")
            raise JellyfinError(
                f"{method} {path} failed: HTTP {e.code} {body}", status=e.code
            ) from e
        except urllib.error.URLError as e:
            raise JellyfinError(f"{method} {path} failed: {e.reason}") from e

    def _get_json(self, path: str, params: dict | None = None):
        status, body = self._request("GET", path, params=params)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def server_info(self) -> dict:
        return self._get_json("/System/Info/Public")

    def plugins(self) -> list[dict]:
        return self._get_json("/Plugins") or []

    def libraries(self, media_only: bool = True) -> list[dict]:
        folders = self._get_json("/Library/VirtualFolders") or []
        if not media_only:
            return folders
        return [
            f
            for f in folders
            if str(f.get("CollectionType", "")).lower() in ("movies", "tvshows", "mixed")
        ]

    def list_items(
        self,
        parent_id: str | None = None,
        include_types: tuple[str, ...] = MEDIA_TYPES,
        limit: int = 500,
    ) -> list[Item]:
        items: list[Item] = []
        start = 0
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": ",".join(include_types),
                "Fields": "MediaStreams,Path,RunTimeTicks,ProviderIds",
                "ParentId": parent_id,
                "StartIndex": start,
                "Limit": limit,
            }
            data = self._get_json("/Items", params) or {}
            batch = [Item.from_dict(d) for d in data.get("Items", [])]
            items.extend(batch)
            total = int(data.get("TotalRecordCount", len(items)))
            start += len(batch)
            if not batch or start >= total:
                break
        return items

    def find_items_by_path(self, path: str, recursive: bool = True) -> list[Item]:
        """Find items whose media file lives at or under the given local path.

        Matches against the item Path field: exact for a file, path-boundary
        prefix for a directory. Iterates ALL virtual folders with parentId
        (a global /Items without parentId returns nothing on many server
        versions, and collection-type filtering can hide the library). If no
        prefix match is found, falls back to matching on the path's trailing
        components, so a differently-mounted prefix still works.
        """
        import logging

        log = logging.getLogger("jsub.client")
        target = os.path.normpath(path).rstrip(os.sep)
        target_parts = [p for p in target.split(os.sep) if p]
        items: list[Item] = []
        for lib in self.libraries(media_only=False):
            lib_id = lib.get("ItemId") or lib.get("Id") or ""
            lib_name = lib.get("Name", lib_id)
            try:
                batch = self.list_items(parent_id=lib_id or None)
            except JellyfinError as e:
                log.warning("library %s scan failed: %s", lib_name, e)
                continue
            with_path = sum(1 for i in batch if i.path)
            log.info("library %s: %d items, %d with Path", lib_name, len(batch), with_path)
            items.extend(batch)
        log.info("path lookup %s: %d candidate items with Path", target, sum(1 for i in items if i.path))

        def norm(p: str) -> str:
            return os.path.normpath(p).rstrip(os.sep)

        prefix_hits = [
            it for it in items
            if it.path and (
                norm(it.path) == target
                or (recursive and norm(it.path).startswith(target + os.sep))
            )
        ]
        if prefix_hits:
            return prefix_hits
        # fallback 1: the item path must CONTAIN the target's components as a
        # contiguous run, ignoring the first component of the target (the
        # mount-point name often differs: /mnt vs /srv vs /data)
        n = len(target_parts)
        suffix_hits = []
        for it in items:
            if not it.path:
                continue
            parts = [p for p in norm(it.path).split(os.sep) if p]
            for skip in (0, 1):
                needle = target_parts[skip:]
                m = len(needle)
                if m == 0:
                    continue
                for start in range(0, len(parts) - m + 1):
                    if parts[start : start + m] == needle:
                        suffix_hits.append(it)
                        break
                else:
                    continue
                break
        if suffix_hits:
            log.info("path lookup %s: matched %d items by path-run", target, len(suffix_hits))
            return suffix_hits
        # fallback 2: match on the target's last 2 components (e.g.
        # "Season 01/S01E01.mp4" or "Friends/Season 01") anywhere in the path
        tail = target_parts[-2:] if n >= 2 else target_parts
        tail_hits = []
        for it in items:
            if not it.path:
                continue
            parts = [p for p in norm(it.path).split(os.sep) if p]
            m = len(tail)
            for start in range(0, len(parts) - m + 1):
                if parts[start : start + m] == tail:
                    tail_hits.append(it)
                    break
        if tail_hits:
            log.info("path lookup %s: matched %d items by trailing components %s", target, len(tail_hits), tail)
            return tail_hits
        sample = [it.path for it in items if it.path][:5]
        log.info("path lookup %s: NO match; sample item paths: %s", target, sample)
        return []

    def get_item(self, item_id: str) -> Item:
        data = self._get_json(
            f"/Items/{item_id}",
            params={"Fields": "MediaStreams,MediaSources,Path,RunTimeTicks,ProviderIds"},
        )
        return Item.from_dict(data or {})

    def search_remote_subtitles(
        self, item_id: str, language: str | None = None
    ) -> list[RemoteSubtitle]:
        lang = (language or "eng")
        routes = [
            f"/Items/{item_id}/RemoteSearch/Subtitles/{lang}",
            f"/Items/{item_id}/Remote/Subtitles/Search",
            f"/Items/{item_id}/RemoteSearch/Subtitles",
        ]
        last_err: JellyfinError | None = None
        for route in routes:
            try:
                data = self._get_json(route, params={"Language": lang})
                return [RemoteSubtitle.from_dict(d) for d in (data or [])]
            except JellyfinError as e:
                last_err = e
                if e.status not in (404, 405):
                    raise
        raise last_err if last_err else JellyfinError("remote subtitle search failed")

    def download_remote_subtitle(self, item_id: str, subtitle_id: str) -> bool:
        routes = [
            f"/Items/{item_id}/Remote/Subtitles/{subtitle_id}",
            f"/Items/{item_id}/RemoteSearch/Subtitles/{subtitle_id}",
        ]
        last_err: JellyfinError | None = None
        for route in routes:
            try:
                status, _ = self._request("POST", route)
                return status in (200, 204)
            except JellyfinError as e:
                last_err = e
                if e.status not in (404, 405):
                    raise
        if last_err:
            raise last_err
        return False

    def download_subtitle_stream(
        self, item_id: str, media_source_id: str, index: int, format: str = "srt"
    ) -> bytes:
        status, body = self._request(
            "GET",
            f"/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{format}",
        )
        return body

    def upload_subtitle(
        self,
        item_id: str,
        data: bytes,
        language: str,
        format: str = "srt",
        is_forced: bool = False,
        is_hearing_impaired: bool = False,
    ) -> None:
        body = {
            "Data": base64.b64encode(data).decode("ascii"),
            "Format": format,
            "Language": language,
            "IsForced": is_forced,
            "IsHearingImpaired": is_hearing_impaired,
        }
        self._request("POST", f"/Videos/{item_id}/Subtitles", json_body=body)

    def refresh_item(self, item_id: str) -> None:
        self._request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={"Recursive": "true", "MetadataRefreshMode": "FullRefresh",
                    "ImageRefreshMode": "None", "ReplaceAllMetadata": "false"},
        )
