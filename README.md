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

```bash
pip install -e .                 # API mode, stdlib only
pip install -e '.[align]'        # + vosk + bundled ffmpeg for align mode
```

Create an API key in Jellyfin: Dashboard → Administration → API Keys.
For align mode you also need an OpenSubtitles API key
(`OPENSUBTITLES_API_KEY`, from https://www.opensubtitles.com/users/importlevel).

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

### align mode (on the server)

```bash
jsub align --languages eng                  # verify + align all items
jsub align --languages deu --dry-run        # preview, upload nothing
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
`JSUB_CONFIG`):

```json
{
  "url": "http://localhost:8096",
  "api_key": "...",
  "languages": "eng,deu",
  "offset_ms": 250,
  "libraries": "optional,comma,separated,ids",
  "min_score": 0.0,
  "dry_run": false
}
```

Languages accept ISO 639-1 (`en`) or 639-2/3 (`eng`); matching is tolerant of
either spelling.

## Notes

- API-mode remote search requires the Open Subtitles plugin (or another
  subtitle provider plugin) installed and configured on the server; align
  mode talks to opensubtitles.com directly and needs no Jellyfin plugin.
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
