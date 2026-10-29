"""jsub: automate Jellyfin subtitle fetching and fitting."""

from .align import Alignment, Anchor, evaluate, fit_linear, match_cues, robust_fit, transform_subtitle
from .client import Item, JellyfinClient, JellyfinError, MediaStream, RemoteSubtitle
from .core import FetchConfig, FitConfig, ItemResult
from .media import extract_audio_window, find_ffmpeg, opensubtitles_hash, probe_audio_streams
from .pipeline import PipelineConfig, run_pipeline
from .srt import Cue, Subtitle, fit_srt, parse_srt, render_srt, shift_srt

__version__ = "0.1.0"

__all__ = [
    "Alignment",
    "Anchor",
    "Cue",
    "FetchConfig",
    "FitConfig",
    "Item",
    "ItemResult",
    "JellyfinClient",
    "JellyfinError",
    "MediaStream",
    "PipelineConfig",
    "RemoteSubtitle",
    "Subtitle",
    "evaluate",
    "extract_audio_window",
    "find_ffmpeg",
    "fit_linear",
    "fit_srt",
    "match_cues",
    "opensubtitles_hash",
    "parse_srt",
    "probe_audio_streams",
    "render_srt",
    "robust_fit",
    "run_pipeline",
    "shift_srt",
    "transform_subtitle",
]
