# jsub — Jellyfin subtitle fetch & fit automation

A small tool (Python stdlib core; optional `vosk` + ffmpeg for media-side
alignment) that automates subtitle handling on a Jellyfin server.

Two modes:

- **API mode** (runs anywhere with network access to Jellyfin): find items
  missing subtitles, download the best remote match via Jellyfin's subtitle
  provider search, apply a fixed timing offset, clamp to runtime, re-upload.
- **align mode** (run **on the Jellyfin server**, where the media files are
  reachable): identify the exact media file, find the right subtitle by
  analyzing the audio itself, and correct offset *and* speed.

- **scan** — list libraries and which Movies/Episodes are missing subtitles in
  your languages.
- **fetch** — for each item missing a subtitle, query Jellyfin's remote
  subtitle search (`GET /Items/{id}/Remote/Subtitles/Search`), score the
  candidates (language match, SRT preferred, match score, download count,
  community rating), and download the best one
  (`POST /Items/{id}/Remote/Subtitles/{subtitleId}`).
- **fit** — re-read the item, download the external subtitle stream, apply a
  timing offset and clamp all cues to the media runtime (drops cues past the
  end, clamps overruns, removes degenerate cues), then re-upload it
  (`POST /Videos/{id}/Subtitles`) and trigger a metadata refresh.
- **run** — fetch + fit in one pass.

## Setup

Prefer your distro's package manager over pip where possible. `jsub` itself
is not packaged anywhere yet, so install it from a clone with
`pip install -e .` (API mode needs no third-party packages at all).

### Debian / Ubuntu (apt)

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg python3-pytest
# vosk is NOT in Debian's repos — use pipx (PEP 668-safe) or the project venv:
sudo apt install pipx && pipx install vosk      # or: pip install vosk inside the venv

git clone https://github.com/kladderapeng/jellysub.git && cd jellysub
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # add '.[align]' only if you didn't use apt/pipx above
```

`ffmpeg` from apt is picked up automatically (`media.py` prefers a system
ffmpeg over the bundled one), so the `imageio-ffmpeg` extra is optional there.
Don't `sudo pip install` into system Python on bookworm/trixie — PEP 668
blocks it; use `apt install python3-<module>` when it exists, else venv/pipx.

### Arch Linux (pacman)

```bash
sudo pacman -Syu
sudo pacman -S python python-vosk ffmpeg python-pytest   # all in [extra], no AUR needed

git clone https://github.com/kladderapeng/jellysub.git && cd jellysub
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

### Generic / pip-only fallback

```bash
pip install -e .                 # API mode, stdlib only
pip install -e '.[align]'        # + vosk + bundled ffmpeg binary for align mode
```

### The `externally-managed-environment` pip error (PEP 668)

On Debian 12+/Ubuntu 24.04+ (and other modern distros) system Python is
protected; `pip install` outside a venv fails with
`error: externally-managed-environment`. The canonical fixes, in order of
preference — do **not** use `--break-system-packages`:

1. **apt first**: `sudo apt install python3-<module>` whenever the module is
   packaged (e.g. `python3-pytest`, `python3-requests`).
2. **venv for the project**: `python3 -m venv .venv && . .venv/bin/activate`,
   then `pip install -e .` inside it — this is what the examples above do.
3. **pipx for CLI tools**: `sudo apt install pipx && pipx install vosk` —
   pipx creates an isolated venv per tool and puts the binary on your PATH.
4. Only if you truly know what you're doing: `pip install --break-system-packages`.

### Credentials

Create an API key in Jellyfin: Dashboard → Administration → API Keys.

