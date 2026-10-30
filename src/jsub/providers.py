"""OpenSubtitles REST API client: identify media by moviehash, list and fetch subs.

https://api.opensubtitles.com/api/v1  (API key via OPENSUBTITLES_API_KEY env or config)
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "jsub v0.1"


class ProviderError(Exception):
    pass


@dataclass
class SubFile:
    file_id: int
    file_name: str = ""
    language: str = ""
    format: str = ""
    download_count: int = 0
    rating: float = 0.0
    hearing_impaired: bool = False
    release: str = ""
    score: float = 0.0


@dataclass
class MovieHit:
    moviehash_match: bool
    feature_id: int | None = None
    title: str = ""
    year: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    files: list[SubFile] = field(default_factory=list)


class OpenSubtitles:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("OPENSUBTITLES_API_KEY", "")
        self.timeout = timeout
        self._token: str | None = None
        user = os.environ.get("OPENSUBTITLES_USERNAME")
        pw = os.environ.get("OPENSUBTITLES_PASSWORD")
        if user and pw and self.api_key:
            try:
                data = self._request(
                    "POST", "/login",
                    json_body={"username": user, "password": pw},
                )
                self._token = data.get("token")
            except ProviderError as e:
                raise ProviderError(f"opensubtitles login failed: {e}") from e

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None, raw: bool = False):
        if not self.api_key:
            raise ProviderError("OPENSUBTITLES_API_KEY is not set")
        url = BASE + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {"Api-Key": self.api_key, "User-Agent": USER_AGENT,
                   "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise ProviderError(f"{method} {path}: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise ProviderError(f"{method} {path}: {e.reason}") from e
        if raw:
            return body
        return json.loads(body.decode("utf-8")) if body else {}

    def search_by_hash(self, moviehash: str, languages: str = "en") -> list[MovieHit]:
        data = self._request(
            "GET", "/subtitles",
            params={"moviehash": moviehash, "languages": languages},
        )
        hits: list[MovieHit] = []
        for entry in data.get("data", []):
            files = [
                SubFile(
                    file_id=int(f.get("file_id", 0)),
                    file_name=str(f.get("attributes", {}).get("files", [{}])[0].get("file_name", "")) if f.get("attributes", {}).get("files") else str(f.get("attributes", {}).get("release", "")),
                    language=str(f.get("attributes", {}).get("language", "")),
                    format=str(f.get("attributes", {}).get("format", "")),
                    download_count=int(f.get("attributes", {}).get("download_count", 0) or 0),
                    rating=float(f.get("attributes", {}).get("ratings", 0) or 0),
                    hearing_impaired=bool(f.get("attributes", {}).get("hearing_impaired", 0)),
                    release=str(f.get("attributes", {}).get("release", "")),
                )
                for f in entry.get("attributes", {}).get("files", [])
            ]
            attrs = entry.get("attributes", {})
            hits.append(
                MovieHit(
                    moviehash_match=True,
                    feature_id=attrs.get("feature_id"),
                    title=str(attrs.get("feature_details", {}).get("title", "")),
                    year=attrs.get("feature_details", {}).get("year"),
                    imdb_id=str(attrs.get("imdb_id", "")) or None,
                    tmdb_id=attrs.get("tmdb_id"),
                    files=files,
                )
            )
        return hits

    def search_by_query(
        self,
        query: str,
        languages: str = "en",
        season: int | None = None,
        episode: int | None = None,
        parent_imdb_id: str | None = None,
        year: int | None = None,
        moviehash: str | None = None,
    ) -> list[MovieHit]:
        params: dict = {"query": query, "languages": languages}
        if season is not None:
            params["season_number"] = season
        if episode is not None:
            params["episode_number"] = episode
        if parent_imdb_id:
            params["parent_imdb_id"] = parent_imdb_id.lstrip("tt")
        if year:
            params["year"] = year
        if moviehash:
            params["moviehash"] = moviehash
        data = self._request("GET", "/subtitles", params=params)
        return self._parse_hits(data)

    def _parse_hits(self, data: dict) -> list[MovieHit]:
        hits: list[MovieHit] = []
        for entry in data.get("data", []):
            attrs = entry.get("attributes", {})
            files = [
                SubFile(
                    file_id=int(f.get("file_id", 0)),
                    file_name=str(f.get("attributes", {}).get("files", [{}])[0].get("file_name", "")) if f.get("attributes", {}).get("files") else str(f.get("attributes", {}).get("release", "")),
                    language=str(f.get("attributes", {}).get("language", "")),
                    format=str(f.get("attributes", {}).get("format", "")),
                    download_count=int(f.get("attributes", {}).get("download_count", 0) or 0),
                    rating=float(f.get("attributes", {}).get("ratings", 0) or 0),
                    hearing_impaired=bool(f.get("attributes", {}).get("hearing_impaired", 0)),
                    release=str(f.get("attributes", {}).get("release", "")),
                )
                for f in attrs.get("files", [])
            ]
            fd = attrs.get("feature_details", {})
            hits.append(
                MovieHit(
                    moviehash_match=bool(attrs.get("moviehash_match", False)),
                    feature_id=attrs.get("feature_id"),
                    title=str(fd.get("title", "")),
                    year=fd.get("year"),
                    imdb_id=str(attrs.get("imdb_id", "")) or None,
                    tmdb_id=attrs.get("tmdb_id"),
                    files=files,
                )
            )
        return hits

    def search_by_imdb(self, imdb_id: str, languages: str = "en") -> list[MovieHit]:
        data = self._request(
            "GET", "/subtitles",
            params={"imdb_id": imdb_id.lstrip("tt"), "languages": languages},
        )
        hits: list[MovieHit] = []
        for entry in data.get("data", []):
            attrs = entry.get("attributes", {})
            files = [
                SubFile(
                    file_id=int(f.get("file_id", 0)),
                    file_name=str(f.get("attributes", {}).get("files", [{}])[0].get("file_name", "")) if f.get("attributes", {}).get("files") else str(f.get("attributes", {}).get("release", "")),
                    language=str(f.get("attributes", {}).get("language", "")),
                    format=str(f.get("attributes", {}).get("format", "")),
                    download_count=int(f.get("attributes", {}).get("download_count", 0) or 0),
                    rating=float(f.get("attributes", {}).get("ratings", 0) or 0),
                    hearing_impaired=bool(f.get("attributes", {}).get("hearing_impaired", 0)),
                    release=str(f.get("attributes", {}).get("release", "")),
                )
                for f in entry.get("attributes", {}).get("files", [])
            ]
            hits.append(
                MovieHit(
                    moviehash_match=False,
                    feature_id=attrs.get("feature_id"),
                    title=str(attrs.get("feature_details", {}).get("title", "")),
                    year=attrs.get("feature_details", {}).get("year"),
                    imdb_id=str(attrs.get("imdb_id", "")) or None,
                    tmdb_id=attrs.get("tmdb_id"),
                    files=files,
                )
            )
        return hits

    def download(self, file_id: int) -> bytes:
        """Get the raw subtitle content for a file_id (gzip-decoded)."""
        data = self._request(
            "POST", "/download", json_body={"file_id": file_id}, raw=True
        )
        try:
            return gzip.decompress(data)
        except OSError:
            return data

    def best_files(self, hits: list[MovieHit], language: str, limit: int = 5) -> list[SubFile]:
        from .client import language_aliases

        want = {a.lower() for a in language_aliases(language)}
        cands: list[SubFile] = []
        for hit in hits:
            for f in hit.files:
                if (f.language or "").lower() in want and f.format.lower() in ("srt", "ass", "ssa"):
                    cands.append(f)
        cands.sort(key=lambda f: (f.rating, f.download_count), reverse=True)
        return cands[:limit]


def decode_subtitle(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
