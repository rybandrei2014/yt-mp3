# yt-mp3 — Plan

Tool to download music in the best possible quality from any YouTube video (savemp3.net-style), self-hosted.

## 1. Reality check: what "best quality" means

- YouTube's best audio stream is **Opus ~130–160 kbps (itag 251, WebM)** or **AAC 128 kbps (itag 140, M4A)**. No lossless / 320 kbps source exists.
- savemp3's "320 kbps MP3" is marketing: it re-encodes the 128–160k source into a bigger MP3. Zero quality gain, only loss.
- **Decision:** default = grab `bestaudio` and **losslessly remux Opus to `.opus` (Ogg)** — same codec, better player support than `.webm`. Raw `.webm` kept as zero-processing option; MP3/M4A offered only as compatibility options.

## 2. Core engine: yt-dlp (do not reinvent)

yt-dlp already solves the hard parts: YouTube JS challenges (EJS), PO tokens, throttling, cookies, playlists, metadata, 1000+ other sites.

Required dependencies:

| Dependency | Purpose |
|---|---|
| `yt-dlp[default]` (pip) | extraction + download engine |
| `ffmpeg` / `ffprobe` | audio extraction, muxing, post-processing |
| `deno` (or node 22+) | runs YouTube's JS challenge solver (`yt-dlp-ejs`) — required since 2025 |
| `mutagen` | thumbnail/metadata embedding (bundled in `yt-dlp[default]`) |

Reference commands the tool must replicate:

```bash
# Best quality, no re-encode (default)
yt-dlp -f "bestaudio[acodec^=opus]/bestaudio/best" -x --audio-format m4a \
       --embed-thumbnail --add-metadata "URL"

# MP3 (VBR highest) — compatibility only
yt-dlp -x --audio-format mp3 --audio-quality 0 "URL"
```

## 3. Architecture

```
Frontend (URL input, quality picker, progress bar)
   │  REST + SSE/WebSocket
FastAPI backend
   ├── POST /api/preview   → extract_info() → title, thumbnail, duration, formats
   ├── POST /api/download  → create job, start download in thread pool
   ├── GET  /api/job/{id}  → status + percent (SSE stream via progress_hooks)
   ├── GET  /api/file/{id} → serve finished file
   └── downloads/           → temp files, cleaned up after expiry
```

- Backend: **Python FastAPI**, yt-dlp imported as a **Python API** (`yt_dlp.YoutubeDL`), not subprocess.
- `progress_hooks` → per-job event queue → SSE stream to the UI progress bar.
- No Redis/Celery in MVP: `ThreadPoolExecutor` with a small worker cap is enough for personal use.

## 4. Anti-bot strategy (the real challenge)

YouTube blocks datacenter IPs and unknown clients (403/429, PO token walls).

1. **Personal / localhost use (MVP):** `--cookies-from-browser chrome` + own residential IP. Works, zero cost.
2. **Small self-hosted:** rotate residential proxies (`--proxy`) + cookie jar per IP.
3. **Public savemp3-scale service:** proxy fleets + constantly updated solvers — out of scope; that is their actual moat.

Operational rule: track yt-dlp **nightly** releases; YouTube breaks extractors monthly.

## 5. MVP scope (this repo)

1. `pip install "yt-dlp[default]"` + install ffmpeg + deno on PATH.
2. FastAPI app + single static HTML page:
   - paste URL → preview title/thumbnail/duration
   - quality picker: **Original (best, opus)** / **M4A** / **MP3 320**
   - progress bar (SSE) → download button when done
3. Options passed to YoutubeDL:
   - `format`: `bestaudio[acodec^=opus]/bestaudio/best`
   - `postprocessors`: FFmpegExtractAudio (only when m4a/mp3 chosen), EmbedThumbnail, EmbedMetadata / XAttrMetadata
   - `cookiesfrombrowser`: `("chrome",)` (configurable)
   - `outtmpl`: `downloads/%(uploader)s - %(title)s.%(ext)s`
4. Run on `localhost` — sidesteps anti-bot entirely (own IP + own browser cookies).

## 6. Stretch goals

- Playlist / channel bulk download with `--download-archive` (skip already-downloaded).
- Trim/cut like savemp3 (`--download-sections "*0:30-3:45"`, re-encode only when cutting).
- Search-by-name input (`ytsearch1:`).
- Config file for default quality, output dir, browser for cookies.
- Docker image bundling yt-dlp + ffmpeg + deno.

## 7. Legal note

Keep it personal-use / localhost. Publicly hosted converters are the category that gets sued and blocked; the tool itself (yt-dlp-based) is fine, distribution/hosting is the risk area.
