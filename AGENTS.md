# AGENTS.md — yt-mp3

Self-hosted, savemp3-style web tool to download best-quality audio from YouTube (and any yt-dlp-supported site).

## Quick start

```powershell
.\run.ps1        # or run.cmd — auto-creates .venv, installs from requirements.lock, starts server
# server: http://127.0.0.1:8000
```

Manual: `python -m venv .venv && .venv\Scripts\pip install --pre -r requirements.lock && .venv\Scripts\python app.py`

Runtime deps on PATH: `ffmpeg` (required for opus/m4a/mp3), a JS runtime — `node`/`deno`/`quickjs` (required for YouTube since 2025, yt-dlp runs the EJS challenge solver). Auto-detected by `detect_js_runtimes()`.

## Layout

```
app.py               FastAPI backend + yt-dlp Python API + in-process job manager
static/index.html    single-page UI (vanilla JS, SSE progress)
config.json          optional (see config.example.json): browser cookies, port, max_concurrent
requirements.txt     loose pins | requirements.lock frozen (regenerate: pip freeze > requirements.lock)
downloads/           output files
PLAN.md              design rationale (read before changing quality/format logic)
```

## API

- `GET /api/health` — yt-dlp version, ffmpeg present, detected JS runtimes
- `POST /api/preview` `{url}` → title/uploader/duration/thumbnail
- `POST /api/download` `{url, quality}` → `{id}`; qualities: `opus` (default), `best`, `m4a`, `mp3`
- `GET /api/job/{id}` — snapshot; `GET /api/job/{id}/events` — SSE stream
- `GET /api/file/{id}` — serves finished file

## Quality model (do not regress)

- YouTube best audio = Opus ~130–160k in WebM (itag 251) or AAC 128k (itag 140). No lossless source exists.
- `opus` (default): format `bestaudio[acodec^=opus]/bestaudio/best` + `FFmpegExtractAudio preferredcodec=opus` → **lossless remux** to Ogg (yt-dlp uses `-c copy` when target codec == source codec).
- `best`: no postprocessing, raw `.webm`.
- `m4a`/`mp3`: lossy re-encode, compatibility only. Never advertise "320k" as better than opus.

## yt-dlp Python-API gotchas (all hit and fixed — do not reintroduce)

1. `js_runtimes` values must be dicts: `{"node": {}}` — `None` crashes with `AttributeError: 'NoneType' object has no attribute 'get'`.
2. `YoutubeDL.download()` returns a **retcode**, not the file path. Use `extract_info(url, download=True)` and read `info["requested_downloads"][0]["filepath"]`.
3. Postprocessor order matters: `FFmpegExtractAudio` → `FFmpegMetadata` → `EmbedThumbnail`. Metadata re-mux after embed **drops the cover art**.
4. The Python API does **not** auto-enable thumbnail writing (CLI's `--embed-thumbnail` does). Set `"writethumbnail": True` or EmbedThumbnail logs "There are no thumbnails on disk".
5. Playlists: `extract_info` may return `{"_type": "playlist", "entries": [...]}` — unwrap first entry.
6. YouTube breaks extractors monthly: keep `--pre` (nightly) yt-dlp; `requirements.lock` pins a nightly.

## Conventions

- Python 3.10+ (yt-dlp deprecates 3.10 — bump venv when convenient), FastAPI, no DB: jobs live in an in-memory dict guarded by `jobs_lock`, downloads run in a `ThreadPoolExecutor(max_concurrent)`.
- Progress: yt-dlp `progress_hooks` mutate the `Job` dataclass; SSE endpoint polls `job.snapshot()` every 0.5s.
- Cookies: only when `config.json` sets `"browser"` (e.g. `"firefox"`) → `cookiesfrombrowser`. Default is none (localhost + own IP usually works).
- Keep everything relative to `BASE_DIR` (portability); no absolute paths.

## Verifying changes

```powershell
# health + full round-trip (preview → download → probe file):
Invoke-WebRequest http://127.0.0.1:8000/api/health
# then POST /api/download {"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw","quality":"opus"}
ffprobe -v quiet -show_streams "downloads\<file>.opus" | Select-String codec_name   # expect opus + format ogg
.\.venv\Scripts\python.exe -c "from mutagen.oggopus import OggOpus; t=OggOpus(r'downloads\<file>.opus').tags; print('METADATA_BLOCK_PICTURE' in t, t.get('title'))"
```

Restart server after edits (no reload): kill the `python.exe` whose CommandLine matches `*app.py*`, then `Start-Process .\.venv\Scripts\python.exe -ArgumentList "app.py" -WindowStyle Hidden`.

## Roadmap (from PLAN.md, not yet built)

Playlist/channel bulk + `--download-archive`, trim (`--download-sections`), search input (`ytsearch1:`), Docker packaging.

## Legal

Personal/localhost use only. The yt-dlp-based tool is fine; public hosting is the risk area.
