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
    from .client import language_aliases

    want = {a.lower() for a in language_aliases(language)}
    cands = []
    for lang in sorted(want):
        try:
            found = client.search_remote_subtitles(item.id, lang)
            log.info("plugin search lang=%s -> %d raw candidates", lang, len(found))
            cands.extend(found)
        except JellyfinError as e:
            log.warning("remote search for %s failed: %s", lang, e)
    seen = set()
    cands = [c for c in cands if not (c.id in seen or seen.add(c.id))]
    log.info("plugin search: %d candidates after dedupe, langs=%s",
             len(cands), sorted({(c.language or "?") for c in cands}))
    cands = [
        c
        for c in cands
        if (not c.language or c.language.lower() in want)
        and (c.format or "srt").lower() in ("srt", "ass", "ssa", "vtt")
    ]
    log.info("plugin search: %d candidates after language/format filter", len(cands))
    cands.sort(
        key=lambda c: (
            float(c.score or 0) + min(c.download_count or 0, 10000) / 100.0
            + (float(c.community_rating or 0) * 2),
        ),
        reverse=True,
    )
    subs: list[tuple[SubFile, Subtitle]] = []
    for cand in cands[:limit]:
        try:
            ok = client.download_remote_subtitle(item.id, cand.id)
        except JellyfinError as e:
            log.warning("download %s failed: %s", cand.id, e)
            continue
        if not ok:
            log.warning("download %s returned non-2xx", cand.id)
            continue
        text = _read_via_api(client, item, language) or _read_sidecar_for(client, item, language, media_root)
        if text is None:
            log.warning("downloaded %s but no subtitle stream appeared (check the Jellyfin server log — the 204 is returned even on failure)", cand.id)
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


def _read_via_api(client: JellyfinClient, item: Item, language: str) -> str | None:
    """Fetch the newest external subtitle stream through the Jellyfin API.

    Works even when the downloaded file hasn't hit the metadata yet, and does
    not depend on the on-disk naming convention.
    """
    from .client import language_aliases

    want = {a.lower() for a in language_aliases(language)}
    user_id = getattr(client, "_user_id", None) or client.current_user_id()
    log.info("[%s] re-read item id=%s user_id=%s", item.name, item.id, user_id)
    if user_id:
        client._user_id = user_id
    try:
        fresh = client.get_item(item.id, user_id=user_id)
    except JellyfinError as e:
        log.warning("[%s] re-read item failed: %s", item.name, e)
        return None
    if not fresh.media_source_id:
        return None
    subs = [s for s in fresh.subtitle_streams() if s.is_external]
    if not subs:
        return None
    target = None
    for s in reversed(subs):
        if not s.language or s.language.lower() in want:
            target = s
            break
    if target is None:
        target = subs[-1]
    try:
        raw = client.download_subtitle_stream(fresh.id, fresh.media_source_id, target.index, "srt")
    except JellyfinError as e:
        log.warning("[%s] stream download failed: %s", item.name, e)
        return None
    return decode_subtitle(raw)


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
    import logging as _logging

    _logging.getLogger("jsub.verify").info(
        "sidecar scan dir=%s base=%s want=%s files=%s", directory, base, sorted(want),
        [n for n in entries if os.path.splitext(n)[1].lower() in (".srt", ".ass", ".ssa", ".vtt")],
    )
    exact: tuple[float, str] | None = None
    any_lang: tuple[float, str] | None = None
    for name in entries:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".srt", ".ass", ".ssa", ".vtt"):
            continue
        if not stem.startswith(base):
            continue
        parts = stem.split(".")
        lang_tag = parts[-1].lower() if len(parts) > 1 else ""
        full = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if lang_tag and lang_tag in want:
            if exact is None or mtime > exact[0]:
                exact = (mtime, full)
        elif not lang_tag or lang_tag.isalpha():
            if any_lang is None or mtime > any_lang[0]:
                any_lang = (mtime, full)
    best = exact or any_lang
    if best is None:
        return None
    with open(best[1], "rb") as f:
        return decode_subtitle(f.read())


def _provider_candidates(
    provider: OpenSubtitles, item: Item, moviehash: str, language: str, limit: int,
    media_path: str | None = None,
) -> list[tuple[SubFile, Subtitle]]:
    from .media import query_terms_from_path

    lang2 = language[:2]
    files = provider.best_files(provider.search_by_hash(moviehash, languages=lang2), language, limit=limit)
    if not files:
        imdb = (item.provider_ids or {}).get("Imdb")
        if imdb:
            files = provider.best_files(provider.search_by_imdb(imdb, languages=lang2), language, limit=limit)
    if not files and media_path:
        terms = query_terms_from_path(media_path)
        parent_imdb = (item.provider_ids or {}).get("Imdb") if terms.get("season") else None
        log.info("[%s] hash/imdb search empty (hash=%s); query fallback: query=%r series=%r season=%s episode=%s parent_imdb=%s",
                 item.name, moviehash, terms["query"], terms.get("series"), terms.get("season"),
                 terms.get("episode"), parent_imdb)
        hits = provider.search_by_query(
            terms["query"], languages=lang2,
            season=terms.get("season"), episode=terms.get("episode"),
            parent_imdb_id=parent_imdb, moviehash=moviehash,
        )
        log.info("[%s] query returned %d hits", item.name, len(hits))
        files = provider.best_files(hits, language, limit=limit)
        if not files:
            hits = provider.search_by_query(terms.get("series") or terms["query"], languages=lang2)
            log.info("[%s] series-only query returned %d hits", item.name, len(hits))
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

    from .media import query_terms_from_path

    terms = query_terms_from_path(local_path)
    log.info(
        "[%s] parsed from path: query=%r series=%r season=%s episode=%s imdb=%s",
        item.name, terms["query"], terms.get("series"), terms.get("season"),
        terms.get("episode"), (item.provider_ids or {}).get("Imdb"),
    )

    subs: list[tuple[SubFile, Subtitle]] = []
    source = "none"
    if provider is not None and getattr(provider, "api_key", ""):
        try:
            subs = _provider_candidates(provider, item, moviehash, cfg.language, cfg.candidates, local_path)
            source = "opensubtitles-api"
        except ProviderError as e:
            log.warning("[%s] opensubtitles API failed (%s), falling back to Jellyfin plugin search", item.name, e)
    if not subs:
        try:
            subs = _jellyfin_plugin_candidates(client, item, cfg.language, cfg.candidates, cfg.media_root)
            source = "jellyfin-plugin" if subs else source
        except JellyfinError as e:
            log.warning("[%s] Jellyfin remote subtitle search failed: %s", item.name, e)
    if not subs:
        sidecar = _read_sidecar_for(client, item, cfg.language, cfg.media_root)
        if sidecar is not None:
            from .providers import SubFile as _SF

            subs = [(_SF(file_id=0, file_name="sidecar", language=cfg.language, format="srt"),
                     parse_srt(sidecar))]
            source = "sidecar"
            log.info("[%s] no remote candidates; using existing sidecar", item.name)
    if not subs:
        return VerifyResult(item.id, item.name, "no-match", moviehash=moviehash,
                            detail="no candidates (set OPENSUBTITLES_API_KEY, enable the Open Subtitles plugin, or place a sidecar .srt next to the file)")
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
