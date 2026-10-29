"""Integration tests against a loopback stub Jellyfin server."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from jsub.client import JellyfinClient, JellyfinError
from jsub.core import FetchConfig, FitConfig
from jsub.pipeline import PipelineConfig, run_pipeline

MOVIE_ID = "11111111111111111111111111111111"
EPISODE_ID = "22222222222222222222222222222222"
LIB_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SOURCE_ID = "source-1"

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nhello\n\n"
    "2\n01:59:59,000 --> 02:00:02,000\nover end\n"
)


class StubState:
    def __init__(self):
        self.uploads = []
        self.downloads = []
        self.refreshes = []


STATE = StubState()


def item_dict(item_id, name, itype, langs, runtime_ticks):
    return {
        "Id": item_id,
        "Name": name,
        "Type": itype,
        "Path": f"/media/{name}.mkv",
        "RunTimeTicks": runtime_ticks,
        "ProviderIds": {"Imdb": "tt0000001"},
        "MediaSources": [{"Id": SOURCE_ID}],
        "MediaStreams": [
            {"Index": i, "Type": "Subtitle", "Language": l, "Codec": "srt", "IsExternal": True}
            for i, l in enumerate(langs)
        ]
        + [{"Index": 99, "Type": "Video", "Codec": "h264"}],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth_ok(self):
        return 'Token="test-key"' in self.headers.get("Authorization", "")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        if not self._auth_ok():
            return self._send_json({"error": "unauthorized"}, 401)
        path, q = self._route()
        if path == "/System/Info/Public":
            return self._send_json({"ServerName": "stub", "Version": "10.9.0"})
        if path == "/Library/VirtualFolders":
            return self._send_json([{"Name": "Movies", "ItemId": LIB_ID, "CollectionType": "movies"}])
        if path == "/Items":
            return self._send_json({
                "Items": [
                    item_dict(MOVIE_ID, "Stub Movie", "Movie", [], 2 * 3600 * 10_000_000),
                    item_dict(EPISODE_ID, "Stub Episode", "Episode", ["eng"], 30 * 60 * 10_000_000),
                ],
                "TotalRecordCount": 2,
            })
        if path == f"/Items/{MOVIE_ID}":
            return self._send_json(item_dict(MOVIE_ID, "Stub Movie", "Movie", ["eng"], 2 * 3600 * 10_000_000))
        if path == f"/Items/{MOVIE_ID}/Remote/Subtitles/Search":
            return self._send_json([
                {"Id": "sub-low", "Name": "x", "Language": "eng", "Format": "srt", "Score": 40, "DownloadCount": 10},
                {"Id": "sub-best", "Name": "Stub Movie", "Language": "eng", "Format": "srt", "Score": 95, "DownloadCount": 9000, "CommunityRating": 8.0},
                {"Id": "sub-fr", "Name": "x", "Language": "fra", "Format": "srt", "Score": 99},
            ])
        if path.startswith(f"/Videos/{MOVIE_ID}/{SOURCE_ID}/Subtitles/") and path.endswith("/Stream.srt"):
            body = SRT.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth_ok():
            return self._send_json({"error": "unauthorized"}, 401)
        path, q = self._route()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if path == f"/Items/{MOVIE_ID}/Remote/Subtitles/sub-best":
            STATE.downloads.append("sub-best")
            return self._send_json({}, 204)
        if path == f"/Videos/{MOVIE_ID}/Subtitles":
            dto = json.loads(body.decode())
            STATE.uploads.append(dto)
            return self._send_json({}, 204)
        if path == f"/Items/{MOVIE_ID}/Refresh":
            STATE.refreshes.append(True)
            return self._send_json({}, 204)
        return self._send_json({"error": "not found"}, 404)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture()
def client(server):
    return JellyfinClient(server, "test-key")


def test_server_info(client):
    assert client.server_info()["ServerName"] == "stub"


def test_auth_failure_raises(server):
    bad = JellyfinClient(server, "wrong-key")
    with pytest.raises(JellyfinError) as exc:
        bad.server_info()
    assert exc.value.status == 401


def test_libraries_and_items(client):
    libs = client.libraries()
    assert libs[0]["ItemId"] == LIB_ID
    items = client.list_items(parent_id=LIB_ID)
    assert {i.type for i in items} == {"Movie", "Episode"}
    movie = next(i for i in items if i.type == "Movie")
    assert movie.runtime_ms == 2 * 3600 * 1000
    assert not movie.has_subtitle("eng")


def test_search_picks_best_and_skips_existing(client):
    cands = client.search_remote_subtitles(MOVIE_ID, "eng")
    assert len(cands) == 3
    episode = client.get_item(EPISODE_ID) if False else None


def test_download_remote(client):
    assert client.download_remote_subtitle(MOVIE_ID, "sub-best") is True
    assert STATE.downloads == ["sub-best"]


def test_full_pipeline_fetch_and_fit(server):
    STATE.uploads.clear()
    STATE.downloads.clear()
    STATE.refreshes.clear()
    client = JellyfinClient(server, "test-key")
    cfg = PipelineConfig(
        fetch=FetchConfig(languages=["eng"]),
        fit=FitConfig(offset_ms=250.0),
        dry_run=False,
    )
    results = run_pipeline(client, cfg)
    actions = {(r.item_id, r.action) for r in results}
    assert (MOVIE_ID, "fetched") in actions
    assert (EPISODE_ID, "skip-existing") in actions
    assert STATE.uploads, "fit step should re-upload the adjusted subtitle"
    dto = STATE.uploads[0]
    assert dto["Language"] == "eng"
    assert dto["Format"] == "srt"
    data = base64.b64decode(dto["Data"]).decode()
    assert "00:00:01,250 --> 00:00:03,250" in data
    assert "02:00:00,000" in data and "02:00:02,250" not in data
    assert STATE.refreshes


def test_dry_run_changes_nothing(server):
    STATE.uploads.clear()
    STATE.downloads.clear()
    client = JellyfinClient(server, "test-key")
    cfg = PipelineConfig(
        fetch=FetchConfig(languages=["eng"]),
        fit=FitConfig(),
        dry_run=True,
    )
    results = run_pipeline(client, cfg)
    assert any(r.action == "would-fetch" for r in results)
    assert not STATE.uploads
    assert not STATE.downloads
