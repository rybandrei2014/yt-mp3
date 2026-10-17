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
- `POST /api/preview` `{url}` → `{type:"video",...}` or `{type:"playlist", entries:[{id,title,duration,thumbnail,url}]}` (flat extraction — fast)
- `POST /api/download` `{url, quality, cover, entries?, trim?}` → `{id, group}` (single) or `{group, total}` (playlist). `entries` = list of selected video URLs; creates one job per entry, tracked together under `group`. `cover: true` writes `<song>.png` for every job in the group. `trim: {start, end}` (seconds) cuts the finished file losslessly
- `POST /api/trim-preview` `{url, trim}` → `{id}` — downloads full track as temp mp3 at `worstaudio/worst` (low SSD/bandwidth strain), cuts range, serves at `GET /api/trim/{id}` (for the `<audio>` preview)
- `GET /api/job/{id}` — snapshot; `GET /api/job/{id}/events` — SSE stream
- `GET /api/group/{id}/events` — SSE aggregate: `{total, done, failed, progress, finished, items:[snapshots]}`; blocks until the whole group finishes
- `GET /api/file/{id}` — serves finished file; `GET /api/file/{id}/cover` — serves extracted cover

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
7. `EmbedThumbnail` rejects `.webm` ("Supported filetypes...") — `best` quality must run with **no postprocessors**. Cover for `best` is produced by `extract_cover()` fallback (downloads a thumbnail URL).
8. yt-dlp's `info["thumbnails"]` URLs can 404 (e.g. `maxresdefault`) — try in reverse order until one downloads.
9. Cover extraction (`extract_cover`): embedded art via `ffmpeg -map 0:v:0 -frames:v 1 out.png` works for opus/m4a/mp3; webm falls back to thumbnail download + convert.
10. Playlist preview uses `"extract_flat": "in_playlist"` (fast, no per-video extraction). Flat entries carry `url`/`id`/`title`/`duration`/`thumbnails` but are NOT downloadable as-is — download = one job per entry `url` with `noplaylist: True`, grouped via `groups[group_id] = [job_ids]`.
11. SSE endpoints stream until the job/group finishes — a plain HTTP GET blocks for the whole duration (that is by design; the browser uses EventSource).
12. yt-dlp `--download-sections`/`download_ranges` produces EMPTY files on YouTube (DASH) — do not use it. Trim = full download + `ffmpeg -ss S -to E -c copy` (frame-accurate for audio, lossless for opus). Temp file for the cut must keep a real extension (`.name.cut.opus`, not `.name.opus.cut`) or ffmpeg cannot choose an output format.

## Conventions

- Python 3.10+ (yt-dlp deprecates 3.10 — bump venv when convenient), FastAPI, no DB: jobs live in an in-memory dict guarded by `jobs_lock`, downloads run in a `ThreadPoolExecutor(max_concurrent)`.
- Progress: yt-dlp `progress_hooks` mutate the `Job` dataclass; SSE endpoint polls `job.snapshot()` every 0.5s.
- Cookies: only when `config.json` sets `"browser"` (e.g. `"firefox"`) → `cookiesfrombrowser`. Default is none (localhost + own IP usually works).
- Temp hygiene: trim previews live in `downloads/.previews/` (hidden dir); `sweep_temp_files()` runs at startup and before each preview, deleting all previews + stray `*.part`/`*.ytdl`/`*.cut.*` in `downloads/`. Only the newest preview ever exists.
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

`--download-archive` (skip already-downloaded), trim (`--download-sections`), search input (`ytsearch1:`), Docker packaging. (Playlist bulk is done.)

## Legal

Personal/localhost use only. The yt-dlp-based tool is fine; public hosting is the risk area.
