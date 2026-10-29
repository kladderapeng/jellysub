"""Local mode: align a subtitle for a media file directly, no Jellyfin needed.

Given a media file, compute its OpenSubtitles moviehash, fetch the top-N
candidate subtitles (opensubtitles.com when OPENSUBTITLES_API_KEY is set,
otherwise any existing sidecar *.srt next to the file is used as the only
candidate), transcribe sampled audio windows with ASR, align, and write the
corrected subtitle as <basename>.<lang>.srt next to the media file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .align import evaluate, transform_subtitle
from .asr import Word
from .media import opensubtitles_hash
from .providers import OpenSubtitles, ProviderError, SubFile, decode_subtitle
from .srt import parse_srt, render_srt
from .verify import VerifyConfig, score_candidates, transcribe_media

log = logging.getLogger("jsub.local")


@dataclass
class LocalResult:
    media_path: str
    action: str
    out_path: str | None = None
    chosen: str = ""
    speed: float = 1.0
    offset_s: float = 0.0
    rms_s: float = 0.0
    anchors: int = 0
    detail: str = ""


def sidecar_candidates(media_path: str, language: str) -> list[tuple[SubFile, object]]:
    """Existing .srt/.ass/.ssa sidecars next to the media file as candidates."""
    from .client import language_aliases

    directory = os.path.dirname(media_path)
    base = os.path.splitext(os.path.basename(media_path))[0]
    want = {a.lower() for a in language_aliases(language)}
    out = []
    try:
        entries = os.listdir(directory)
    except OSError:
        return out
    for name in sorted(entries, key=lambda n: os.path.getmtime(os.path.join(directory, n)), reverse=True):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".srt", ".ass", ".ssa", ".vtt"):
            continue
        if not stem.startswith(base):
            continue
        parts = stem.split(".")
        tag = parts[-1].lower() if len(parts) > 1 else ""
        if tag and tag not in want:
            continue
        with open(os.path.join(directory, name), "rb") as f:
            text = decode_subtitle(f.read())
        sub = parse_srt(text)
        out.append((SubFile(file_id=0, file_name=name, language=tag or language, format=ext[1:]), sub))
    return out


def align_local_file(
    media_path: str,
    language: str = "eng",
    vcfg: VerifyConfig | None = None,
    provider: OpenSubtitles | None = None,
    sub_path: str | None = None,
) -> LocalResult:
    vcfg = vcfg or VerifyConfig(language=language)
    vcfg.language = language
    if not os.path.exists(media_path):
        return LocalResult(media_path, "error", detail=f"no such file: {media_path}")
    moviehash = opensubtitles_hash(media_path)
    runtime_s = _probe_runtime_s(media_path, vcfg)
    log.info("[%s] moviehash=%s runtime=%.0fs", os.path.basename(media_path), moviehash, runtime_s)

    if sub_path:
        if not os.path.exists(sub_path):
            return LocalResult(media_path, "error", detail=f"no such subtitle file: {sub_path}")
        with open(sub_path, "rb") as f:
            subs = [(SubFile(file_id=0, file_name=os.path.basename(sub_path),
                             language=language, format=os.path.splitext(sub_path)[1][1:]),
                      parse_srt(decode_subtitle(f.read())))]
        source = "explicit --sub"
    else:
        subs = sidecar_candidates(media_path, language)
        source = "sidecar"
    if provider is not None and getattr(provider, "api_key", ""):
        try:
            lang2 = language[:2]
            hits = provider.search_by_hash(moviehash, languages=lang2)
            files = provider.best_files(hits, language, limit=vcfg.candidates)
            api_subs = []
            for f in files:
                try:
                    api_subs.append((f, parse_srt(decode_subtitle(provider.download(f.file_id)))))
                except Exception as e:
                    log.warning("candidate %s failed: %s", f.file_id, e)
            if api_subs:
                subs = api_subs
                source = "opensubtitles-api"
        except ProviderError as e:
            log.warning("opensubtitles API failed (%s), using sidecar only", e)
    if not subs:
        return LocalResult(media_path, "no-match", detail="no candidate subtitles (set OPENSUBTITLES_API_KEY or place a sidecar .srt next to the file)")
    log.info("[%s] %d candidate subtitle(s) via %s", os.path.basename(media_path), len(subs), source)

    words = transcribe_media(media_path, runtime_s, vcfg)
    if not words:
        return LocalResult(media_path, "error", detail="ASR produced no words")
    log.info("[%s] ASR produced %d words", os.path.basename(media_path), len(words))

    scored = score_candidates(subs, words)
    best = scored[0]
    if best.alignment is None or best.score <= 0:
        return LocalResult(media_path, "no-match", detail=f"best score {best.score:.3f}, no usable alignment")
    al = best.alignment
    corrected = render_srt(transform_subtitle(best.sub, al))
    base = os.path.splitext(media_path)[0]
    from .client import iso639_2_to_3

    lang_tag = iso639_2_to_3(best.file.language or language)[:2] if len(best.file.language or language) <= 3 else (best.file.language or language)[:2]
    out_path = f"{base}.{lang_tag}.srt"
    log.info(
        "[%s] chose %s: speed=%.5f offset=%.2fs rms=%.3fs anchors=%d -> %s",
        os.path.basename(media_path), best.file.file_name, al.speed, al.offset_s, al.rms_s,
        len(al.anchors), out_path,
    )
    if vcfg.dry_run:
        return LocalResult(media_path, "would-align", out_path=out_path, chosen=best.file.file_name,
                           speed=al.speed, offset_s=al.offset_s, rms_s=al.rms_s, anchors=len(al.anchors))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(corrected)
    return LocalResult(media_path, "aligned", out_path=out_path, chosen=best.file.file_name,
                       speed=al.speed, offset_s=al.offset_s, rms_s=al.rms_s, anchors=len(al.anchors))


def _probe_runtime_s(media_path: str, vcfg: VerifyConfig) -> float:
    """Best-effort duration via ffprobe; falls back to 0 (windows then start at 0)."""
    import json
    import shutil
    import subprocess

    from .media import find_ffmpeg

    ffmpeg = vcfg.ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return 0.0
    ffprobe = shutil.which("ffprobe") or os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(ffprobe):
        return 0.0
    try:
        proc = subprocess.run(
            [ffprobe, "-hide_banner", "-loglevel", "error", "-show_entries",
             "format=duration", "-of", "json", media_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception as e:
        log.warning("ffprobe failed: %s", e)
    return 0.0
