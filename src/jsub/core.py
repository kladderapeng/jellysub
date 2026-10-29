"""Core pipeline: find items missing subtitles, fetch best remote match, fit, upload."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .client import (
    Item,
    JellyfinClient,
    JellyfinError,
    RemoteSubtitle,
    iso639_2_to_3,
    language_aliases,
)
from .srt import fit_srt, parse_srt, render_srt, shift_srt

log = logging.getLogger("jsub")

TEXT_FORMATS = {"srt", "ass", "ssa", "vtt", "sub", "ttml"}


@dataclass
class FetchConfig:
    languages: list[str] = field(default_factory=lambda: ["eng"])
    prefer_hearing_impaired: bool = False
    min_score: float = 0.0
    allow_formats: set[str] = field(default_factory=lambda: set(TEXT_FORMATS))


@dataclass
class FitConfig:
    offset_ms: float = 0.0
    only_if_runtime: bool = True


@dataclass
class ItemResult:
    item_id: str
    name: str
    action: str
    language: str | None = None
    detail: str = ""


def normalize_lang(code: str) -> str:
    return code.strip().lower().replace("-", "_")


def score_candidate(
    candidate: RemoteSubtitle, item: Item, language: str, cfg: FetchConfig
) -> float:
    if normalize_lang(candidate.language) not in {
        normalize_lang(a) for a in language_aliases(language)
    }:
        return -1.0
    if candidate.format and candidate.format not in cfg.allow_formats:
        return -1.0
    if cfg.prefer_hearing_impaired and not candidate.is_hearing_impaired:
        score = -5.0
    else:
        score = 0.0
    if candidate.score is not None:
        score += float(candidate.score)
    if candidate.download_count:
        score += min(candidate.download_count / 100.0, 10.0)
    if candidate.community_rating:
        score += float(candidate.community_rating) * 2.0
    if candidate.format == "srt":
        score += 10.0
    name = (item.name or "").lower()
    cname = (candidate.name or "").lower()
    if cname and cname in name:
        score += 2.0
    return score


def pick_best(
    candidates: list[RemoteSubtitle], item: Item, language: str, cfg: FetchConfig
) -> RemoteSubtitle | None:
    best: RemoteSubtitle | None = None
    best_score = cfg.min_score
    for cand in candidates:
        s = score_candidate(cand, item, language, cfg)
        if s < best_score:
            continue
        if best is None or s > best_score:
            best, best_score = cand, s
    return best


def fetch_and_fit(
    client: JellyfinClient,
    item: Item,
    cfg: FetchConfig,
    fit: FitConfig,
    dry_run: bool = False,
) -> list[ItemResult]:
    results: list[ItemResult] = []
    for language in cfg.languages:
        if item.has_subtitle(language) or item.has_subtitle(iso639_2_to_3(language)):
            log.info("[%s] already has %s subtitle, skipping", item.name, language)
            results.append(ItemResult(item.id, item.name, "skip-existing", language))
            continue
        try:
            candidates = client.search_remote_subtitles(item.id, language)
        except JellyfinError as e:
            log.warning("[%s] remote search failed: %s", item.name, e)
            results.append(ItemResult(item.id, item.name, "error", language, str(e)))
            continue
        best = pick_best(candidates, item, language, cfg)
        if best is None:
            log.info("[%s] no suitable remote subtitle for %s", item.name, language)
            results.append(ItemResult(item.id, item.name, "no-match", language))
            continue
        log.info(
            "[%s] selected %s (%s, score=%s)", item.name, best.id, best.format, best.score
        )
        if dry_run:
            results.append(
                ItemResult(item.id, item.name, "would-fetch", language, best.id)
            )
            continue
        if not client.download_remote_subtitle(item.id, best.id):
            results.append(
                ItemResult(item.id, item.name, "error", language, "download failed")
            )
            continue
        results.append(
            ItemResult(item.id, item.name, "fetched", language, best.id)
        )
    return results


def fit_uploaded(
    client: JellyfinClient,
    item: Item,
    language: str,
    fit: FitConfig,
    dry_run: bool = False,
) -> ItemResult:
    """Re-read the item, take its newest matching external subtitle, shift/clamp, re-upload."""
    fresh = client.get_item(item.id)
    subs = [s for s in fresh.subtitle_streams() if s.is_external]
    target = None
    wanted = {a.lower() for a in language_aliases(language)}
    for s in reversed(subs):
        if (s.language or "").lower() in wanted:
            target = s
            break
    if target is None and subs:
        target = subs[-1]
    if target is None:
        return ItemResult(item.id, item.name, "error", language, "no external subtitle found")
    log.info(
        "[%s] fitting subtitle stream index %s (lang=%s)",
        item.name, target.index, target.language,
    )
    if dry_run:
        return ItemResult(item.id, item.name, "would-fit", language, f"stream {target.index}")
    if fresh.media_source_id is None:
        return ItemResult(item.id, item.name, "error", language, "no media source id")
    fmt = (target.codec or "srt").lower()
    if fmt not in ("srt", "ass", "ssa", "vtt"):
        return ItemResult(item.id, item.name, "error", language, f"cannot edit format {fmt}")
    raw = client.download_subtitle_stream(fresh.id, fresh.media_source_id, target.index, "srt")
    text = raw.decode("utf-8", "replace")
    if not text.lstrip().startswith(("1", "WEBVTT")) and "-->" not in text:
        return ItemResult(item.id, item.name, "error", language, "downloaded subtitle not SRT-like")
    adjusted = fit_subtitle_text(text, fresh.runtime_ms, fit)
    client.upload_subtitle(
        fresh.id,
        adjusted.encode("utf-8"),
        language=target.language or iso639_2_to_3(language),
        format="srt",
        is_forced=target.is_forced,
        is_hearing_impaired=target.is_hearing_impaired,
    )
    try:
        client.refresh_item(fresh.id)
    except JellyfinError as e:
        log.warning("[%s] metadata refresh failed: %s", item.name, e)
    return ItemResult(
        item.id,
        item.name,
        "fitted",
        language,
        f"offset={fit.offset_ms}ms runtime={fresh.runtime_ms}ms",
    )


def fit_subtitle_text(
    srt_text: str, runtime_ms: int | None, fit: FitConfig
) -> str:
    sub = parse_srt(srt_text)
    if fit.offset_ms:
        sub = shift_srt(sub, fit.offset_ms)
    if runtime_ms is not None and fit.only_if_runtime:
        sub = fit_srt(sub, runtime_ms)
    return render_srt(sub)
