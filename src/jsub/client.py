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
        return cls(
            id=str(d.get("Id", "")),
            name=str(d.get("Name", "")),
            language=str(d.get("Language", "")),
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

    def libraries(self) -> list[dict]:
        folders = self._get_json("/Library/VirtualFolders") or []
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

        Matches against the item Path field, so it works for both a single
        file (/data/series/Friends/Season 01/S01E01.mp4) and a directory
        (/data/movies/Movie (1920), matched recursively).
        """
        target = os.path.normpath(path).rstrip(os.sep)
        params = {
            "Recursive": "true" if recursive else "false",
            "IncludeItemTypes": ",".join(MEDIA_TYPES),
            "Fields": "MediaStreams,MediaSources,Path,RunTimeTicks,ProviderIds",
            "Limit": 500,
        }
        items: list[Item] = []
        start = 0
        while True:
            data = self._get_json("/Items", params={**params, "StartIndex": start}) or {}
            batch = [Item.from_dict(d) for d in data.get("Items", [])]
            items.extend(batch)
            total = int(data.get("TotalRecordCount", len(items)))
            start += len(batch)
            if not batch or start >= total:
                break
        out = []
        for it in items:
            p = it.path
            if not p:
                continue
            p = os.path.normpath(p).rstrip(os.sep)
            if p == target or (recursive and p.startswith(target + os.sep)):
                out.append(it)
        return out

    def get_item(self, item_id: str) -> Item:
        data = self._get_json(
            f"/Items/{item_id}",
            params={"Fields": "MediaStreams,MediaSources,Path,RunTimeTicks,ProviderIds"},
        )
        return Item.from_dict(data or {})

    def search_remote_subtitles(
        self, item_id: str, language: str | None = None
    ) -> list[RemoteSubtitle]:
        params = {"Language": language} if language else None
        data = self._get_json(f"/Items/{item_id}/Remote/Subtitles/Search", params=params)
        return [RemoteSubtitle.from_dict(d) for d in (data or [])]

    def download_remote_subtitle(self, item_id: str, subtitle_id: str) -> bool:
        status, _ = self._request(
            "POST", f"/Items/{item_id}/Remote/Subtitles/{subtitle_id}"
        )
        return status in (200, 204)

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
