"""End-to-end verify_item test with a stubbed provider, stubbed ASR, and stub Jellyfin."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import jsub.verify as verify_mod
from jsub.asr import Word
from jsub.client import Item, JellyfinClient
from jsub.srt import parse_srt
from jsub.verify import VerifyConfig, verify_item

ITEM_ID = "33333333333333333333333333333333"
SOURCE_ID = "src"
MEDIA_PATH = "/nonexistent/media.mkv"

SRT_WRONG = "1\n00:00:01,000 --> 00:00:03,000\ncompletely unrelated words here now\n"
SRT_RIGHT = (
    "1\n00:00:10,000 --> 00:00:14,000\nwe are going to test this thing right now\n\n"
    "2\n00:00:40,000 --> 00:00:44,000\nanother line of speech for the second cue\n\n"
    "3\n00:00:70,000 --> 00:00:74,000\na third distinct passage spoken aloud\n\n"
    "4\n00:01:00,000 --> 00:01:04,000\nand a fourth one for more anchors\n\n"
    "5\n00:02:10,000 --> 00:02:14,000\nfifth sentence to strengthen the anchors\n\n"
    "6\n00:02:40,000 --> 00:02:44,000\nsixth and final phrase for the fit\n"
)


class StubProvider:
    api_key = "stub-key"

    def __init__(self):
        self.downloads = []

    def search_by_hash(self, moviehash, languages="en"):
        return []

    def search_by_imdb(self, imdb_id, languages="en"):
        return []

    def best_files(self, hits, language, limit=5):
        from jsub.providers import SubFile

        return [
            SubFile(file_id=1, file_name="wrong.srt", language="en", format="srt", download_count=10, rating=9.0),
            SubFile(file_id=2, file_name="right.srt", language="en", format="srt", download_count=100, rating=8.0),
        ][:limit]

    def download(self, file_id):
        self.downloads.append(file_id)
        return (SRT_WRONG if file_id == 1 else SRT_RIGHT).encode()


CUES = [
    (10.0, "we are going to test this thing right now"),
    (40.0, "another line of speech for the second cue"),
    (70.0, "a third distinct passage spoken aloud"),
    (100.0, "and a fourth one for more anchors"),
    (130.0, "fifth sentence to strengthen the anchors"),
    (160.0, "sixth and final phrase for the fit"),
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

    def _send(self, status=204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        if path == f"/Videos/{ITEM_ID}/Subtitles":
            Handler.uploads.append(json.loads(body.decode()))
            return self._send(204)
        if path == f"/Items/{ITEM_ID}/Refresh":
            return self._send(204)
        return self._send(404)


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_verify_item_picks_aligned_candidate(server, monkeypatch, tmp_path):
    Handler.uploads.clear()
    media = tmp_path / "movie.mkv"
    media.write_bytes(os.urandom(300_000))
    item = Item(
        id=ITEM_ID, name="Test Movie", type="Movie", path=str(media),
        runtime_ticks=int(120 * 10_000_000), provider_ids={"Imdb": "tt1234567"},
        media_source_id=SOURCE_ID,
    )
    client = JellyfinClient(server, "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(verify_mod.os.path, "exists", lambda p: True)
    cfg = VerifyConfig(language="eng", candidates=2, windows=1, window_seconds=10.0)
    res = verify_item(client, item, cfg, StubProvider())
    assert res.action == "aligned"
    assert res.chosen.startswith("right.srt")
    assert res.anchors >= 2
    assert res.rms_s < 1.0
    assert res.corrected_srt is not None
    assert Handler.uploads, "corrected subtitle should be uploaded"
    dto = Handler.uploads[0]
    assert dto["Format"] == "srt"
    data = base64.b64decode(dto["Data"]).decode()
    sub = parse_srt(data)
    assert len(sub.cues) == 6
    assert sub.cues[0].start_ms == pytest.approx(10_000, abs=1500)


def test_verify_item_dry_run_uploads_nothing(server, monkeypatch, tmp_path):
    Handler.uploads.clear()
    media = tmp_path / "movie.mkv"
    media.write_bytes(os.urandom(300_000))
    item = Item(id=ITEM_ID, name="Test Movie", type="Movie", path=str(media),
                runtime_ticks=120 * 10_000_000, provider_ids={"Imdb": "tt1"}, media_source_id=SOURCE_ID)
    client = JellyfinClient(server, "k")
    monkeypatch.setattr(verify_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(verify_mod.os.path, "exists", lambda p: True)
    cfg = VerifyConfig(language="eng", dry_run=True)
    res = verify_item(client, item, cfg, StubProvider())
    assert res.action == "would-align"
    assert not Handler.uploads


import os  # noqa: E402
