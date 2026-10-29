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
    {"Id": "m1", "Name": "Movie 1920", "Type": "Movie", "Path": "/srv/media/movies/Movie (1920)/Movie (1920).mkv",
     "RunTimeTicks": 7200 * 10_000_000, "MediaSources": [{"Id": "s1"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
    {"Id": "m2", "Name": "Movie 2020", "Type": "Movie", "Path": "/srv/media/movies/Movie (2020)/Movie (2020).mkv",
     "RunTimeTicks": 6000 * 10_000_000, "MediaSources": [{"Id": "s2"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
    {"Id": "e1", "Name": "S01E01", "Type": "Episode", "Path": "/srv/media/series/Friends/Season 01/S01E01.mp4",
     "RunTimeTicks": 1800 * 10_000_000, "MediaSources": [{"Id": "s3"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
    {"Id": "e2", "Name": "S01E02", "Type": "Episode", "Path": "/srv/media/series/Friends/Season 01/S01E02.mp4",
     "RunTimeTicks": 1800 * 10_000_000, "MediaSources": [{"Id": "s4"}],
     "MediaStreams": [{"Index": 0, "Type": "Video"}]},
]


LIB_ID = "lib-tv"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path == "/Library/VirtualFolders":
            return self._json([{"Name": "TV", "ItemId": LIB_ID, "CollectionType": "tvshows"},
                               {"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies"}])
        if path == "/Items":
            parent = (q.get("ParentId") or [""])[0]
            items = [i for i in ITEMS if (parent == LIB_ID) == i["Path"].startswith("/srv/media/series")]
            return self._json({"Items": items, "TotalRecordCount": len(items)})
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
    items = collect_items(client, _pipe("/srv/media/series/Friends/Season 01"))
    assert sorted(i.id for i in items) == ["e1", "e2"]


def test_series_root_recursive(client):
    items = collect_items(client, _pipe("/srv/media/series/Friends"))
    assert sorted(i.id for i in items) == ["e1", "e2"]


def test_single_movie_directory(client):
    items = collect_items(client, _pipe("/srv/media/movies/Movie (1920)"))
    assert [i.id for i in items] == ["m1"]


def test_single_file_selection(client, tmp_path):
    f = tmp_path / "S01E02.mp4"
    f.touch()
    items = collect_items(client, _pipe("/srv/media/series/Friends/Season 01/S01E02.mp4"))
    assert [i.id for i in items] == ["e2"]


def test_no_match(client):
    assert collect_items(client, _pipe("/srv/elsewhere")) == []


def test_prefix_is_not_a_false_match(client):
    # /data/movies/Movie (1920) must not match /data/movies/Movie (2020)
    items = collect_items(client, _pipe("/srv/media/movies/Movie (1920)"))
    assert "m2" not in [i.id for i in items]


def test_path_flag_accepted_after_subcommand():
    from jsub.__main__ import build_parser

    p = build_parser()
    a = p.parse_args(["run", "--path", "/data/series/Friends/Season 01"])
    assert a.command == "run" and a.path == "/data/series/Friends/Season 01"


def test_path_flag_before_subcommand_via_main(monkeypatch, capsys):
    import jsub.__main__ as m

    class FakeClient:
        def libraries(self):
            return [{"Name": "TV", "ItemId": LIB_ID, "CollectionType": "tvshows"}]

        def list_items(self, parent_id=None):
            return []

    monkeypatch.setattr(m, "build_client", lambda cfg: FakeClient())
    monkeypatch.setattr(m, "load_config", lambda args: {"url": "x", "api_key": "y"})
    rc = m.main(["--path", "/data/series/Friends", "scan"])
    assert rc == 0
    assert "0/0 items" in capsys.readouterr().out


def test_find_by_path_queries_each_library(client):
    items = client.find_items_by_path("/srv/media/series/Friends/Season 01")
    assert sorted(i.id for i in items) == ["e1", "e2"]
    items = client.find_items_by_path("/srv/media/movies/Movie (1920)")
    assert [i.id for i in items] == ["m1"]


def test_suffix_fallback_matches_different_mount_prefix(client):
    # Jellyfin sees /srv/media/... but user types /mnt/media/...
    items = client.find_items_by_path("/mnt/media/series/Friends/Season 01")
    assert sorted(i.id for i in items) == ["e1", "e2"]


def test_libraries_media_only_false_includes_other_types(client):
    libs = client.libraries(media_only=False)
    assert any(l.get("CollectionType") == "tvshows" for l in libs)
