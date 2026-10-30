"""Subtitle candidates via the `subliminal` library (multi-provider).

subliminal covers podnapisi, addic7ed, opensubtitles(.com/.org), tvsubtitles,
subtis, subtitulamos, gestdown, bsplayer, napiprojekt. We use it as an
additional candidate source alongside opensubtitles.com direct and the
Jellyfin plugin: hash-based video identification + provider search +
download, all handled by subliminal.

Requires the optional `subliminal` package (pip install 'jsub[subliminal]').
Some providers need credentials (opensubtitles.com: OPENSUBTITLES_USERNAME/
OPENSUBTITLES_PASSWORD or OPENSUBTITLESCOM_API_KEY env; addic7ed:
ADDIC7ED_USERNAME/ADDIC7ED_PASSWORD). Keyless providers (podnapisi, tvsubtitles,
...) work with no configuration.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("jsub.subliminal")


@dataclass
class SubCandidate:
    provider: str
    subtitle_id: str
    name: str
    language: str
    format: str
    score: float = 0.0


def subliminal_candidates(
    video_path: str,
    languages: list[str],
    providers: list[str] | None = None,
    limit: int = 10,
) -> list[SubCandidate]:
    """Search subliminal providers for subtitles matching the video file.

    Returns candidates (not yet downloaded). Raises ImportError if the
    optional `subliminal` dependency is missing.
    """
    try:
        from subliminal import Episode, Movie, Video, provider_manager
        from subliminal.providers import Provider
    except ImportError as e:
        raise ImportError(
            "subliminal is not installed; run: pip install 'jsub[subliminal]'"
        ) from e

    from subliminal.core import provider_registry

    v = Video.fromguess(None, _guess_info(video_path))
    if isinstance(v, Episode) or getattr(v, "series", None):
        pass
    langs = _subliminal_languages(languages)

    chosen = providers or ["podnapisi", "opensubtitlescom", "opensubtitles", "addic7ed", "tvsubtitles"]
    out: list[SubCandidate] = []
    for pname in chosen:
        if pname not in provider_registry:
            log.warning("unknown subliminal provider %s, skipping", pname)
            continue
        provider_cls = provider_registry[pname]
        kwargs = _provider_credentials(pname)
        try:
            provider = provider_cls(**kwargs)
            if hasattr(provider, "login") and callable(getattr(provider, "login")):
                try:
                    provider.login()
                except Exception as e:
                    log.warning("subliminal provider %s login failed: %s", pname, e)
                    continue
            found = provider.query(langs, v)
            log.info("subliminal provider %s -> %d candidates", pname, len(found))
            for s in found:
                out.append(
                    SubCandidate(
                        provider=pname,
                        subtitle_id=str(s.id),
                        name=str(getattr(s, "name", "") or s.id),
                        language=str(getattr(s, "language", "")),
                        format=str(getattr(s, "format", "srt")),
                        score=float(getattr(s, "score", 0) or 0),
                    )
                )
        except Exception as e:
            log.warning("subliminal provider %s failed: %s", pname, e)
        if len(out) >= limit:
            break
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:limit]


def subliminal_download(candidate: SubCandidate, video_path: str) -> bytes:
    """Download and decode a candidate subtitle via subliminal."""
    from subliminal import Video
    from subliminal.core import provider_registry

    v = Video.fromguess(None, _guess_info(video_path))
    provider = provider_registry[candidate.provider](**_provider_credentials(candidate.provider))
    if hasattr(provider, "login") and callable(getattr(provider, "login")):
        provider.login()
    subs = provider.query(_subliminal_languages([candidate.language]), v)
    sub = next((s for s in subs if str(s.id) == candidate.subtitle_id), None)
    if sub is None:
        raise RuntimeError(f"candidate {candidate.subtitle_id} not found on re-query")
    provider.download_subtitle(sub)
    return sub.content


def _guess_info(path: str) -> dict:
    import os

    from guessit import guessit

    return guessit(path)


def _subliminal_languages(languages: list[str]):
    from subliminal import Language

    out = []
    for lang in languages:
        for code in (lang, lang[:2]):
            try:
                out.append(Language.fromietf(code))
                break
            except Exception:
                continue
    return out or [Language.fromietf("en")]


def _provider_credentials(pname: str) -> dict:
    if pname in ("opensubtitlescom", "opensubtitles"):
        kw: dict = {}
        u = os.environ.get("OPENSUBTITLES_USERNAME")
        p = os.environ.get("OPENSUBTITLES_PASSWORD")
        if u and p:
            kw["username"], kw["password"] = u, p
        key = os.environ.get("OPENSUBTITLESCOM_API_KEY") or os.environ.get("OPENSUBTITLES_API_KEY")
        if key:
            kw["apikey"] = key
        return kw
    if pname == "addic7ed":
        u = os.environ.get("ADDIC7ED_USERNAME")
        p = os.environ.get("ADDIC7ED_PASSWORD")
        if u and p:
            return {"username": u, "password": p}
        return {}
    return {}
