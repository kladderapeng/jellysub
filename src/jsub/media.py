"""Local media inspection: OpenSubtitles movie hash and audio extraction.

Runs on the Jellyfin host (or any machine with the media mounted), reading
files directly via the Path field from the Jellyfin API.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
from dataclasses import dataclass

CHUNK = 65536


def opensubtitles_hash(path: str) -> str:
    """OpenSubtitles moviehash: little-endian hex of (size + first/last 64KiB)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(CHUNK)
        f.seek(max(0, size - CHUNK))
        tail = f.read(CHUNK)
    h = size
    for data in (head, tail):
        for i in range(0, len(data), 8):
            h += struct.unpack("<q", data[i : i + 8].ljust(8, b"\0"))[0]
    return f"{h & 0xFFFFFFFFFFFFFFFF:016x}"


def find_ffmpeg() -> str:
    """Locate an ffmpeg binary: PATH, Jellyfin's bundled ffmpeg, imageio-ffmpeg."""
    env = os.environ.get("FFMPEG_PATH")
    if env and os.path.exists(env):
        return env
    found = shutil.which("ffmpeg") or shutil.which("jellyfin-ffmpeg")
    if found:
        return found
    for candidate in (
        "/usr/lib/jellyfin-ffmpeg/ffmpeg",
        "/usr/lib/jellyfin/ffmpeg",
        "/opt/jellyfin/ffmpeg",
    ):
        if os.path.exists(candidate):
            return candidate
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


@dataclass
class AudioWindow:
    path: str
    start_s: float
    duration_s: float


def extract_audio_window(
    media_path: str,
    start_s: float,
    duration_s: float,
    out_wav: str,
    ffmpeg: str | None = None,
    audio_index: int = 0,
) -> AudioWindow:
    """Extract a mono 16 kHz PCM WAV window from the media's audio track."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("no ffmpeg binary found (set FFMPEG_PATH)")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start_s):.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", media_path,
        "-map", f"0:a:{audio_index}",
        "-ar", "16000", "-ac", "1", "-f", "wav", out_wav,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(out_wav):
        raise RuntimeError(
            f"ffmpeg audio extraction failed: {proc.stderr.strip()[:400]}"
        )
    return AudioWindow(path=out_wav, start_s=start_s, duration_s=duration_s)


def probe_audio_streams(media_path: str, ffmpeg: str | None = None) -> list[dict]:
    """Return ffprobe-style audio stream info (index, language, channels)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("no ffmpeg binary found (set FFMPEG_PATH)")
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.exists(ffprobe):
        ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe:
        return [{"index": 0, "language": None, "channels": None}]
    import json

    proc = subprocess.run(
        [
            ffprobe, "-hide_banner", "-loglevel", "error",
            "-select_streams", "a", "-show_entries",
            "stream=index,channels:stream_tags=language",
            "-of", "json", media_path,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return [{"index": 0, "language": None, "channels": None}]
    data = json.loads(proc.stdout or "{}")
    out = []
    for s in data.get("streams", []):
        out.append(
            {
                "index": int(s.get("index", 0)),
                "language": (s.get("tags") or {}).get("language"),
                "channels": s.get("channels"),
            }
        )
    return out or [{"index": 0, "language": None, "channels": None}]
