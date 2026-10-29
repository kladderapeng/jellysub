"""Media-side subtitle verification and alignment.

For a Jellyfin item with a local Path:
  1. compute the OpenSubtitles moviehash of the media file,
  2. query subtitle providers (hash first, IMDb id as fallback),
  3. download the top-N candidate subtitle files,
  4. transcribe sampled audio windows of the media with ASR,
  5. align each candidate against the recognized words (linear fit),
  6. pick the candidate with the strongest alignment and rewrite its timing,
  7. upload the corrected subtitle back to Jellyfin.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field

from .align import Alignment, evaluate, transform_subtitle
from .asr import Word, ensure_model, Transcriber
from .client import Item, JellyfinClient, JellyfinError
from .media import extract_audio_window, find_ffmpeg, opensubtitles_hash, probe_audio_streams
from .providers import OpenSubtitles, ProviderError, SubFile, decode_subtitle
from .srt import Subtitle, parse_srt, render_srt

log = logging.getLogger("jsub.verify")


@dataclass
class VerifyConfig:
    language: str = "eng"
    candidates: int = 5
    windows: int = 3
    window_seconds: float = 45.0
    model_dir: str | None = None
    audio_index: int = 0
    ffmpeg: str | None = None
    dry_run: bool = False


@dataclass
class CandidateScore:
    file: SubFile
    alignment: Alignment | None
    sub: Subtitle | None

    @property
    def score(self) -> float:
        if self.alignment is None:
            return -1.0
        return self.alignment.quality * (1.0 / (1.0 + self.alignment.rms_s))


@dataclass
class VerifyResult:
    item_id: str
    name: str
    action: str
    moviehash: str | None = None
    chosen: str = ""
    speed: float = 1.0
    offset_s: float = 0.0
    rms_s: float = 0.0
    anchors: int = 0
    detail: str = ""
    corrected_srt: str | None = None


def sample_windows(runtime_s: float, count: int, window_s: float) -> list[tuple[float, float]]:
    """Evenly spread windows across the runtime, avoiding the very start/end."""
    if runtime_s <= 0:
        return [(0.0, window_s)]
    count = max(1, min(count, int(runtime_s // max(window_s, 1)) or 1))
    step = runtime_s / (count + 1)
    out = []
    for i in range(1, count + 1):
        start = max(0.0, i * step - window_s / 2)
        start = min(start, max(0.0, runtime_s - window_s))
        out.append((start, min(window_s, runtime_s - start)))
    return out


def transcribe_media(
    media_path: str, runtime_s: float, cfg: VerifyConfig
) -> list[Word]:
    ffmpeg = cfg.ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("no ffmpeg binary found (set FFMPEG_PATH or install imageio-ffmpeg)")
    model_path = ensure_model(cfg.language, cfg.model_dir)
    transcriber = Transcriber(model_path)
    words: list[Word] = []
    with tempfile.TemporaryDirectory(prefix="jsub-audio-") as tmp:
        for start, dur in sample_windows(runtime_s, cfg.windows, cfg.window_seconds):
            wav = os.path.join(tmp, f"w{start:.0f}.wav")
            try:
                extract_audio_window(media_path, start, dur, wav, ffmpeg=ffmpeg, audio_index=cfg.audio_index)
            except RuntimeError as e:
                log.warning("audio window at %.0fs failed: %s", start, e)
                continue
            words.extend(transcriber.transcribe_wav(wav, base_offset_s=start))
    return words


def score_candidates(
    subs: list[tuple[SubFile, Subtitle]], words: list[Word]
) -> list[CandidateScore]:
    out = []
    for f, sub in subs:
        alignment = evaluate(sub, words)
        out.append(CandidateScore(file=f, alignment=alignment, sub=sub))
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def verify_item(
    client: JellyfinClient,
    item: Item,
    cfg: VerifyConfig,
    provider: OpenSubtitles | None = None,
) -> VerifyResult:
    if not item.path or not os.path.exists(item.path):
        return VerifyResult(item.id, item.name, "error", detail="media path missing/unreachable")
    provider = provider or OpenSubtitles()
    moviehash = opensubtitles_hash(item.path)
    runtime_s = (item.runtime_ms or 0) / 1000.0
    log.info("[%s] moviehash=%s runtime=%.0fs", item.name, moviehash, runtime_s)

    lang2 = cfg.language[:2]
    try:
        hits = provider.search_by_hash(moviehash, languages=lang2)
        files = provider.best_files(hits, cfg.language, limit=cfg.candidates)
        if not files:
            imdb = (item.provider_ids or {}).get("Imdb")
            if imdb:
                hits = provider.search_by_imdb(imdb, languages=lang2)
                files = provider.best_files(hits, cfg.language, limit=cfg.candidates)
    except ProviderError as e:
        return VerifyResult(item.id, item.name, "error", moviehash=moviehash, detail=str(e))
    if not files:
        return VerifyResult(item.id, item.name, "no-match", moviehash=moviehash)
    log.info("[%s] %d candidate subtitle files", item.name, len(files))

    subs: list[tuple[SubFile, Subtitle]] = []
    for f in files:
        try:
            raw = provider.download(f.file_id)
            text = decode_subtitle(raw)
            subs.append((f, parse_srt(text)))
        except Exception as e:
            log.warning("[%s] candidate %s failed: %s", item.name, f.file_id, e)
    if not subs:
        return VerifyResult(item.id, item.name, "error", moviehash=moviehash, detail="all downloads failed")

    words = transcribe_media(item.path, runtime_s, cfg)
    log.info("[%s] ASR produced %d words", item.name, len(words))
    if not words:
        return VerifyResult(item.id, item.name, "error", moviehash=moviehash, detail="ASR produced no words")

    scored = score_candidates(subs, words)
    best = scored[0]
    if best.alignment is None or best.score <= 0:
        return VerifyResult(
            item.id, item.name, "no-match", moviehash=moviehash,
            detail=f"best score {best.score:.3f}, no usable alignment",
        )
    al = best.alignment
    corrected = transform_subtitle(best.sub, al)
    srt_out = render_srt(corrected)
    log.info(
        "[%s] chose %s: speed=%.5f offset=%.2fs rms=%.3fs anchors=%d",
        item.name, best.file.file_name, al.speed, al.offset_s, al.rms_s, len(al.anchors),
    )
    result = VerifyResult(
        item.id, item.name, "aligned", moviehash=moviehash,
        chosen=best.file.file_name, speed=al.speed, offset_s=al.offset_s,
        rms_s=al.rms_s, anchors=len(al.anchors), corrected_srt=srt_out,
    )
    if cfg.dry_run:
        result.action = "would-align"
        return result
    lang3 = cfg.language
    client.upload_subtitle(item.id, srt_out.encode("utf-8"), language=lang3, format="srt")
    try:
        client.refresh_item(item.id)
    except JellyfinError as e:
        log.warning("[%s] refresh failed: %s", item.name, e)
    return result
