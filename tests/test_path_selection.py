"""Path-based item selection: --path with a directory or a single file."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from jsub.client import JellyfinClient
from jsub.core import FetchConfig, FitConfig
from jsub.pipeline import PipelineConfig, collect_items

ITEMS = [
    {"Id": "m1", "Name": "Movie 1920", "Type": "Movie", "Path": "/data/movies/Movie (1920)/Movie (1920).mkv",
     "RunTimeTicks": 7200 * 10_000_000, "MediaSources": [{"Id": "s1"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
    {"Id": "m2", "Name": "Movie 2020", "Type": "Movie", "Path": "/data/movies/Movie (2020)/Movie (2020).mkv",
     "RunTimeTicks": 6000 * 10_000_000, "MediaSources": [{"Id": "s2"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
    {"Id": "e1", "Name": "S01E01", "Type": "Episode", "Path": "/data/series/Friends/Season 01/S01E01.mp4",
     "RunTimeTicks": 1800 * 10_000_000, "MediaSources": [{"Id": "s3"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
    {"Id": "e2", "Name": "S01E02", "Type": "Episode", "Path": "/data/series/Friends/Season 01/S01E02.mp4",
     "RunTimeTicks": 1800 * 10_000_000, "MediaSources": [{"Id": "s4"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/Items":
            q = parse_qs(urlparse(self.path).query)
            assert q.get("Recursive") in (["true"], ["false"])
            body = json.dumps({"Items": ITEMS, "TotalRecordCount": len(ITEMS)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture()
def client():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    httpd.shutdown()


def _pipe(path):
    return PipelineConfig(fetch=FetchConfig(), fit=FitConfig(), path=path)


def test_directory_selection_recursive(client):
    items = collect_items(client, _pipe("/data/series/Friends/Season 01"))
    assert sorted(i.id for i in items) == ["e1", "e2"]


def test_series_root_recursive(client):
    items = collect_items(client, _pipe("/data/series/Friends"))
    assert sorted(i.id for i in items) == ["e1", "e2"]


def test_single_movie_directory(client):
    items = collect_items(client, _pipe("/data/movies/Movie (1920)"))
    assert [i.id for i in items] == ["m1"]


def test_single_file_selection(client, tmp_path):
    f = tmp_path / "S01E02.mp4"
    f.touch()
    items = collect_items(client, _pipe("/data/series/Friends/Season 01/S01E02.mp4"))
    assert [i.id for i in items] == ["e2"]


def test_no_match(client):
    assert collect_items(client, _pipe("/elsewhere")) == []


def test_prefix_is_not_a_false_match(client):
    # /data/movies/Movie (1920) must not match /data/movies/Movie (2020)
    items = collect_items(client, _pipe("/data/movies/Movie (1920)"))
    assert "m2" not in [i.id for i in items]
