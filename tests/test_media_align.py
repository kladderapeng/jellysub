import os
import struct

from jsub.align import (
    Alignment,
    evaluate,
    fit_linear,
    match_cues,
    normalize_text,
    robust_fit,
    transform_subtitle,
)
from jsub.asr import Word
from jsub.media import opensubtitles_hash
from jsub.srt import Cue, Subtitle, parse_srt, render_srt
from jsub.verify import sample_windows


def test_normalize_text_strips_tags_and_punct():
    assert normalize_text("<i>Hello, World!</i> it's 100% ok") == [
        "hello", "world", "it", "s", "100", "ok",
    ]


def test_opensubtitles_hash_known_vector(tmp_path):
    data = bytes(range(256)) * 1024  # 256 KiB pattern
    p = tmp_path / "movie.mkv"
    p.write_bytes(data)
    h = opensubtitles_hash(str(p))
    assert len(h) == 16
    int(h, 16)
    size = len(data)
    head = data[:65536]
    tail = data[-65536:]
    expect = size
    for chunk in (head, tail):
        for i in range(0, len(chunk), 8):
            expect += struct.unpack("<q", chunk[i : i + 8].ljust(8, b"\0"))[0]
    assert h == f"{expect & 0xFFFFFFFFFFFFFFFF:016x}"


def test_opensubtitles_hash_stable_and_size_sensitive(tmp_path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"x" * 200_000)
    b.write_bytes(b"x" * 200_001)
    assert opensubtitles_hash(str(a)) != opensubtitles_hash(str(b))
    assert opensubtitles_hash(str(a)) == opensubtitles_hash(str(a))


def make_words(script, start=0.0, step=0.4):
    words = []
    t = start
    for w in script:
        words.append(Word(start=t, end=t + step - 0.05, text=w))
        t += step
    return words


def test_match_cues_finds_anchors():
    script = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog", "today"]
    words = make_words(script, start=10.0)
    cues = [
        Cue(index=1, start_ms=1000, end_ms=3000, lines=["The quick brown fox jumps over the lazy dog today"]),
        Cue(index=2, start_ms=99999, end_ms=101000, lines=["zzz qqq vvv bbb"]),
    ]
    anchors = match_cues(cues, words)
    assert len(anchors) == 1
    assert anchors[0].sub_ms == 1.0
    assert abs(anchors[0].audio_s - 10.0) < 2.0
    assert anchors[0].overlap >= 3


def test_fit_linear_exact():
    from jsub.align import Anchor

    anchors = [Anchor(sub_ms=s, audio_s=1.02 * s + 5.0, overlap=5) for s in (10, 100, 500, 900)]
    al = fit_linear(anchors)
    assert al is not None
    assert abs(al.speed - 1.02) < 1e-9
    assert abs(al.offset_s - 5.0) < 1e-9
    assert al.rms_s < 1e-9


def test_robust_fit_drops_outlier():
    from jsub.align import Anchor

    anchors = [Anchor(sub_ms=s, audio_s=1.0 * s + 2.0, overlap=5) for s in (10, 50, 100, 200, 400)]
    anchors.append(Anchor(sub_ms=300, audio_s=9999.0, overlap=5))
    al = robust_fit(anchors)
    assert al is not None
    assert abs(al.speed - 1.0) < 0.01
    assert abs(al.offset_s - 2.0) < 0.01
    assert len(al.anchors) == 5


def test_transform_subtitle_applies_speed_and_offset():
    srt = "1\n00:00:10,000 --> 00:00:12,000\nhello there\n"
    sub = parse_srt(srt)
    al = Alignment(speed=1.02, offset_s=5.0, rms_s=0.0)
    out = transform_subtitle(sub, al)
    expect_start = (10.0 - 5.0) / 1.02
    assert abs(out.cues[0].start_ms / 1000.0 - expect_start) < 0.01
    assert out.cues[0].lines == ["hello there"]


def test_evaluate_end_to_end_synthetic():
    true_speed, true_offset = 1.03, 7.0
    cue_texts = [
        "We are going to test this thing right now today",
        "Another line of speech for the second cue here ok",
        "A third distinct passage spoken aloud for alignment",
        "And a fourth one to give the fit more anchors",
    ]
    words = []
    for i, text in enumerate(cue_texts):
        sub_t = (i + 1) * 30.0
        audio_t = true_speed * sub_t + true_offset
        for j, w in enumerate(normalize_text(text)):
            words.append(Word(start=audio_t + j * 0.5, end=audio_t + j * 0.5 + 0.4, text=w))
    cues = []
    for i, text in enumerate(cue_texts):
        sub_t = (i + 1) * 30.0
        cues.append(
            Cue(index=i + 1, start_ms=int(sub_t * 1000), end_ms=int((sub_t + 5) * 1000), lines=[text])
        )
    sub = Subtitle(cues=cues)
    al = evaluate(sub, words)
    assert al is not None
    assert abs(al.speed - true_speed) < 0.05
    assert abs(al.offset_s - true_offset) < 1.0
    assert al.rms_s < 1.0
    corrected = transform_subtitle(sub, al)
    assert corrected.cues[0].start_ms / 1000.0 == pytest_approx(
        (30.0 - true_offset) / true_speed, abs=0.05
    )


def pytest_approx(value, abs=1e-6):
    import pytest

    return pytest.approx(value, abs=abs)


def test_sample_windows_spread():
    wins = sample_windows(3600.0, 3, 45.0)
    assert len(wins) == 3
    for start, dur in wins:
        assert 0 <= start
        assert dur <= 45.0
        assert start + dur <= 3600.0 + 0.01
    starts = [w[0] for w in wins]
    assert starts == sorted(starts)
