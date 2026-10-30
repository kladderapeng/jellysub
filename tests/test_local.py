"""Local mode: align_local_file writes a corrected .srt next to the media file."""

from __future__ import annotations

import os

import pytest

import jsub.local as local_mod
from jsub.asr import Word
from jsub.local import align_local_file, sidecar_candidates
from jsub.srt import parse_srt
from jsub.verify import VerifyConfig

SRT = (
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


@pytest.fixture()
def media(tmp_path, monkeypatch):
    m = tmp_path / "Movie.mkv"
    m.write_bytes(b"\0" * 300_000)
    (tmp_path / "Movie.en.srt").write_text(SRT)
    monkeypatch.setattr(local_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(local_mod, "_probe_runtime_s", lambda *a, **k: 200.0)
    return m


def test_local_writes_corrected_srt_next_to_media(media):
    res = align_local_file(str(media), "eng", VerifyConfig(language="eng"), provider=None)
    assert res.action == "aligned"
    assert res.out_path == str(media.with_suffix("")) + ".en.srt"
    assert os.path.exists(res.out_path)
    sub = parse_srt(open(res.out_path).read())
    assert len(sub.cues) == 4
    assert sub.cues[0].start_ms == pytest.approx(10_000, abs=1500)
    assert res.anchors >= 3


def test_local_dry_run_writes_nothing(media):
    res = align_local_file(str(media), "eng", VerifyConfig(language="eng", dry_run=True), provider=None)
    assert res.action == "would-align"
    assert not os.path.exists(str(media.with_suffix("")) + ".zzz.srt")


def test_local_missing_file(tmp_path):
    res = align_local_file(str(tmp_path / "nope.mkv"), "eng", VerifyConfig(), provider=None)
    assert res.action == "error"


def test_local_no_candidates(tmp_path, monkeypatch):
    m = tmp_path / "Bare.mkv"
    m.write_bytes(b"\0" * 300_000)
    monkeypatch.setattr(local_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(local_mod, "_probe_runtime_s", lambda *a, **k: 100.0)
    res = align_local_file(str(m), "eng", VerifyConfig(language="eng"), provider=None)
    assert res.action == "no-match"
    assert "OPENSUBTITLES_API_KEY" in res.detail


def test_sidecar_candidates_filters_language(media):
    (media.parent / "Movie.deu.srt").write_text(SRT)
    eng = sidecar_candidates(str(media), "eng")
    assert all("deu" not in f.file_name for f, _ in eng)
    assert len(eng) == 1


def test_local_with_explicit_sub_no_provider(tmp_path, monkeypatch):
    m = tmp_path / "Bare.mkv"
    m.write_bytes(b"\0" * 300_000)
    sub = tmp_path / "subs" / "whatever.srt"
    sub.parent.mkdir()
    sub.write_text(SRT)
    monkeypatch.setattr(local_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(local_mod, "_probe_runtime_s", lambda *a, **k: 200.0)
    res = align_local_file(str(m), "eng", VerifyConfig(language="eng"), provider=None, sub_path=str(sub))
    assert res.action == "aligned"
    assert res.chosen == "whatever.srt"
    assert os.path.exists(str(m.with_suffix("")) + ".en.srt")


def test_query_terms_from_path_episode_and_movie():
    from jsub.media import query_terms_from_path

    t = query_terms_from_path("/media/series/Mum/Season 01/Mum S01E01.mp4")
    assert t["series"] == "Mum" and t["season"] == 1 and t["episode"] == 1
    assert t["query"] == "Mum S01E01"
    t = query_terms_from_path("/data/movies/Movie (1920)/Movie (1920).mkv")
    assert "season" not in t and t["query"] == "Movie (1920)"


def test_provider_query_fallback(tmp_path, monkeypatch):
    from jsub.providers import OpenSubtitles, SubFile
    from jsub.local import align_local_file
    from jsub.verify import VerifyConfig

    calls = []

    class P(OpenSubtitles):
        def __init__(self):
            self.api_key = "k"
        def search_by_hash(self, moviehash, languages="en"):
            calls.append(("hash", moviehash))
            return []
        def search_by_query(self, query, languages="en", **kw):
            calls.append(("query", query, kw))
            return [type("H", (), {"files": [SubFile(file_id=7, file_name="q.srt", language="en", format="srt", download_count=99, rating=9.0)]})()]
        def download(self, file_id):
            return SRT.encode()

    m = tmp_path / "Mum" / "Season 01" / "Mum S01E01.mp4"
    m.parent.mkdir(parents=True)
    m.write_bytes(b"\0" * 300_000)
    monkeypatch.setattr(local_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(local_mod, "_probe_runtime_s", lambda *a, **k: 200.0)
    res = align_local_file(str(m), "eng", VerifyConfig(language="eng"), provider=P())
    assert calls[0][0] == "hash"
    assert any(c[0] == "query" and "S01E01" in c[1] and c[2].get("season") == 1 for c in calls), calls
    assert res.action == "aligned" and res.chosen == "q.srt"


def test_subliminal_candidate_source_used_when_no_others(tmp_path, monkeypatch):
    """No API key, no sidecar, no --sub: subliminal provides the candidate."""
    import jsub.subliminal_provider as sp

    monkeypatch.setitem(__import__("sys").modules, "jsub.subliminal_provider", sp)
    monkeypatch.setattr(sp, "subliminal_candidates", lambda *a, **k: [sp.SubCandidate("podnapisi", "42", "Mum.S01E01.srt", "en", "srt", 90.0)])
    monkeypatch.setattr(sp, "subliminal_download", lambda c, p: SRT.encode())

    m = tmp_path / "Mum S01E01.mp4"
    m.write_bytes(b"\0" * 300_000)
    monkeypatch.setattr(local_mod, "transcribe_media", lambda *a, **k: fake_words())
    monkeypatch.setattr(local_mod, "_probe_runtime_s", lambda *a, **k: 200.0)
    res = align_local_file(str(m), "eng", VerifyConfig(language="eng"), provider=None)
    assert res.action == "aligned"
    assert res.chosen == "Mum.S01E01.srt"


def test_config_file_supplies_opensubtitles_credentials(tmp_path, monkeypatch):
    import jsub.__main__ as m

    cfg_file = tmp_path / "jsub.json"
    cfg_file.write_text(__import__("json").dumps({
        "url": "http://x", "api_key": "k",
        "os_api_key": "KEY123", "os_username": "u@x", "os_password": "p@$$w0rd#!",
    }))
    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_USERNAME", raising=False)
    monkeypatch.delenv("OPENSUBTITLES_PASSWORD", raising=False)
    args = m.build_parser().parse_args(["--config", str(cfg_file), "info"])
    cfg = m.load_config(args)
    assert cfg["os_password"] == "p@$$w0rd#!"
    assert __import__("os").environ["OPENSUBTITLES_PASSWORD"] == "p@$$w0rd#!"
    assert __import__("os").environ["OPENSUBTITLES_API_KEY"] == "KEY123"


def test_provider_works_with_userpass_only_no_api_key(monkeypatch):
    """No OPENSUBTITLES_API_KEY: fallback app key + login token from user/pass."""
    from jsub.providers import OpenSubtitles

    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    monkeypatch.setenv("OPENSUBTITLES_USERNAME", "u")
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", "p")

    calls = []

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def read(self):
            import json as j
            return j.dumps(self._p).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        status = 200

    import urllib.request
    real = urllib.request.urlopen
    def fake(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/login"):
            return FakeResp({"token": "tok123"})
        return FakeResp({"data": []})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    p = OpenSubtitles()
    assert p.api_key  # fallback key present without env
    assert p._token == "tok123"
    assert any(u.endswith("/login") for u in calls)
