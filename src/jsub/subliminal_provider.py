"""Subtitle candidates via the `subliminal` library (multi-provider).

subliminal covers podnapisi, addic7ed, opensubtitles(.com/.org), tvsubtitles,
subtis, subtitulamos, gestdown, bsplayer, napiprojekt. We use it as an
additional candidate source alongside opensubtitles.com direct and the
Jellyfin plugin: guessit-based video identification + per-provider query +
download, all handled by subliminal.

Requires the optional `subliminal` + `guessit` packages
(pip install 'jsub[subliminal]'). Some providers need credentials
(opensubtitles.com: OPENSUBTITLES_USERNAME/OPENSUBTITLES_PASSWORD or
OPENSUBTITLESCOM_API_KEY; addic7ed: ADDIC7ED_USERNAME/ADDIC7ED_PASSWORD);
keyless providers (podnapisi, opensubtitles.org, subtitulamos, ...) work
with no configuration.
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


def _video(path: str):
    from guessit import guessit
    from subliminal.video import Video

    return Video.fromguess(path, guessit(path))


def _langs(languages: list[str]):
    from subliminal.subtitle import Language

    out = []
    for lang in languages:
        for code in (lang, lang[:2]):
            try:
                out.append(Language.fromietf(code))
                break
            except Exception:
                continue
    return out or [Language.fromietf("en")]


def _provider_class(pname: str):
    from subliminal import provider_manager

    if pname not in provider_manager.names():
        return None
    return provider_manager[pname].plugin


def _init_provider(provider, pname: str):
    for init_name in ("initialize", "login"):
        init = getattr(provider, init_name, None)
        if callable(init):
            try:
                init()
                return True
            except Exception as e:
                log.warning("subliminal provider %s %s failed: %s", pname, init_name, e)
                return False
    return True


def _provider_credentials(pname: str) -> dict:
    if pname in ("opensubtitlescom", "opensubtitlescomvip", "opensubtitles", "opensubtitlesvip"):
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


def _fix_dogpile_cache():
    """subliminal 2.8's module-level cache region breaks with dogpile-cache >= 1.4
    ('_lock_registry'); reconfigure it in-memory."""
    try:
        import subliminal.cache as sc
        from dogpile.cache import make_region

        sc.region.configure("dogpile.cache.memory", expiration_time=3600)
    except Exception:
        pass


def _series_variants(v) -> list[str]:
    """Title variants to try: series name, with year, and parsed-from-path forms."""
    import re as _re

    names = []
    series = getattr(v, "series", None) or getattr(v, "title", None)
    if series:
        names.append(series)
        year = getattr(v, "year", None)
        if year:
            names.append(f"{series} {year}")
    import os as _os

    name = _os.path.splitext(_os.path.basename(getattr(v, "name", "") or ""))[0]
    m = _re.search(r"^(.*?)\s+[sS]\d{1,2}[eE]\d{1,3}", name)
    if m and m.group(1) not in names:
        names.append(m.group(1).strip())
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _query_provider(pname: str, video_path: str, languages: list[str]):
    """Query one subliminal provider; each provider has its own query signature."""
    from subliminal.subtitle import Language

    cls = _provider_class(pname)
    if cls is None:
        return []
    v = _video(video_path)
    langs = set(_langs(languages))
    from subliminal.video import Episode as _Episode
    is_episode = isinstance(v, _Episode) or getattr(v, "season", None) is not None
    kwargs = _provider_credentials(pname)

    if pname in ("podnapisi",):
        found = []
        for lang in langs:
            kw = dict(kwargs)
            if is_episode:
                kw.update(season=v.season, episode=getattr(v, "episode", None))
            found += cls().query(lang, getattr(v, "title", v.name), **kw)
        return found
    if pname in ("opensubtitles", "opensubtitlesvip"):
        if not (os.environ.get("OPENSUBTITLES_USERNAME") and os.environ.get("OPENSUBTITLES_PASSWORD")):
            log.info("subliminal provider %s needs OPENSUBTITLES_USERNAME/OPENSUBTITLES_PASSWORD (xmlrpc login), skipping", pname)
            return []
        kw = dict(kwargs)
        from .media import opensubtitles_hash

        try:
            kw["moviehash"] = opensubtitles_hash(video_path)
            kw["size"] = os.path.getsize(video_path)
        except OSError:
            pass
        if is_episode:
            kw["season"] = v.season
            kw["episode"] = getattr(v, "episode", None) or 1
            kw["query"] = (getattr(v, "series", None) or getattr(v, "title", ""))
        else:
            kw["query"] = getattr(v, "title", v.name)
        kw.pop("moviehash", None) if not kw.get("size") else None
        p = cls(**kwargs)
        p.initialize()
        return p.query(langs, **{k: x for k, x in kw.items() if x is not None})
    if pname in ("opensubtitlescom", "opensubtitlescomvip"):
        return cls(**kwargs).query(langs, **_episode_kwargs(v, kwargs, video_path))
    if pname == "addic7ed":
        if not is_episode:
            return []
        p = cls(**kwargs)
        p.initialize()
        return p.query(show_id=None, series=v.series, season=v.season)
    if pname == "tvsubtitles":
        if not is_episode:
            return []
        return cls(**kwargs).query(show_id=None, series=v.series, season=v.season,
                                    episode=getattr(v, "episode", None))
    if pname in ("subtitulamos",):
        p = cls(**kwargs)
        p.initialize()
        if not is_episode:
            return p.query(languages=langs)
        found = []
        for series in _series_variants(v):
            try:
                found = p.query(series=series, season=v.season,
                                episode=getattr(v, "episode", None), languages=langs)
            except Exception as e:
                log.info("subtitulamos series=%r failed: %s", series, e)
                continue
            if found:
                log.info("subtitulamos matched series=%r -> %d subs", series, len(found))
                break
        return found
    if pname == "subtis":
        from subliminal.video import Movie

        if not isinstance(v, Movie):
            return []
        return cls().query(v, langs)
    if pname == "bsplayer":
        kw = dict(kwargs)
        try:
            from .media import opensubtitles_hash

            kw["file_hash"] = opensubtitles_hash(video_path)
            kw["size"] = os.path.getsize(video_path)
        except OSError:
            pass
        return cls().query(langs, **kw)
    if pname == "napiprojekt":
        try:
            from .media import opensubtitles_hash

            h = opensubtitles_hash(video_path)
        except OSError:
            return []
        out = []
        for lang in langs:
            out += cls().query(lang, h)
        return out
    if pname == "gestdown":
        if not is_episode:
            return []
        out = []
        for lang in langs:
            out += cls().query(None, v.series, v.season, getattr(v, "episode", None), lang)
        return out
    # generic fallback
    p = cls(**kwargs)
    if not _init_provider(p, pname):
        return []
    try:
        return p.query(langs, v)
    except TypeError:
        try:
            return p.query(v, langs)
        except Exception:
            return []


def _episode_kwargs(v, kwargs: dict, video_path: str) -> dict:
    kw = {}
    if getattr(v, "type", "") == "episode" or getattr(v, "season", None) is not None:
        kw["season"] = v.season
        kw["episode"] = getattr(v, "episode", None)
        kw["query"] = getattr(v, "series", None) or getattr(v, "title", "")
    return kw


def subliminal_candidates(
    video_path: str,
    languages: list[str],
    providers: list[str] | None = None,
    limit: int = 10,
) -> list[SubCandidate]:
    """Search subliminal providers for subtitles matching the video file."""
    try:
        import subliminal  # noqa: F401
        import guessit  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "subliminal/guessit not importable in this environment; "
            "install with: pip install 'jsub[subliminal]' or apt python3-subliminal python3-guessit"
        ) from e

    _fix_dogpile_cache()
    chosen = providers or ["podnapisi", "opensubtitles", "opensubtitlescom", "addic7ed", "tvsubtitles", "subtitulamos"]
    out: list[SubCandidate] = []
    for pname in chosen:
        try:
            found = _query_provider(pname, video_path, languages)
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
    cls = _provider_class(candidate.provider)
    if cls is None:
        raise RuntimeError(f"unknown provider {candidate.provider}")
    v = _video(video_path)
    langs = set(_langs([candidate.language]))
    kwargs = _provider_credentials(candidate.provider)
    pname = candidate.provider

    if pname == "podnapisi":
        provider = cls()
        lang = next(iter(langs))
        kw = dict(kwargs)
        if getattr(v, "season", None) is not None:
            kw.update(season=v.season, episode=getattr(v, "episode", None))
        subs = provider.query(lang, getattr(v, "title", v.name), **kw)
    elif pname in ("opensubtitles", "opensubtitlesvip", "opensubtitlescom", "opensubtitlescomvip"):
        found = _query_provider(pname, video_path, [candidate.language])
        subs = found
    else:
        subs = _query_provider(pname, video_path, [candidate.language])

    sub = next((s for s in subs if str(s.id) == candidate.subtitle_id), None)
    if sub is None and subs:
        sub = subs[0]
    if sub is None:
        raise RuntimeError(f"candidate {candidate.subtitle_id} not found on re-query ({pname})")
    provider = sub.provider if hasattr(sub, "provider") else None
    if provider is None:
        provider = cls(**kwargs)
        _init_provider(provider, pname)
    provider.download_subtitle(sub)
    if not getattr(sub, "content", None):
        raise RuntimeError("download returned empty content")
    return sub.content
