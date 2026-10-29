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
    media_root: str | None = None


def resolve_media_path(server_path: str, media_root: str | None = None) -> str | None:
    """Map Jellyfin's server-side item path to a locally readable path.

    Tries the path as-is, then re-rooted under media_root (e.g. Jellyfin sees
    /media/series/... but locally it is /mnt/media/series/...), matching on
    the trailing components so any prefix difference is tolerated.
    """
    candidates = [server_path]
    if media_root:
        root = media_root.rstrip(os.sep)
        candidates.append(root + server_path)
        parts = [p for p in server_path.split(os.sep) if p]
        for skip in range(1, len(parts)):
            candidates.append(os.path.join(root, *parts[skip:]))
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


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


def _jellyfin_plugin_candidates(
    client: JellyfinClient, item: Item, language: str, limit: int, media_root: str | None = None
) -> list[tuple[SubFile, Subtitle]]:
    """Candidate subtitles via Jellyfin's own remote search (Open Subtitles
    plugin installed server-side, no API key needed), read back from disk."""
    from .core import iso639_2_to_3

    cands = client.search_remote_subtitles(item.id, language)
    lang3 = iso639_2_to_3(language)
    cands = [
        c
        for c in cands
        if (c.language or "").lower() in (language.lower(), lang3)
        and (c.format or "srt").lower() in ("srt", "ass", "ssa", "vtt")
    ]
    cands.sort(
        key=lambda c: (
            float(c.score or 0) + min(c.download_count or 0, 10000) / 100.0
            + (float(c.community_rating or 0) * 2),
        ),
        reverse=True,
    )
    subs: list[tuple[SubFile, Subtitle]] = []
    for cand in cands[:limit]:
        if not client.download_remote_subtitle(item.id, cand.id):
            continue
        text = _read_sidecar_for(client, item, language, media_root)
        if text is None:
            continue
        subs.append(
            (
                SubFile(
                    file_id=0,
                    file_name=cand.name or cand.id,
                    language=cand.language or language,
                    format=cand.format or "srt",
                    download_count=cand.download_count or 0,
                    rating=float(cand.community_rating or 0),
                ),
                parse_srt(text),
            )
        )
    return subs


def _read_sidecar_for(client: JellyfinClient, item: Item, language: str, media_root: str | None = None) -> str | None:
    """Read the newest external subtitle file sitting next to the media."""
    from .client import language_aliases

    if not item.path:
        return None
    local = resolve_media_path(item.path, media_root)
    directory = os.path.dirname(local or item.path)
    base = os.path.splitext(os.path.basename(local or item.path))[0]
    want = {a.lower() for a in language_aliases(language)}
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    best: tuple[float, str] | None = None
    for name in entries:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".srt", ".ass", ".ssa", ".vtt"):
            continue
        if not stem.startswith(base):
            continue
        parts = stem.split(".")
        lang_tag = parts[-1].lower() if len(parts) > 1 else ""
        if lang_tag and lang_tag not in want:
            continue
        full = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, full)
    if best is None:
        return None
    with open(best[1], "rb") as f:
        return decode_subtitle(f.read())


def _provider_candidates(
    provider: OpenSubtitles, item: Item, moviehash: str, language: str, limit: int
) -> list[tuple[SubFile, Subtitle]]:
    lang2 = language[:2]
    hits = provider.search_by_hash(moviehash, languages=lang2)
    files = provider.best_files(hits, language, limit=limit)
    if not files:
        imdb = (item.provider_ids or {}).get("Imdb")
        if imdb:
            hits = provider.search_by_imdb(imdb, languages=lang2)
            files = provider.best_files(hits, language, limit=limit)
    subs: list[tuple[SubFile, Subtitle]] = []
    for f in files:
        try:
            subs.append((f, parse_srt(decode_subtitle(provider.download(f.file_id)))))
        except Exception as e:
            log.warning("[%s] candidate %s failed: %s", item.name, f.file_id, e)
    return subs


def verify_item(
    client: JellyfinClient,
    item: Item,
    cfg: VerifyConfig,
    provider: OpenSubtitles | None = None,
) -> VerifyResult:
    local_path = resolve_media_path(item.path or "", cfg.media_root)
    if local_path is None:
        return VerifyResult(
            item.id, item.name, "error",
            detail=f"media path missing/unreachable: {item.path}"
                   + (f" (media_root={cfg.media_root})" if cfg.media_root else ""),
        )
    moviehash = opensubtitles_hash(local_path)
    runtime_s = (item.runtime_ms or 0) / 1000.0
    log.info("[%s] moviehash=%s runtime=%.0fs", item.name, moviehash, runtime_s)

    subs: list[tuple[SubFile, Subtitle]] = []
    source = "none"
    if provider is not None and getattr(provider, "api_key", ""):
        try:
            subs = _provider_candidates(provider, item, moviehash, cfg.language, cfg.candidates)
            source = "opensubtitles-api"
        except ProviderError as e:
            log.warning("[%s] opensubtitles API failed (%s), falling back to Jellyfin plugin search", item.name, e)
    if not subs:
        try:
            subs = _jellyfin_plugin_candidates(client, item, cfg.language, cfg.candidates, cfg.media_root)
            source = "jellyfin-plugin" if subs else source
        except JellyfinError as e:
            log.warning("[%s] Jellyfin remote subtitle search failed: %s", item.name, e)
            return VerifyResult(
                item.id, item.name, "no-match", moviehash=moviehash,
                detail=f"Jellyfin remote search unavailable ({e}); install the Open Subtitles plugin or set OPENSUBTITLES_API_KEY",
            )
    if not subs:
        return VerifyResult(item.id, item.name, "no-match", moviehash=moviehash,
                            detail="no candidates (need OPENSUBTITLES_API_KEY or the Open Subtitles plugin in Jellyfin)")
    log.info("[%s] %d candidate subtitle files via %s", item.name, len(subs), source)

    words = transcribe_media(local_path, runtime_s, cfg)
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
        chosen=f"{best.file.file_name} [{source}]", speed=al.speed, offset_s=al.offset_s,
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
