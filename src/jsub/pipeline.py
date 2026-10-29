"""End-to-end pipeline: scan libraries, fetch missing subtitles, fit them."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .client import Item, JellyfinClient, JellyfinError
from .core import (
    FetchConfig,
    FitConfig,
    ItemResult,
    fetch_and_fit,
    fit_uploaded,
    iso639_2_to_3,
)

log = logging.getLogger("jsub")


@dataclass
class PipelineConfig:
    fetch: FetchConfig
    fit: FitConfig
    library_ids: list[str] | None = None
    dry_run: bool = False
    do_fetch: bool = True
    do_fit: bool = True


def run_pipeline(client: JellyfinClient, cfg: PipelineConfig) -> list[ItemResult]:
    results: list[ItemResult] = []
    if cfg.library_ids:
        libraries = [
            {"ItemId": lid, "Name": lid} for lid in cfg.library_ids
        ]
    else:
        libraries = client.libraries()
    for lib in libraries:
        lib_id = lib.get("ItemId") or lib.get("Id") or ""
        lib_name = lib.get("Name", lib_id)
        log.info("scanning library %s (%s)", lib_name, lib_id)
        try:
            items = client.list_items(parent_id=lib_id or None)
        except JellyfinError as e:
            log.warning("library %s scan failed: %s", lib_name, e)
            continue
        for item in items:
            if item.type not in ("Movie", "Episode"):
                continue
            if cfg.do_fetch:
                results.extend(fetch_and_fit(client, item, cfg.fetch, cfg.fit, cfg.dry_run))
            if cfg.do_fit:
                for language in cfg.fetch.languages:
                    fresh = None
                    try:
                        fresh = client.get_item(item.id)
                    except JellyfinError as e:
                        log.warning("[%s] item lookup failed: %s", item.name, e)
                        continue
                    if fresh.has_subtitle(language) or fresh.has_subtitle(
                        iso639_2_to_3(language)
                    ):
                        results.append(
                            fit_uploaded(client, fresh, language, cfg.fit, cfg.dry_run)
                        )
    return results
