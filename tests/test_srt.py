from jsub.srt import fit_srt, ms_to_timestamp, parse_srt, render_srt, shift_srt, timestamp_to_ms

SAMPLE = """1
00:00:01,000 --> 00:00:04,000
Hello world

2
00:01:00,500 --> 00:01:03,250
Second line
continued

3
01:59:58,000 --> 02:00:01,000
Last cue
"""


def test_parse_roundtrip():
    sub = parse_srt(SAMPLE)
    assert len(sub.cues) == 3
    assert sub.cues[0].start_ms == 1000
    assert sub.cues[0].end_ms == 4000
    assert sub.cues[1].lines == ["Second line", "continued"]
    assert sub.duration_ms == 2 * 3_600_000 + 1000
    rendered = render_srt(sub)
    assert parse_srt(rendered).cues == sub.cues


def test_timestamp_conversion():
    assert timestamp_to_ms("00:01:02,345") == 62_345
    assert timestamp_to_ms("01:00:00,000") == 3_600_000
    assert ms_to_timestamp(62_345) == "00:01:02,345"
    assert ms_to_timestamp(-5) == "00:00:00,000"


def test_shift_positive_and_negative():
    sub = parse_srt(SAMPLE)
    shifted = shift_srt(sub, 1500)
    assert shifted.cues[0].start_ms == 2500
    assert shifted.cues[0].end_ms == 5500
    neg = shift_srt(sub, -2000)
    assert neg.cues[0].end_ms == 2000
    assert neg.cues[0].start_ms == 0


def test_shift_drops_cues_ending_before_zero():
    sub = parse_srt(SAMPLE)
    dropped = shift_srt(sub, -5000)
    assert len(dropped.cues) == 2
    assert dropped.cues[0].start_ms == 60_500 - 5000


def test_fit_clamps_to_runtime():
    sub = parse_srt(SAMPLE)
    runtime = 2 * 3_600_000  # 2h exactly; last cue ends at 2h+1s
    fitted = fit_srt(sub, runtime)
    assert fitted.cues[-1].end_ms == runtime
    assert fitted.cues[-1].start_ms == 2 * 3_600_000 - 2000


def test_fit_drops_cues_past_runtime_and_degenerate():
    text = (
        "1\n00:00:01,000 --> 00:00:02,000\nok\n\n"
        "2\n00:10:00,000 --> 00:10:00,000\nzero\n\n"
        "3\n00:10:01,000 --> 00:10:02,000\npast\n"
    )
    fitted = fit_srt(parse_srt(text), 5 * 60_000)
    assert [c.index for c in fitted.cues] == [1]
    assert fitted.cues[0].lines == ["ok"]


def test_render_renumbers_after_fit():
    sub = parse_srt(SAMPLE)
    fitted = fit_srt(shift_srt(sub, -5000), 100_000)
    assert [c.index for c in fitted.cues] == list(range(1, len(fitted.cues) + 1))
    assert "-->" in render_srt(fitted)
