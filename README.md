# yt-mp3

Self-hosted, savemp3-style web tool to download **best-quality audio** from YouTube (and any [yt-dlp-supported site](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)), with a built-in library and a waveform trim editor.

- No re-encode by default: best YouTube audio (Opus ~130–160k) is **losslessly remuxed** to `.opus`
- Playlist support: preview, pick videos, bulk download
- Trim modal: classic waveform timeline (drag selection, scrub/preview in browser, lossless `ffmpeg -c copy` cut — original kept)
- Cover art embedding + optional separate `.png`
- No database, no build step: FastAPI + one vanilla-JS page

## Download (no install needed)

Grab the archive for your OS from the [latest release](https://github.com/rybandrei2014/yt-mp3/releases) — `yt-mp3-<version>-win64.zip`, `-macos-arm64.zip` or `-linux64.zip`. Unpack anywhere, run `yt-mp3` (`yt-mp3.exe` on Windows): the server starts and your browser opens at <http://127.0.0.1:8000>. `ffmpeg`/`ffprobe` and a JS runtime are **bundled** in the `bin/` folder — nothing else to install. Downloads land in `downloads/` next to the executable.

First launch caveats (binaries are unsigned):

- **Windows**: SmartScreen → *More info* → *Run anyway*
- **macOS**: right-click → *Open* once, or `xattr -dr com.apple.quarantine yt-mp3`

To cut a release: `git tag v0.1.0; git push --tags` — GitHub Actions builds all three archives automatically.

## Requirements (running from source)

- Python 3.10+
- `ffmpeg` on PATH (required for opus/m4a/mp3 and trimming)
- A JS runtime on PATH — `node`, `deno`, or `quickjs` (required for YouTube since 2025; yt-dlp runs its EJS challenge solver). Auto-detected.

## Quick start

```powershell
.\run.ps1        # or run.cmd — auto-creates .venv, installs from requirements.lock, starts server
# open http://127.0.0.1:8000
```

Manual:

```powershell
python -m venv .venv
.venv\Scripts\pip install --pre -r requirements.lock
.venv\Scripts\python app.py
```

YouTube breaks extractors roughly monthly — yt-dlp is pinned to a **nightly** (`--pre`). Re-run `run.ps1` after pulling, or update with:

```powershell
.venv\Scripts\pip install -U --pre yt-dlp yt-dlp-ejs
```

## Configuration

Optional `config.json` (see `config.example.json`), all keys optional:

| Key | Default | Meaning |
|---|---|---|
| `port` | `8000` | listen port on `127.0.0.1` |
| `max_concurrent` | `2` | parallel download jobs |
| `download_dir` | `downloads` | where audio lands; may be absolute (e.g. `"G:\\yt-mp3"`) |
| `browser` | none | read cookies from this browser (`"firefox"`, `"chrome"`, …) — only needed if YouTube blocks your IP |

## Quality options

| Option | What it does |
|---|---|
| **Opus** (default) | best Opus stream, lossless remux to Ogg — this is the real max quality |
| Original stream | raw `.webm`, zero processing |
| M4A / MP3 | lossy re-encode, for compatibility only |

There is no lossless source on YouTube; "320k MP3" is never better than the Opus stream.

## How it works

```
app.py               FastAPI backend + yt-dlp Python API + in-memory job manager (ThreadPoolExecutor)
static/index.html    single-page UI (vanilla JS, SSE progress, canvas waveform)
yt-mp3.spec          PyInstaller build for the release archives
.github/workflows/   release.yml: on tag v*, builds win/mac/linux archives with bundled ffmpeg + qjs
downloads/           output files (+ hidden .previews/ for trim waveforms)
```

Flow: paste URL → `POST /api/preview` (flat extraction, also lists playlists) → pick quality → `POST /api/download` → live progress via SSE → file appears in **Library** → `✂ Trim` opens the waveform modal (full-quality decode, drag the selection, `▶` previews in-browser, `✂ Cut` writes a lossless copy).

## License

MIT — see [LICENSE](LICENSE). The code is yours to use and modify; how you use the tool is your responsibility. Bundled components (yt-dlp, ffmpeg, quickjs-ng) carry their own licenses.

## Legal

Personal / localhost use only. Downloading may violate a site's ToS and copyright law in your jurisdiction — know your local rules. Public hosting of this tool is the risk area.
