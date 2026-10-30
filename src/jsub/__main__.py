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


def default_config_paths() -> list[str]:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return [
        os.path.join(xdg, "jsub", "config.json"),
        os.path.join(os.getcwd(), "jsub.json"),
        os.path.join(os.getcwd(), "config.json"),
    ]


def load_config(args: argparse.Namespace) -> dict:
    cfg: dict = {}
    path = getattr(args, "config", None) or os.environ.get("JSUB_CONFIG")
    if not path:
        for cand in default_config_paths():
            if os.path.exists(cand):
                path = cand
                break
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
        log.info("loaded config from %s", path)
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
        "media_root": "JSUB_MEDIA_ROOT",
        "user_id": "JSUB_USER_ID",
        "os_api_key": "OPENSUBTITLES_API_KEY",
        "os_username": "OPENSUBTITLES_USERNAME",
        "os_password": "OPENSUBTITLES_PASSWORD",
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
    if getattr(args, "path", None):
        cfg["path"] = args.path
    if getattr(args, "media_root", None):
        cfg["media_root"] = args.media_root
    if getattr(args, "sub", None):
        cfg["sub"] = args.sub
    if getattr(args, "user_id", None):
        cfg["user_id"] = args.user_id
    for key in ("os_api_key", "os_username", "os_password"):
        val = cfg.get(key)
        if val:
            os.environ[{"os_api_key": "OPENSUBTITLES_API_KEY",
                        "os_username": "OPENSUBTITLES_USERNAME",
                        "os_password": "OPENSUBTITLES_PASSWORD"}[key]] = str(val)
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
        path=cfg.get("path"),
        dry_run=bool(cfg.get("dry_run", False)),
        do_fetch=args.command in ("fetch", "run"),
        do_fit=args.command in ("fit", "run"),
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to JSON config file (keys: url, api_key, languages, os_api_key, os_username, os_password, ...)")
    common.add_argument("--url", help="Jellyfin base URL, e.g. http://localhost:8096")
    common.add_argument("--api-key", help="Jellyfin API key")
    common.add_argument("--languages", help="comma separated subtitle languages")
    common.add_argument("--offset-ms", type=float, help="subtitle timing offset in ms")
    common.add_argument("--libraries", help="comma separated library ids to limit scan")
    common.add_argument("--path", help="limit to a local media directory (all files in it) or a single file, matched against item paths, e.g. /data/series/Friends/Season 01")
    common.add_argument("--media-root", help="local mount point corresponding to Jellyfin's media root, e.g. /mnt when Jellyfin sees /media/... (used by align to read files)")
    common.add_argument("--sub", help="explicit subtitle file to align (local mode), no provider/API key needed")
    common.add_argument("--user-id", help="Jellyfin user id to pass as UserId (default: resolved via /Users/Me, then /Users admin list)")
    common.add_argument("--dry-run", action="store_true", help="do not modify anything")
    common.add_argument("--verbose", "-v", action="store_true")
    parser = argparse.ArgumentParser(
        prog="jsub", description="Automate Jellyfin subtitle fetching and fitting.",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)
    local_sub = None
    for name, help_text in [
        ("info", "show server info"),
        ("scan", "list libraries and items missing subtitles"),
        ("fetch", "download best remote subtitles for items missing them"),
        ("fit", "re-time existing subtitles (offset + clamp to runtime)"),
        ("run", "fetch and fit in one pass"),
        ("align", "verify+align subtitles against the media audio (runs on the server)"),
        ("local", "align a subtitle for a local media file directly (no Jellyfin); writes <name>.<lang>.srt next to the file"),
    ]:
        sp = sub.add_parser(name, parents=[common], help=help_text)
        if name == "local":
            sp.add_argument("file", nargs="?", help="media file path to align")
    return parser


