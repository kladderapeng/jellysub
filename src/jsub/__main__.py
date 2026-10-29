"""Command line interface for jsub.

Configuration (env vars, optionally overridden by a JSON file given with
--config): JELLYFIN_URL, JELLYFIN_API_KEY, JSUB_LANGUAGES (comma separated,
ISO 639-2 or 639-1), JSUB_OFFSET_MS, JSUB_DRY_RUN.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .client import JellyfinClient, JellyfinError
from .core import FetchConfig, FitConfig
from .pipeline import PipelineConfig, run_pipeline
from .verify import VerifyConfig, verify_item

log = logging.getLogger("jsub")


def load_config(args: argparse.Namespace) -> dict:
    cfg: dict = {}
    path = args.config or os.environ.get("JSUB_CONFIG")
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    env_map = {
        "url": "JELLYFIN_URL",
        "api_key": "JELLYFIN_API_KEY",
        "languages": "JSUB_LANGUAGES",
        "offset_ms": "JSUB_OFFSET_MS",
        "dry_run": "JSUB_DRY_RUN",
        "libraries": "JSUB_LIBRARIES",
        "min_score": "JSUB_MIN_SCORE",
        "candidates": "JSUB_CANDIDATES",
        "windows": "JSUB_WINDOWS",
        "window_seconds": "JSUB_WINDOW_SECONDS",
        "model_dir": "JSUB_MODEL_DIR",
        "ffmpeg": "FFMPEG_PATH",
    }
    for key, env in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    if args.url:
        cfg["url"] = args.url
    if args.api_key:
        cfg["api_key"] = args.api_key
    if args.languages:
        cfg["languages"] = args.languages
    if args.offset_ms is not None:
        cfg["offset_ms"] = args.offset_ms
    if args.dry_run:
        cfg["dry_run"] = True
    if args.libraries:
        cfg["libraries"] = args.libraries
    return cfg


def build_client(cfg: dict) -> JellyfinClient:
    url = cfg.get("url")
    api_key = cfg.get("api_key")
    if not url or not api_key:
        raise SystemExit(
            "Jellyfin URL and API key are required: set JELLYFIN_URL and "
            "JELLYFIN_API_KEY, or pass --url/--api-key (or use --config)."
        )
    return JellyfinClient(url, api_key)


def build_pipeline_config(cfg: dict, args: argparse.Namespace) -> PipelineConfig:
    languages = str(cfg.get("languages", "eng")).split(",")
    languages = [lang.strip() for lang in languages if lang.strip()]
    fetch = FetchConfig(
        languages=languages,
        min_score=float(cfg.get("min_score", 0.0)),
    )
    fit = FitConfig(offset_ms=float(cfg.get("offset_ms", 0.0)))
    libraries = None
    if cfg.get("libraries"):
        raw = str(cfg["libraries"])
        libraries = [x.strip() for x in raw.split(",") if x.strip()]
    return PipelineConfig(
        fetch=fetch,
        fit=fit,
        library_ids=libraries,
        dry_run=bool(cfg.get("dry_run", False)),
        do_fetch=args.command in ("fetch", "run"),
        do_fit=args.command in ("fit", "run"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jsub", description="Automate Jellyfin subtitle fetching and fitting."
    )
    parser.add_argument("--config", help="path to JSON config file")
    parser.add_argument("--url", help="Jellyfin base URL, e.g. http://localhost:8096")
    parser.add_argument("--api-key", help="Jellyfin API key")
    parser.add_argument("--languages", help="comma separated subtitle languages")
    parser.add_argument("--offset-ms", type=float, help="subtitle timing offset in ms")
    parser.add_argument("--libraries", help="comma separated library ids to limit scan")
    parser.add_argument("--dry-run", action="store_true", help="do not modify anything")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="show server info")
    sub.add_parser("scan", help="list libraries and items missing subtitles")
    sub.add_parser("fetch", help="download best remote subtitles for items missing them")
    sub.add_parser("fit", help="re-time existing subtitles (offset + clamp to runtime)")
    sub.add_parser("run", help="fetch and fit in one pass")
    sub.add_parser("align", help="verify+align subtitles against the media audio (runs on the server)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_config(args)
    client = build_client(cfg)

    if args.command == "info":
        info = client.server_info()
        print(json.dumps(info, indent=2))
        return 0

    if args.command == "scan":
        pipe = build_pipeline_config(cfg, args)
        for lib in (client.libraries() if not pipe.library_ids else [
            {"ItemId": i, "Name": i} for i in pipe.library_ids
        ]):
            lib_id = lib.get("ItemId") or lib.get("Id") or ""
            items = client.list_items(parent_id=lib_id or None)
            missing = [
                it for it in items
                if it.type in ("Movie", "Episode")
                and not any(it.has_subtitle(lang) for lang in pipe.fetch.languages)
            ]
            print(f"{lib.get('Name', lib_id)}: {len(missing)}/{len(items)} items missing subtitles")
            for it in missing:
                print(f"  - {it.type}  {it.name}  [{it.id}]")
        return 0

    if args.command == "align":
        from .providers import OpenSubtitles

        vcfg = VerifyConfig(
            language=(str(cfg.get("languages", "eng")).split(",")[0].strip()),
            candidates=int(cfg.get("candidates", 5)),
            windows=int(cfg.get("windows", 3)),
            window_seconds=float(cfg.get("window_seconds", 45.0)),
            model_dir=cfg.get("model_dir"),
            ffmpeg=cfg.get("ffmpeg"),
            dry_run=bool(cfg.get("dry_run", False)),
        )
        provider = OpenSubtitles() if os.environ.get("OPENSUBTITLES_API_KEY") else None
        lib_ids = None
        if cfg.get("libraries"):
            lib_ids = [x.strip() for x in str(cfg["libraries"]).split(",") if x.strip()]
        libraries = (
            [{"ItemId": i, "Name": i} for i in lib_ids]
            if lib_ids
            else client.libraries()
        )
        counts: dict[str, int] = {}
        for lib in libraries:
            lib_id = lib.get("ItemId") or lib.get("Id") or ""
            for item in client.list_items(parent_id=lib_id or None):
                if item.type not in ("Movie", "Episode"):
                    continue
                try:
                    res = verify_item(client, item, vcfg, provider)
                except Exception as e:
                    log.warning("[%s] align failed: %s", item.name, e)
                    res = type("R", (), {"action": "error", "name": item.name,
                                          "moviehash": None, "chosen": "", "speed": 1.0,
                                          "offset_s": 0.0, "rms_s": 0.0, "anchors": 0,
                                          "detail": str(e), "item_id": item.id})()
                counts[res.action] = counts.get(res.action, 0) + 1
                log.info(
                    "%-12s %-30s hash=%s file=%s speed=%.5f offset=%.2fs rms=%.3fs anchors=%d %s",
                    res.action, res.name, res.moviehash, res.chosen, res.speed,
                    res.offset_s, res.rms_s, res.anchors, res.detail,
                )
        print(json.dumps(counts, indent=2))
        return 0

    pipe = build_pipeline_config(cfg, args)
    try:
        results = run_pipeline(client, pipe)
    except JellyfinError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
        log.info("%-8s %-40s lang=%s %s", r.action, r.name, r.language, r.detail)
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
