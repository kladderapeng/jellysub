"""verify_item without an OpenSubtitles API key: candidates via the Jellyfin
plugin search, sidecar SRT read back from the media directory."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

import jsub.verify as verify_mod
from jsub.asr import Word
from jsub.client import Item, JellyfinClient
from jsub.verify import VerifyConfig, verify_item

ITEM_ID = "44444444444444444444444444444444"

SRT_RIGHT = (
    "1\n00:00:10,000 --> 00:00:14,000\nwe are going to test this thing right now\n\n"
    "2\n00:00:40,000 --> 00:00:44,000\nanother line of speech for the second cue\n\n"
    "3\n00:01:10,000 --> 00:01:14,000\na third distinct passage spoken aloud\n\n"
    "4\n00:01:40,000 --> 00:01:44,000\nand a fourth one for more anchors\n"
)

CUES = [
    (10.0, "we are going to test this thing right now"),
    (40.0, "another line of speech for the second cue"),
    (70.0, "a third distinct passage spoken aloud"),
    (100.0, "and a fourth one for more anchors"),
]


def fake_words(*args, **kwargs):
    words = []
    for sub_start_s, text in CUES:
        t = sub_start_s + 0.7
        for w in text.split():
            words.append(Word(start=t, end=t + 0.4, text=w))
            t += 0.5
    return words


class Handler(BaseHTTPRequestHandler):
    uploads = []

    def log_message(self, *a):
        pass

    def _send(self, obj=None, status=200):
        body = json.dumps(obj or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == f"/Items/{ITEM_ID}/Remote/Subtitles/Search":
            return self._send([
                {"Id": "sub1", "Name": "Movie.en.srt", "Language": "eng", "Format": "srt", "Score": 90, "DownloadCount": 500},
            ])
        return self._send({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        if path == f"/Items/{ITEM_ID}/Remote/Subtitles/sub1":
            return self._send({}, 204)
        if path == f"/Videos/{ITEM_ID}/Subtitles":
            Handler.uploads.append(json.loads(body.decode()))
            return self._send({}, 204)
        if path == f"/Items/{ITEM_ID}/Refresh":
            return self._send({}, 204)
        return self._send({}, 404)


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_plugin_fallback_without_api_key(server, monkeypatch, tmp_path):
    Handler.uploads.clear()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media = media_dir / "Movie.mkv"
    media.write_bytes(b"\0" * 300_000)
    sidecar = media_dir / "Movie.en.srt"
    sidecar.write_text(SRT_RIGHT)
    item = Item(id=ITEM_ID, name="Movie", type="Movie", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={"Imdb": "tt9"},
                media_source_id="s")
    client = JellyfinClient(server, "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    cfg = VerifyConfig(language="eng")
    res = verify_item(client, item, cfg, provider=None)  # no API key at all
    assert res.action == "aligned"
    assert "jellyfin-plugin" in res.chosen
    assert res.anchors >= 3
    assert Handler.uploads, "corrected subtitle should be uploaded"


def test_no_candidates_anywhere(server, monkeypatch, tmp_path):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"\0" * 300_000)
    item = Item(id=ITEM_ID, name="Movie", type="Movie", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={}, media_source_id="s")
    client = JellyfinClient(server, "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    res = verify_item(client, item, VerifyConfig(language="eng"), provider=None)
    assert res.action == "no-match"
    assert "OPENSUBTITLES_API_KEY" in res.detail


def test_plugin_search_404_is_graceful(server, monkeypatch, tmp_path):
    class H404(T.Handler if False else object):
        pass
    # reuse existing stub but make search 404: easiest is a new handler
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status):
            b = _json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def do_GET(self):
            if _up(self.path).path == f"/Items/{ITEM_ID}/Remote/Subtitles/Search":
                return self._j({"error": "no provider"}, 404)
            return self._j({}, 404)
        def do_POST(self):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"\0" * 300_000)
    item = Item(id=ITEM_ID, name="Movie", type="Movie", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={}, media_source_id="s")
    client = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    res = verify_item(client, item, VerifyConfig(language="eng"), provider=None)
    assert res.action == "no-match"
    assert "Open Subtitles plugin" in res.detail or "OPENSUBTITLES_API_KEY" in res.detail
    httpd.shutdown()


def test_legacy_remotesubtitles_route_fallback(monkeypatch, tmp_path):
    """Server only implements the legacy /RemoteSearch/Subtitles route."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def do_GET(self):
            p = _up(self.path).path
            if p == f"/Items/{ITEM_ID}/RemoteSearch/Subtitles/eng":
                return self._j([{"Id": "legacy1", "Name": "Movie.en.srt", "Language": "eng", "Format": "srt", "Score": 90}])
            if p == f"/Items/{ITEM_ID}/RemoteSearch/Subtitles":
                return self._j([{"Id": "legacy1", "Name": "Movie.en.srt", "Language": "eng", "Format": "srt", "Score": 90}])
            if p.endswith("/Remote/Subtitles/Search"):
                return self._j({"error": "not found"}, 404)
            return self._j({}, 404)
        def do_POST(self):
            p = _up(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            if length: self.rfile.read(length)
            if p == f"/Items/{ITEM_ID}/RemoteSearch/Subtitles/legacy1" or p == f"/Videos/{ITEM_ID}/Subtitles" or p == f"/Items/{ITEM_ID}/Refresh":
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    media_dir = tmp_path / "media"; media_dir.mkdir()
    media = media_dir / "Movie.mkv"; media.write_bytes(b"\0" * 300_000)
    (media_dir / "Movie.en.srt").write_text(SRT_RIGHT)
    item = Item(id=ITEM_ID, name="Movie", type="Movie", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={}, media_source_id="s")
    client = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    res = verify_item(client, item, VerifyConfig(language="eng"), provider=None)
    assert res.action == "aligned"
    assert "jellyfin-plugin" in res.chosen
    httpd.shutdown()


def test_sidecar_fallback_when_no_remote_candidates(server, monkeypatch, tmp_path):
    """No API key, plugin search 404s, but a sidecar exists next to the media."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            self._j({}, 404)
        def do_POST(self):
            p = _up(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            if length: self.rfile.read(length)
            if p == f"/Videos/{ITEM_ID}/Subtitles" or p == f"/Items/{ITEM_ID}/Refresh":
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    d = tmp_path / "media"; d.mkdir()
    media = d / "Movie.mkv"; media.write_bytes(b"\0" * 300_000)
    (d / "Movie.en.srt").write_text(SRT_RIGHT)
    item = Item(id=ITEM_ID, name="Movie", type="Movie", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={}, media_source_id="s")
    client = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    res = verify_item(client, item, VerifyConfig(language="eng"), provider=None)
    assert res.action == "aligned"
    assert "sidecar" in res.chosen
    httpd.shutdown()


def test_plugin_candidates_with_missing_language_accepted(server, monkeypatch, tmp_path):
    """Plugin returns candidates without a Language field (as seen on 12.4)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            p = _up(self.path).path
            if p.startswith(f"/Items/{ITEM_ID}/RemoteSearch/Subtitles"):
                return self._j([{"Id": "nolang1", "Name": "Mum.S01E01.en.srt", "Format": "srt", "Score": 80, "DownloadCount": 100}])
            return self._j({}, 404)
        def do_POST(self):
            p = _up(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            if length: self.rfile.read(length)
            if "RemoteSearch/Subtitles/nolang1" in p or p == f"/Videos/{ITEM_ID}/Subtitles" or p == f"/Items/{ITEM_ID}/Refresh":
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    d = tmp_path / "media"; d.mkdir()
    media = d / "Movie.mkv"; media.write_bytes(b"\0" * 300_000)
    (d / "Movie.en.srt").write_text(SRT_RIGHT)
    item = Item(id=ITEM_ID, name="Movie", type="Movie", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={}, media_source_id="s")
    client = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    res = verify_item(client, item, VerifyConfig(language="eng"), provider=None)
    assert res.action == "aligned", res.detail
    assert "jellyfin-plugin" in res.chosen
    httpd.shutdown()


def test_plugin_readback_via_api_stream(server, monkeypatch, tmp_path):
    """Downloaded sub is read via /Videos/.../Stream.srt, not from disk."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            p = _up(self.path).path
            if p.startswith(f"/Items/{ITEM_ID}/RemoteSearch/Subtitles"):
                return self._j([{"Id": "s1", "Name": "x.srt", "Format": "srt", "Language": "en", "Score": 90}])
            if p == f"/Items/{ITEM_ID}":
                return self._j({"Id": ITEM_ID, "Name": "Movie", "Type": "Episode", "Path": "/nonexistent/Movie.mkv",
                                "RunTimeTicks": 200 * 10_000_000, "ProviderIds": {},
                                "MediaSources": [{"Id": "src1"}],
                                "MediaStreams": [{"Index": 3, "Type": "Subtitle", "Language": "eng", "Codec": "srt", "IsExternal": True}]})
            if p.endswith("/Stream.srt"):
                b = SRT_RIGHT.encode()
                self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
                return
            return self._j({}, 404)
        def do_POST(self):
            p = _up(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            if length: self.rfile.read(length)
            if "RemoteSearch/Subtitles/s1" in p or p == f"/Videos/{ITEM_ID}/Subtitles" or p == f"/Items/{ITEM_ID}/Refresh":
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    media = tmp_path / "Movie.mkv"; media.write_bytes(b"\0" * 300_000)
    item = Item(id=ITEM_ID, name="Movie", type="Episode", path=str(media),
                runtime_ticks=200 * 10_000_000, provider_ids={}, media_source_id="src1")
    client = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    res = verify_item(client, item, VerifyConfig(language="eng"), provider=None)
    assert res.action == "aligned", res.detail
    httpd.shutdown()


def test_get_item_with_user_id(monkeypatch, tmp_path):
    """GET /Items/{id} is called with UserId on 12.x (bare call 400s)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up, parse_qs as _pq

    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            u = _up(self.path)
            if u.path == "/Users/Me":
                return self._j({"Id": "user-1", "Name": "kladderapeng"})
            if u.path == f"/Items/{ITEM_ID}":
                q = _pq(u.query)
                seen.append(q.get("UserId"))
                if not q.get("UserId"):
                    return self._j({"error": "Error processing request"}, 400)
                return self._j({"Id": ITEM_ID, "Name": "Movie", "Type": "Movie", "Path": "/x.mkv",
                                "RunTimeTicks": 100, "MediaSources": [{"Id": "s"}],
                                "MediaStreams": [{"Index": 0, "Type": "Video"}]})
            return self._j({}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    item = c.get_item(ITEM_ID, user_id=c.current_user_id())
    assert item.id == ITEM_ID
    assert seen == [["user-1"]]
    httpd.shutdown()


def test_get_item_users_route_fallback(tmp_path):
    """12.x: /Items/{id}?UserId 400s but /Users/{uid}/Items/{id} works."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up, parse_qs as _pq

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            u = _up(self.path)
            if u.path == "/Users/Me":
                return self._j({"Id": "user-1"})
            if u.path == f"/Items/{ITEM_ID}":
                return self._j({"error": "Error processing request"}, 400)
            if u.path == f"/Users/user-1/Items/{ITEM_ID}":
                return self._j({"Id": ITEM_ID, "Name": "Movie", "Type": "Movie", "Path": "/x.mkv",
                                "RunTimeTicks": 100, "MediaSources": [{"Id": "s"}],
                                "MediaStreams": [{"Index": 0, "Type": "Video"}]})
            return self._j({}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    item = c.get_item(ITEM_ID, user_id=c.current_user_id())
    assert item.id == ITEM_ID and item.media_source_id == "s"
    httpd.shutdown()


def test_user_id_fallback_via_users_list_and_listing_route(tmp_path):
    """/Users/Me 403s (some API keys), /Users works, /Items/{id} 400s, /Items?Ids= works."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading, json as _json
    from urllib.parse import urlparse as _up

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _j(self, obj, status=200):
            b = _json.dumps(obj).encode()
            self.send_response(status); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            u = _up(self.path)
            if u.path == "/Users/Me":
                return self._j({"error": "forbidden"}, 403)
            if u.path == "/Users":
                return self._j([{"Id": "u-admin", "Name": "kladderapeng", "Policy": {"IsAdministrator": True}}])
            if u.path == f"/Items/{ITEM_ID}":
                return self._j({"error": "Error processing request"}, 400)
            if u.path == "/Items":
                return self._j({"Items": [{"Id": ITEM_ID, "Name": "Movie", "Type": "Movie", "Path": "/x.mkv",
                                           "RunTimeTicks": 100, "MediaSources": [{"Id": "s"}],
                                           "MediaStreams": [{"Index": 0, "Type": "Video"}]}], "TotalRecordCount": 1})
            return self._j({}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = JellyfinClient(f"http://127.0.0.1:{httpd.server_port}", "k")
    uid = c.current_user_id()
    assert uid == "u-admin"
    item = c.get_item(ITEM_ID, user_id=uid)
    assert item.id == ITEM_ID and item.media_source_id == "s"
    httpd.shutdown()
