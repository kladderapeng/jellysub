"""Align a subtitle to the media's audio track using ASR word timestamps.

Model: audio_time = speed * subtitle_time + offset (a linear map, fit by
least squares over anchor points). Anchor points come from matching normalized
word n-grams of each subtitle cue against the recognized ASR word stream.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .asr import Word
from .srt import Cue, Subtitle

TAG_RE = re.compile(r"\{[^}]*\}|<[^>]+>")
NONWORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> list[str]:
    text = TAG_RE.sub(" ", text)
    text = NONWORD_RE.sub(" ", text.lower())
    return [t for t in WS_RE.split(text.strip()) if t]


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


@dataclass
class Anchor:
    sub_ms: float
    audio_s: float
    overlap: int


@dataclass
class Alignment:
    speed: float
    offset_s: float
    rms_s: float
    anchors: list[Anchor] = field(default_factory=list)

    @property
    def quality(self) -> float:
        if not self.anchors:
            return 0.0
        return len(self.anchors) / max(1.0, self.rms_s + 0.05)


def match_cues(
    cues: list[Cue],
    words: list[Word],
    ngram_size: int = 4,
    min_overlap: int = 3,
    prior: Alignment | None = None,
    search_radius_s: float = 6.0,
    stride: int = 1,
) -> list[Anchor]:
    """For each cue, find the ASR word window whose tokens best overlap the cue.

    Returns anchors (cue start in seconds, audio start of the matched window,
    overlap). The matched window is constrained to start near the cue's
    predicted position when a rough estimate exists, otherwise scanned over
    the whole word stream.
    """
    asr_flat: list[str] = []
    asr_pos: list[int] = []
    for i, w in enumerate(words):
        for t in normalize_text(w.text):
            asr_flat.append(t)
            asr_pos.append(i)
    n = len(asr_flat)
    if n == 0:
        return []
    anchors: list[Anchor] = []
    for cue in cues:
        toks = normalize_text(" ".join(cue.lines))
        if len(toks) < min_overlap:
            continue
        k = min(ngram_size, len(toks))
        want = ngrams(toks, k)
        sub_s = cue.start_ms / 1000.0
        mid_s = (cue.start_ms + cue.end_ms) / 2000.0
        if prior is not None:
            predicted = prior.speed * sub_s + prior.offset_s
            center = sum(w.start for w in words) / max(1, len(words))
            if not (center * 0.5 <= predicted <= center * 2.0 + 60.0):
                prior = None
        best_overlap = 0
        best_i = -1
        span = len(toks) + ngram_size
        if prior is not None:
            candidates = [
                i
                for i in range(n)
                if abs(words[asr_pos[i]].start - predicted) <= search_radius_s
            ]
        else:
            candidates = range(0, n, stride)
        for i in candidates:
            window = ngrams(asr_flat[i : i + span], k)
            overlap = len(want & window)
            if overlap > best_overlap or (
                overlap == best_overlap
                and best_i >= 0
                and prior is None
                and abs(words[asr_pos[i]].start - sub_s) < abs(words[asr_pos[best_i]].start - sub_s)
            ):
                best_overlap = overlap
                best_i = i
        if best_i >= 0 and best_overlap >= min_overlap:
            w0 = words[asr_pos[best_i]]
            anchors.append(
                Anchor(sub_ms=sub_s, audio_s=w0.start, overlap=best_overlap)
            )
    return anchors


def fit_linear(anchors: list[Anchor]) -> Alignment | None:
    """Least squares fit audio_s = speed * sub_s + offset_s."""
    pts = [(a.sub_ms, a.audio_s) for a in anchors]
    if len(pts) < 2:
        if len(pts) == 1:
            return Alignment(speed=1.0, offset_s=pts[0][1] - pts[0][0], rms_s=0.0, anchors=anchors)
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    speed = (n * sxy - sx * sy) / denom
    offset = (sy - speed * sx) / n
    rms = math.sqrt(sum((speed * p[0] + offset - p[1]) ** 2 for p in pts) / n)
    return Alignment(speed=speed, offset_s=offset, rms_s=rms, anchors=anchors)


def robust_fit(
    anchors: list[Anchor], max_residual_s: float = 1.5, iterations: int = 5
) -> Alignment | None:
    """RANSAC-style fit: try all pairs, keep the model with most inliers, refit."""
    if len(anchors) < 2:
        return fit_linear(anchors)
    pts = sorted(anchors, key=lambda a: a.sub_ms)
    best_key: tuple | None = None
    best_inliers: list[Anchor] = []
    best_speed = 1.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            a, b = pts[i], pts[j]
            dx = b.sub_ms - a.sub_ms
            if abs(dx) < 1.0:
                continue
            speed = (b.audio_s - a.audio_s) / dx
            offset = a.audio_s - speed * a.sub_ms
            inliers = [
                p
                for p in pts
                if abs(speed * p.sub_ms + offset - p.audio_s) <= max_residual_s
            ]
            residual_sum = sum(
                abs(speed * p.sub_ms + offset - p.audio_s) for p in inliers
            )
            key = (len(inliers), -residual_sum)
            if best_key is None or key > best_key:
                best_key = key
                best_inliers = inliers
                best_speed = speed
    if not best_inliers:
        return fit_linear(anchors)
    result = fit_linear(best_inliers)
    if result is not None:
        result.anchors = best_inliers
    return result


def evaluate(sub: Subtitle, words: list[Word]) -> Alignment | None:
    """Multi-pass alignment: stride scan, then refine with progressively
    finer strides and prior-guided windows around the previous fit."""
    anchors = match_cues(sub.cues, words, stride=4)
    current = robust_fit(anchors)
    if current is None:
        return None
    for stride in (2, 1):
        anchors = match_cues(sub.cues, words, stride=stride, prior=current)
        nxt = robust_fit(anchors)
        if nxt is not None and (nxt.rms_s, -len(nxt.anchors)) <= (current.rms_s, -len(current.anchors)):
            current = nxt
    return current


def transform_subtitle(sub: Subtitle, alignment: Alignment) -> Subtitle:
    """Map subtitle times into the audio timeline: t' = (t - offset) / speed."""
    speed = alignment.speed if abs(alignment.speed) > 1e-6 else 1.0
    out: list[Cue] = []
    for c in sub.cues:
        start = (c.start_ms / 1000.0 - alignment.offset_s) / speed
        end = (c.end_ms / 1000.0 - alignment.offset_s) / speed
        start_ms = max(0, int(round(start * 1000)))
        end_ms = max(start_ms + 1, int(round(end * 1000)))
        out.append(Cue(index=len(out) + 1, start_ms=start_ms, end_ms=end_ms, lines=list(c.lines)))
    return Subtitle(cues=out)
