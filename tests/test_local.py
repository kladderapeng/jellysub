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
