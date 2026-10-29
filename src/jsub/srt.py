"""Minimal SRT parsing and timing manipulation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def ms_to_timestamp(ms: int) -> str:
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def timestamp_to_ms(text: str) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})", text.strip())
    if not m:
        raise ValueError(f"invalid SRT timestamp: {text!r}")
    h, mi, s, frac = m.groups()
    frac = (frac + "000")[:3]
    return (int(h) * 3_600_000) + (int(mi) * 60_000) + (int(s) * 1000) + int(frac)


@dataclass
class Cue:
    index: int
    start_ms: int
    end_ms: int
    lines: list[str] = field(default_factory=list)


@dataclass
class Subtitle:
    cues: list[Cue] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        if not self.cues:
            return 0
        return max(c.end_ms for c in self.cues)


def parse_srt(text: str) -> Subtitle:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", text.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = block.strip("\n").split("\n")
        if not lines:
            continue
        idx = 0
        if lines[0].strip().isdigit():
            idx = 1
        if idx >= len(lines) or not TIMESTAMP_RE.search(lines[idx]):
            continue
        m = TIMESTAMP_RE.search(lines[idx])
        assert m is not None
        start = (
            int(m.group(1)) * 3_600_000
            + int(m.group(2)) * 60_000
            + int(m.group(3)) * 1000
            + int((m.group(4) + "000")[:3])
        )
        end = (
            int(m.group(5)) * 3_600_000
            + int(m.group(6)) * 60_000
            + int(m.group(7)) * 1000
            + int((m.group(8) + "000")[:3])
        )
        cues.append(
            Cue(index=len(cues) + 1, start_ms=start, end_ms=end, lines=lines[idx + 1 :])
        )
    return Subtitle(cues=cues)


def render_srt(sub: Subtitle) -> str:
    out: list[str] = []
    for cue in sub.cues:
        out.append(str(cue.index))
        out.append(f"{ms_to_timestamp(cue.start_ms)} --> {ms_to_timestamp(cue.end_ms)}")
        out.extend(cue.lines)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def shift_srt(sub: Subtitle, offset_ms: float) -> Subtitle:
    """Shift all cue times by offset_ms; cues ending at/before 0 are dropped."""
    cues = [
        Cue(
            index=i + 1,
            start_ms=max(0, c.start_ms + int(round(offset_ms))),
            end_ms=c.end_ms + int(round(offset_ms)),
            lines=list(c.lines),
        )
        for i, c in enumerate(sub.cues)
        if c.end_ms + offset_ms > 0
    ]
    return Subtitle(cues=cues)


def fit_srt(sub: Subtitle, runtime_ms: int) -> Subtitle:
    """Clamp cue times to the media runtime and drop degenerate cues.

    Cues that start at/after the runtime, or whose duration collapses to
    zero or below, are removed. Cue endings past the runtime are clamped.
    """
    cues: list[Cue] = []
    for c in sub.cues:
        if c.start_ms >= runtime_ms:
            continue
        start = max(0, c.start_ms)
        end = min(c.end_ms, runtime_ms)
        if end - start < 1:
            continue
        cues.append(Cue(index=len(cues) + 1, start_ms=start, end_ms=end, lines=list(c.lines)))
    return Subtitle(cues=cues)