**No OpenSubtitles API key is required** if the Open Subtitles plugin is
installed in Jellyfin (the plugin itself only needs an opensubtitles.com
account, set in the plugin's settings): `align` then obtains candidates via
Jellyfin's own remote search (`/Items/{id}/Remote/Subtitles/Search`) and
reads the downloaded sidecar file from the media directory. Set
`OPENSUBTITLES_API_KEY` only if you also want direct opensubtitles.com
queries — the moviehash lookup there finds the exact release even when the
plugin's search doesn't. Without either, `align` reports `no-match`.

Additionally, `pip install -e '.[subliminal]'` enables the
[subliminal](https://subliminal.readthedocs.io/) multi-provider backend
(podnapisi, addic7ed, opensubtitles, tvsubtitles, subtitulamos, ...) as a
candidate source in both `align` and `local`, using hash/name-based video
identification via guessit. Keyless providers (podnapisi, tvsubtitles, ...)
work with no credentials; opensubtitles.com/addic7ed read
`OPENSUBTITLES_USERNAME`/`OPENSUBTITLES_PASSWORD`/`OPENSUBTITLESCOM_API_KEY`
and `ADDIC7ED_USERNAME`/`ADDIC7ED_PASSWORD` from the environment.
Candidate order: opensubtitles.com API (if key) → Jellyfin plugin →
subliminal providers → existing sidecar file.

Vosk ASR models (~40 MB each, per language) are data, not packages — they
auto-download on first use into `JSUB_MODEL_DIR` (default `~/.cache/jsub/models`).

## Usage

```bash
export JELLYFIN_URL=http://localhost:8096
export JELLYFIN_API_KEY=<your-api-key>

jsub info                                   # server info
jsub scan                                   # what is missing subtitles?
jsub run --languages eng,deu --offset-ms 250
jsub run --dry-run                          # see what would happen
jsub fit --languages eng                    # only re-time existing subs
```

### Selecting only part of the library

`--path` limits any command to a local media directory (recursively) or a
single file, matched against each item's `Path` — handy with Jellyfin's usual
layout:

```bash
jsub run --path /data/movies/Movie\ (1920)                 # one movie folder
jsub align --path /data/series/Friends/Season\ 01          # one season
jsub scan --path /data/series/Friends/S01E01.mp4           # a single episode file
```

(The path must be the server-side path as Jellyfin sees it, since `--path`
matches item `Path` values; when running on the server itself they are the
same.)

### align mode (on the server)

```bash
jsub align --languages eng                  # verify + align all items
jsub align --languages deu --dry-run        # preview, upload nothing
```

### local mode (no Jellyfin at all)

If you just have a media file, skip Jellyfin entirely — `local` works directly
on the file and writes the corrected subtitle next to it as
`<name>.<lang>.srt`:

```bash
jsub local /mnt/media/movies/Movie\ (1920)/Movie.mkv --languages eng
# candidates: sidecar .srt next to the file, or opensubtitles.com by moviehash
# when OPENSUBTITLES_API_KEY is set
jsub local Movie.mkv --sub downloaded.srt   # align a specific subtitle file —
                                            # no API key / plugin needed at all
jsub local Movie.mkv --dry-run              # preview, write nothing
```

How it works, per item:

1. **Identify the file**: compute the OpenSubtitles *moviehash* of the media
   file directly from its `Path` (size + first/last 64 KiB), then query
   `api.opensubtitles.com` by hash (falling back to the IMDb id). This finds
   subtitles for the exact release, not just a same-titled one.
2. **Transcribe the audio**: extract a few evenly spread ~45 s windows of the
   audio track with ffmpeg (16 kHz mono WAV) and run offline ASR (Vosk) with
   word-level timestamps.
3. **Match text passages**: normalize each subtitle cue (strip tags/punct)
   and match its word 4-grams against the recognized word stream; each cue
   whose n-grams overlap yields an anchor point (cue time ↔ audio time).
4. **Fit the linear map**: `audio_time = speed · sub_time + offset`, fitted
   by least squares over the anchors with RANSAC-style outlier rejection.
   Three good anchors are enough; more cues just improve the fit.
5. **Pick and rewrite**: among the top-N candidate subtitle files, choose
   the one with the strongest alignment (most anchors, lowest RMS), apply
   the fitted speed+offset to every cue, and upload the corrected SRT back
   to Jellyfin (`POST /Videos/{id}/Subtitles`) followed by a metadata
   refresh.

Env knobs for align: `JSUB_CANDIDATES` (default 5), `JSUB_WINDOWS` (3),
`JSUB_WINDOW_SECONDS` (45), `JSUB_MODEL_DIR` (default `~/.cache/jsub/models`,
Vosk models auto-download on first use), `FFMPEG_PATH` (otherwise PATH,
`/usr/lib/jellyfin-ffmpeg/ffmpeg`, or the imageio-ffmpeg binary).

Configuration can also come from a JSON file (`--config config.json` or
`JSUB_CONFIG`, works before or after the subcommand) — this is the
recommended way to keep secrets like passwords out of shell history:

```json
{
  "url": "http://localhost:8096",
  "api_key": "jellyfin-api-key",
  "languages": "eng,deu",
  "offset_ms": 250,
  "libraries": "optional,comma,separated,ids",
  "min_score": 0.0,
  "dry_run": false,
  "os_username": "opensubtitles-account@example.com",
  "os_password": "any chars here, no shell quoting needed"
}
```

`os_username`/`os_password` map to `OPENSUBTITLES_USERNAME` /
`OPENSUBTITLES_PASSWORD` (env still wins if both are set). **No API key is
needed**: with user:pass, jsub logs in (`POST /login`) for the download
Bearer token and uses the public app key that ships with subliminal for
searches. `os_api_key` is optional. Protect the file:
`chmod 600 ~/.config/jsub/config.json`.

Languages accept ISO 639-1 (`en`) or 639-2/3 (`eng`); matching is tolerant of
either spelling.

## Notes

- API-mode remote search requires the Open Subtitles plugin (or another
  subtitle provider plugin) installed and configured on the server; align
  mode uses that plugin's search when `OPENSUBTITLES_API_KEY` is unset and
  queries opensubtitles.com directly when it is set.
- "Fit"/"align" only re-time text formats (srt/ass/ssa/vtt); image formats
  (PGS/VobSub) are skipped.
- The Jellyfin API always returns `204` for remote subtitle downloads, even on
  failure; check the server log if a subtitle does not appear after a fetch.
- ASR quality drives alignment quality: the small Vosk models (~40 MB) work
  well for clear speech; use a larger model (`JSUB_MODEL_DIR`) for noisy
  audio. Alignment confidence is reported as `rms` (seconds) and anchor count
  in the logs — low anchors or high rms means "no-match" and nothing is
  uploaded.

## Tests

```bash
python -m pytest tests/
```

Tests run against an in-process loopback stub of the Jellyfin API; no real
server is needed.
