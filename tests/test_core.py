from jsub.client import Item, MediaStream, RemoteSubtitle
from jsub.core import (
    FetchConfig,
    FitConfig,
    fit_subtitle_text,
    iso639_2_to_3,
    normalize_lang,
    pick_best,
    score_candidate,
)


def make_item(name="Some Movie", langs=()):
    streams = [
        MediaStream(index=i, type="Subtitle", language=l, codec="srt", is_external=True)
        for i, l in enumerate(langs)
    ]
    return Item(id="abc", name=name, type="Movie", media_streams=streams)


def test_normalize_and_iso():
    assert normalize_lang("EN") == "en"
    assert normalize_lang("pt-BR") == "pt_br"
    assert iso639_2_to_3("en") == "eng"
    assert iso639_2_to_3("xx") == "xx"


def test_score_rejects_wrong_language_and_format():
    cfg = FetchConfig(languages=["eng"])
    item = make_item()
    assert score_candidate(RemoteSubtitle(id="1", language="fra", format="srt"), item, "eng", cfg) == -1.0
    assert score_candidate(RemoteSubtitle(id="2", language="eng", format="pgssub"), item, "eng", cfg) == -1.0


def test_score_prefers_srt_and_high_score():
    cfg = FetchConfig(languages=["eng"])
    item = make_item("Some Movie 2024")
    srt = RemoteSubtitle(id="1", language="eng", format="srt", score=90, download_count=5000)
    ass = RemoteSubtitle(id="2", language="eng", format="ass", score=95, download_count=5000)
    assert score_candidate(srt, item, "eng", cfg) > score_candidate(ass, item, "eng", cfg)


def test_pick_best_accepts_iso3_and_2():
    cfg = FetchConfig(languages=["eng"])
    item = make_item()
    cands = [
        RemoteSubtitle(id="a", language="eng", format="srt", score=50),
        RemoteSubtitle(id="b", language="en", format="srt", score=80),
    ]
    best = pick_best(cands, item, "eng", cfg)
    assert best is not None and best.id == "b"


def test_pick_best_none_when_below_min_score():
    cfg = FetchConfig(languages=["eng"], min_score=100.0)
    cands = [RemoteSubtitle(id="a", language="eng", format="srt", score=10)]
    assert pick_best(cands, make_item(), "eng", cfg) is None


def test_has_subtitle_matches_2_and_3_letter():
    item = make_item(langs=("eng",))
    assert item.has_subtitle("eng")
    assert item.has_subtitle("en")
    assert not item.has_subtitle("fra")


def test_fit_subtitle_text_applies_offset_and_clamp():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nhi\n\n2\n00:09:59,000 --> 00:10:02,000\nbye\n"
    out = fit_subtitle_text(srt, runtime_ms=10 * 60_000, fit=FitConfig(offset_ms=500))
    assert "00:00:01,500 --> 00:00:03,500" in out
    assert "00:09:59,500 --> 00:10:00,000" in out


def test_fit_subtitle_text_without_runtime_keeps_end():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nhi\n"
    out = fit_subtitle_text(srt, runtime_ms=None, fit=FitConfig(offset_ms=0))
    assert "00:00:03,000" in out