def _merge_common_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in options given before the subcommand when the subparser's own
    value is the default (argparse subparsers don't inherit main-parser
    values when the flag appears only before the subcommand)."""
    common = getattr(args, "_common", None)
    if common is None:
        return args
    for action in common._actions:
        if action.dest in ("help",):
            continue
        name = f"--{action.dest.replace('_', '-')}"
        if name in sys.argv and getattr(args, action.dest) in (None, False, action.default):
            pass
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if argv is None:
        argv = sys.argv[1:]
    if getattr(args, "config", None) is None:
        for i, a in enumerate(argv):
            if a == "--config" and i + 1 < len(argv):
                args.config = argv[i + 1]
                break
            if a.startswith("--config="):
                args.config = a.split("=", 1)[1]
                break
    sub_pos = next((i for i, a in enumerate(argv) if a in
                    ("info", "scan", "fetch", "fit", "run", "align")), None)
    if sub_pos is not None:
        before = argv[:sub_pos]
        flags = {f"--{a.dest.replace('_', '-')}": a for a in parser._actions}
        flags["-v"] = flags.get("--verbose")
        for i, a in enumerate(before):
            action = None
            val = None
            if a in flags:
                action = flags[a]
                if action.dest in ("dry_run", "verbose", "help"):
                    val = True
                elif i + 1 < len(before):
                    val = before[i + 1]
            elif "=" in a and a.split("=", 1)[0] in flags:
                action = flags[a.split("=", 1)[0]]
                val = a.split("=", 1)[1]
            if action is None or action.dest == "help":
                continue
            if getattr(args, action.dest) in (None, False):
                if action.dest in ("dry_run", "verbose"):
                    setattr(args, action.dest, True)
                elif action.type is float:
                    setattr(args, action.dest, float(val))
                else:
                    setattr(args, action.dest, val)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = load_config(args)
    client = build_client(cfg) if args.command != "local" else None
    if client is not None and cfg.get("user_id"):
        client._user_id = cfg["user_id"]

    if args.command == "info":
        info = client.server_info()
        try:
            plugins = client.plugins()
        except JellyfinError as e:
            plugins = [{"error": str(e)}]
        out = {"server": info, "plugins": [
            {"Name": p.get("Name"), "Version": p.get("Version"),
             "Status": p.get("Status"), "Id": p.get("Id")}
            for p in plugins
        ]}
        print(json.dumps(out, indent=2))
        return 0

    if args.command == "scan":
        pipe = build_pipeline_config(cfg, args)
        from .pipeline import collect_items

        items = collect_items(client, pipe)
        missing = [
            it for it in items
            if it.type in ("Movie", "Episode")
            and not any(it.has_subtitle(lang) for lang in pipe.fetch.languages)
        ]
        print(f"{len(missing)}/{len(items)} items missing subtitles")
        for it in missing:
            print(f"  - {it.type}  {it.name}  [{it.id}]  {it.path or ''}")
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
            media_root=cfg.get("media_root"),
        )
        provider = OpenSubtitles() if (os.environ.get("OPENSUBTITLES_API_KEY") or os.environ.get("OPENSUBTITLES_USERNAME")) else None
        from .pipeline import collect_items

        vpipe = build_pipeline_config(cfg, args)
        counts: dict[str, int] = {}
        for item in collect_items(client, vpipe):
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

    if args.command == "local":
        from .local import align_local_file
        from .providers import OpenSubtitles

        media_path = getattr(args, "file", None) or cfg.get("path")
        if not media_path:
            print("error: local needs a file path (positional arg or --path FILE)", file=sys.stderr)
            return 2
        vcfg = VerifyConfig(
            language=(str(cfg.get("languages", "eng")).split(",")[0].strip()),
            candidates=int(cfg.get("candidates", 5)),
            windows=int(cfg.get("windows", 3)),
            window_seconds=float(cfg.get("window_seconds", 45.0)),
            model_dir=cfg.get("model_dir"),
            ffmpeg=cfg.get("ffmpeg"),
            dry_run=bool(cfg.get("dry_run", False)),
        )
        provider = OpenSubtitles() if (os.environ.get("OPENSUBTITLES_API_KEY") or os.environ.get("OPENSUBTITLES_USERNAME")) else None
        res = align_local_file(media_path, vcfg.language, vcfg, provider, sub_path=cfg.get("sub"))
        log.info(
            "%-12s %s -> %s speed=%.5f offset=%.2fs rms=%.3fs anchors=%d %s",
            res.action, res.media_path, res.out_path, res.speed, res.offset_s, res.rms_s,
            res.anchors, res.detail,
        )
        print(json.dumps({"action": res.action, "out": res.out_path, "chosen": res.chosen,
                          "speed": res.speed, "offset_s": res.offset_s, "rms_s": res.rms_s,
                          "anchors": res.anchors, "detail": res.detail}, indent=2))
        return 0 if res.action in ("aligned", "would-align") else 1

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
