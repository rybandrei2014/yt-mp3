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
config.json          optional (see config.example.json): browser cookies, port, max_concurrent, download_dir — GITIGNORED (personal), never commit
icon.ico             Windows exe icon (generated from static/icon.png; regenerate: .venv\Scripts\python.exe tools\make_icons.py — needs Pillow)
tools/make_icons.py  regenerates icon.ico; macOS icon.icns is built in CI (sips+iconutil from static/icon.png); static/icon.png also serves as browser favicon
requirements.txt     loose pins | requirements.lock frozen (regenerate: pip freeze > requirements.lock)
requirements-build.txt  release build deps (pyinstaller)
yt-mp3.spec          PyInstaller spec (onedir; bundles static/, yt_dlp_ejs plugin + metadata)
.github/workflows/release.yml  on tag v*: matrix win64/macos-arm64/linux64 → GitHub Release
downloads/           output files
PLAN.md              design rationale (read before changing quality/format logic)
```

## Releases

`git tag vX.Y.Z && git push --tags` → CI (`.github/workflows/release.yml`) per OS: downloads ffmpeg+ffprobe (BtbN builds for win/linux; ffmpeg.martin-riedl.de for macos-arm64) and quickjs-ng `qjs` binary (pinned `QJS_VERSION`) into `bin/`, runs `pyinstaller yt-mp3.spec`, copies `bin/` into `dist/yt-mp3/`, smoke-tests the frozen exe (`/api/health` must report bundled ffmpeg + quickjs), zips to GitHub Release.

Frozen-app gotchas (app.py top): `BASE_DIR` = exe dir (writable: config.json, downloads), `APP_DIR` = `sys._MEIPASS` (bundled static/), `BASE_DIR/bin` is prepended to `PATH` so `shutil.which`/`detect_js_runtimes` find the bundled binaries; browser auto-opens only when frozen. Local build: `.\.venv\Scripts\pyinstaller.exe --noconfirm yt-mp3.spec` (output in `dist/`, gitignored).

## API

- `GET /api/health` — yt-dlp version, ffmpeg present, detected JS runtimes
- `POST /api/preview` `{url}` → `{type:"video",...}` or `{type:"playlist", entries:[{id,title,duration,thumbnail,url}]}` (flat extraction — fast)
- `POST /api/download` `{url, quality, cover, entries?}` → `{id, group}` (single) or `{group, total}` (playlist). `entries` = list of selected video URLs; creates one job per entry, tracked together under `group`. `cover: true` writes `<song>.png` for every job in the group. Downloads are ALWAYS full tracks — trimming is a separate post-download step. The playlist modal list is **virtualized** (`static/index.html`: `PL_ROW = 50` px window + `#plPadTop`/`#plPadBottom` spacers → ~24 rows in the DOM regardless of playlist size; selection lives in `pl.sel` (a `Set` of indices, `[Deleted video]`/`[Private video]` start unchecked), thumbnails are `loading="lazy"`, re-render is rAF-throttled on scroll)
- `GET /api/library?after=<filename>&limit=&q=` — cursor-paged downloaded audio files in `download_dir` (`{items:[{id,filename,duration,size,cover}], total, next}`, cursor = last filename of previous page, sorted by `name.lower()`, id = sha1(name)[:12], duration cached by mtime, ffprobe runs only for the page — limit capped at 500). `q` = fulltext substring filter on filename (case-insensitive, diacritic-insensitive via NFKD casefold); `total`/`next`/cursor all operate on the filtered set. UI search box `#libSearch` (250ms debounce, `#libSearchClear` ✕, Esc clears, inline SVG magnifier as CSS background) is hidden when library is empty. Search reload goes through `reloadLibrary()`: it pins scroll to the library anchor before clearing (collapsing the list mid-scroll otherwise jumps the viewport) and pages are inserted as one `DocumentFragment`; the spinner is the static `#libSpinner` element toggled by `display` — never create/remove it inside `#library` (layout churn = visible jerk + cover fade artifacts). Rows enter with the staggered `libIn` keyframe (`animation-delay` = index × 22 ms, capped at 24) and the sentinel observer uses `rootMargin: 240px` to prefetch the next page before it is reached. UI uses infinite scroll (IntersectionObserver sentinel `#libSentinel`, page = 50); leaving the top marks the list stale, returning to the top refetches from page 1 immediately (never clear mid-scroll — the page collapses and the browser jumps to top, re-triggering the reload)
- `DELETE /api/library/{id}` — deletes the file (and its sidecar `.png` cover if present); UI shows a confirm modal first
- `GET /api/library/{id}/cover` — 128px jpg thumbnail for the row: source = sidecar `.png` if present, else embedded art extracted with `ffmpeg -map 0:v:0 -frames:v 1`; cached in `download_dir/.covers/{id}.{mtime}.jpg` (cleaned on delete). 404 when no art (trim `-c copy` cuts often drop it) → UI hides the img via `onerror`
- `POST /api/trim-preview` `{url?, file_id?, trim?, mp3?, clip?}` → `{id}` — waveform/listen source served at `GET /api/trim/{id}`. `file_id` = local library file: serves the ORIGINAL file directly (full-quality waveform; `mp3: true` forces a 64k mp3 conversion fallback for browsers that cannot decode opus). `url` = YouTube worstaudio 64k mp3 download (only for pre-download preview). `url` + `clip=<secs>` = cheap listen preview: extracts WITHOUT browser cookies, Range-fetches only the head of the cheapest audio stream, then `ffmpeg -t <secs> -c copy` → ~90 KB for 15 s (UI uses `clip: 15`) instead of the whole track (falls back to the full worstaudio download when the video needs cookies, e.g. age/private). Cached per id
- `POST /api/trim-local` `{file_id, trim:{start,end}}` → cuts a COPY named `<stem>_<XmYYs>_<XmYYs>.<ext>` via `ffmpeg -c copy` (original kept), returns new `{id, filename}`
- `GET /api/file/{id}` / `GET /api/file/{id}/cover` — job outputs; `GET /api/file-by-name/{name}` — library files by filename (path-traversal guarded)
- `GET /api/job/{id}` — snapshot; `GET /api/job/{id}/events` — SSE stream
- `GET /api/group/{id}/events` — SSE aggregate: `{total, done, failed, progress, finished, items:[≤50 done snapshots], issues:[≤100 error snapshots], truncated}`; blocks until the whole group finishes. Snapshots are capped (`group_summary`) — serializing 10k items twice a second would saturate the socket and freeze the tab

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
13. `FFmpegExtractAudio.preferredquality` must be a NUMBER: `0`–`9` = VBR scale, `>10` = `-b:a {q}k` (e.g. `64` → 64 kbps). Strings like `"64K"` are silently ignored (`float_or_none` → None).
14. Stream URLs resolved inside a **cookie'd** session (`cookiesfrombrowser`) return **403** when fetched outside it — handing them to `ffmpeg -i <url>` or `urllib` fails even with the same jar (googlevideo.com is outside the `.youtube.com` cookie domain). For `clip` previews we extract with `cookiesfrombrowser` popped, then Range-fetch the head of the cheapest audio stream and `ffmpeg -t N -c copy` the local head (`make_url_clip`); if that extraction fails, fall back to the full worstaudio download (which uses cookies and yt-dlp's own downloader).

## Conventions

- Python 3.10+ (yt-dlp deprecates 3.10 — bump venv when convenient), FastAPI, no DB: jobs live in an in-memory dict guarded by `jobs_lock`, downloads run in a `ThreadPoolExecutor(max_concurrent)`.
- Progress: yt-dlp `progress_hooks` mutate the `Job` dataclass; SSE endpoint polls `job.snapshot()` every 0.5s.
- Cookies: only when `config.json` sets `"browser"` (e.g. `"firefox"`) → `cookiesfrombrowser`. Default is none (localhost + own IP usually works).
- Temp hygiene: trim previews live in `downloads/.previews/` (hidden dir); `sweep_temp_files()` runs at startup and before each preview, deleting all previews + stray `*.part`/`*.ytdl`/`*.cut.*` in `downloads/`. Only the newest preview ever exists.
- Keep everything relative to `BASE_DIR` (portability); no absolute paths — EXCEPT `download_dir` in `config.json`, which may be absolute (e.g. `"G:\\yt-mp3"`) to keep downloads/previews off the app's drive. Relative values resolve against `BASE_DIR`.

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
