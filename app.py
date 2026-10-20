import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import unicodedata
import urllib.request
import uuid
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE_DIR = Path(sys.executable).resolve().parent
    APP_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = APP_DIR = Path(__file__).resolve().parent

BIN_DIR = BASE_DIR / "bin"
if BIN_DIR.is_dir():
    os.environ["PATH"] = os.pathsep.join([str(BIN_DIR), os.environ.get("PATH", "")])

DEFAULT_CONFIG = {
    "browser": None,
    "max_concurrent": 2,
    "port": 8000,
    "download_dir": "downloads",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg_file = BASE_DIR / "config.json"
    if cfg_file.exists():
        cfg.update(json.loads(cfg_file.read_text(encoding="utf-8")))
    return cfg


CONFIG = load_config()

DOWNLOADS_DIR = Path(CONFIG["download_dir"])
if not DOWNLOADS_DIR.is_absolute():
    DOWNLOADS_DIR = BASE_DIR / DOWNLOADS_DIR
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR = DOWNLOADS_DIR / ".previews"
COVER_DIR = DOWNLOADS_DIR / ".covers"


def sweep_temp_files():
    PREVIEW_DIR.mkdir(exist_ok=True)
    for f in list(PREVIEW_DIR.iterdir()) + list(DOWNLOADS_DIR.glob(".preview_*")):
        if f.is_file():
            f.unlink(missing_ok=True)
    for pattern in ("*.part", "*.ytdl", "*.cut.*"):
        for f in DOWNLOADS_DIR.glob(pattern):
            f.unlink(missing_ok=True)


sweep_temp_files()

QUALITIES = {
    "opus": {"label": "Opus (.opus, lossless remux - recommended)", "ext": "opus"},
    "best": {"label": "Original stream (.webm, zero processing)", "ext": "webm"},
    "m4a": {"label": "M4A (AAC, compatible)", "ext": "m4a"},
    "mp3": {"label": "MP3 (VBR highest, compatible)", "ext": "mp3"},
}

JS_RUNTIMES = (("deno", "deno"), ("node", "node"), ("quickjs", "qjs"), ("bun", "bun"))


def detect_js_runtimes() -> dict:
    found = {}
    for name, exe in JS_RUNTIMES:
        if shutil.which(exe):
            found[name] = {}
    return found


@dataclass
class Job:
    id: str
    url: str
    quality: str
    cover: bool = False
    status: str = "queued"
    progress: float = 0.0
    title: str = ""
    filepath: str | None = None
    cover_path: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=lambda: __import__("time").time())

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 1),
            "title": self.title,
            "error": self.error,
            "filename": Path(self.filepath).name if self.filepath else None,
            "cover_filename": Path(self.cover_path).name if self.cover_path else None,
        }


jobs: dict[str, Job] = {}
groups: dict[str, list[str]] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=int(CONFIG["max_concurrent"]))

app = FastAPI(title="yt-mp3")


class PreviewRequest(BaseModel):
    url: str


class TrimRange(BaseModel):
    start: float
    end: float


class DownloadRequest(BaseModel):
    url: str
    quality: str = "opus"
    cover: bool = False
    entries: list[str] | None = None


class TrimPreviewRequest(BaseModel):
    url: str | None = None
    file_id: str | None = None
    trim: TrimRange | None = None
    mp3: bool = False
    clip: float | None = None


class TrimLocalRequest(BaseModel):
    file_id: str
    trim: TrimRange


def base_ydl_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "js_runtimes": detect_js_runtimes(),
    }
    if CONFIG.get("browser"):
        opts["cookiesfrombrowser"] = (CONFIG["browser"],)
    return opts


def build_download_opts(job: Job) -> dict:
    opts = base_ydl_opts()
    opts.update(
        {
            "format": "bestaudio[acodec^=opus]/bestaudio/best",
            "paths": {"home": str(DOWNLOADS_DIR)},
            "outtmpl": "%(uploader)s - %(title)s.%(ext)s",
            "progress_hooks": [make_progress_hook(job)],
        }
    )
    if job.quality == "best":
        opts["postprocessors"] = []
        return opts
    opts["writethumbnail"] = True
    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": job.quality,
            "preferredquality": "0",
        },
        {"key": "FFmpegMetadata"},
        {"key": "EmbedThumbnail"},
    ]
    opts["postprocessors"] = postprocessors
    return opts


def make_progress_hook(job: Job):
    def hook(d: dict):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job.progress = d.get("downloaded_bytes", 0) / total * 100
            job.status = "downloading"
        elif d["status"] == "finished":
            job.progress = 100.0
            job.status = "processing"

    return hook


def extract_cover(job: Job, info: dict) -> str | None:
    path = Path(job.filepath)
    cover = path.with_suffix(".png")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    r = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
         "-map", "0:v:0", "-frames:v", "1", str(cover)],
        capture_output=True,
    )
    if r.returncode == 0 and cover.exists():
        return str(cover)
    cover.unlink(missing_ok=True)
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    tmp = DOWNLOADS_DIR / f".cover_{job.id}.img"
    for thumb in reversed(thumbs):
        try:
            urllib.request.urlretrieve(thumb["url"], tmp)
        except OSError:
            continue
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(tmp), str(cover)],
                capture_output=True,
            )
            if r.returncode == 0 and cover.exists():
                return str(cover)
        finally:
            tmp.unlink(missing_ok=True)
    return None


trim_previews: dict[str, str] = {}


AUDIO_EXTS = {".opus", ".webm", ".m4a", ".mp3", ".ogg"}
_lib_cache: dict[str, tuple[float, float]] = {}


def file_id(path: Path) -> str:
    return hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:12]


def resolve_file(fid: str) -> Path | None:
    for f in DOWNLOADS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS and file_id(f) == fid:
            return f
    return None


def probe_duration(path: Path) -> float | None:
    stat = path.stat()
    key = str(path)
    cached = _lib_cache.get(key)
    if cached and cached[0] == stat.st_mtime:
        return cached[1]
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    dur = float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    if dur is not None:
        _lib_cache[key] = (stat.st_mtime, dur)
    return dur


def search_key(name: str) -> str:
    return unicodedata.normalize("NFKD", name).casefold()


@app.get("/api/library")
def library(after: str | None = None, limit: int = 200, q: str | None = None):
    limit = max(1, min(limit, 500))
    files = sorted(
        (f for f in DOWNLOADS_DIR.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
        key=lambda p: p.name.lower(),
    )
    if q:
        needle = search_key(q).strip()
        files = [f for f in files if needle in search_key(f.name)]
    start = bisect_right([f.name.lower() for f in files], after.lower()) if after else 0
    page = files[start : start + limit]
    items = [
        {
            "id": file_id(f),
            "filename": f.name,
            "duration": probe_duration(f),
            "size": f.stat().st_size,
            "cover": f.with_suffix(".png").exists(),
        }
        for f in page
    ]
    nxt = page[-1].name if page and start + len(page) < len(files) else None
    return {"items": items, "total": len(files), "next": nxt}


@app.delete("/api/library/{fid}")
def delete_file(fid: str):
    f = resolve_file(fid)
    if not f:
        raise HTTPException(404, "file not found")
    f.unlink()
    cover = f.with_suffix(".png")
    if cover.exists():
        cover.unlink()
    _lib_cache.pop(str(f), None)
    for old in COVER_DIR.glob(f"{fid}.*"):
        old.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/library/{fid}/cover")
def library_cover(fid: str):
    f = resolve_file(fid)
    if not f:
        raise HTTPException(404, "file not found")
    sidecar = f.with_suffix(".png")
    src = sidecar if sidecar.exists() else f
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(404, "no cover")
    COVER_DIR.mkdir(exist_ok=True)
    out = COVER_DIR / f"{fid}.{int(src.stat().st_mtime)}.jpg"
    if not out.exists():
        for old in COVER_DIR.glob(f"{fid}.*"):
            old.unlink(missing_ok=True)
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
             "-map", "0:v:0", "-frames:v", "1",
             "-vf", "scale=128:128:force_original_aspect_ratio=decrease", str(out)],
            capture_output=True,
        )
        if r.returncode != 0 or not out.exists():
            out.unlink(missing_ok=True)
            raise HTTPException(404, "no embedded cover")
    return FileResponse(out)


def make_preview(source: Path, pid: str) -> str:
    out = PREVIEW_DIR / f".preview_{pid}.mp3"
    if out.exists():
        return str(out)
    r = subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(source),
         "-b:a", "64k", str(out)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not out.exists():
        raise HTTPException(status_code=422, detail=f"preview failed: {r.stderr.strip()[:200]}")
    return str(out)


def make_full_url_preview(url: str) -> dict:
    flat_opts = base_ydl_opts()
    flat_opts["extract_flat"] = "in_playlist"
    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    vid = info.get("id")
    if not vid:
        raise HTTPException(status_code=422, detail="could not resolve video id")
    full = PREVIEW_DIR / f".preview_{vid}.mp3"
    if not full.exists():
        sweep_temp_files()
        opts = base_ydl_opts()
        opts.update(
            {
                "format": "worstaudio/worst",
                "paths": {"home": str(PREVIEW_DIR)},
                "outtmpl": f".preview_{vid}.%(ext)s",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": 64}
                ],
            }
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not full.exists():
            raise HTTPException(status_code=422, detail="could not download preview source")
    trim_previews[vid] = str(full)
    return {"id": vid, "duration": info.get("duration")}


def make_url_clip(url: str, secs: float) -> dict:
    """First `secs` of a remote track without pulling the whole file.

    yt-dlp --download-sections yields EMPTY files on YouTube DASH, and stream URLs
    resolved inside a cookie'd session cannot be fetched outside it (403, even with
    the same jar — googlevideo.com is not under the cookie domain). So: extract
    WITHOUT browser cookies, Range-fetch the head of the cheapest audio stream,
    then `ffmpeg -t N -c copy` that local head. Falls back to the full worstaudio
    preview for videos that do need cookies (age/private).
    """
    secs = max(1.0, min(secs, 120.0))
    try:
        opts = base_ydl_opts()
        opts.pop("cookiesfrombrowser", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        vid = info.get("id")
        if not vid:
            raise LookupError("could not resolve video id")
        fmts = [f for f in info.get("formats") or [] if f.get("acodec") not in (None, "none") and f.get("url")]
        fmts.sort(key=lambda f: f.get("tbr") or 0)
        pick = next((f for f in fmts if f.get("ext") == "m4a"), None) or (fmts[0] if fmts else None)
        if not pick:
            raise LookupError("no audio stream found")
        ext = pick.get("ext") or "m4a"
        pid = f"{vid}_{int(secs)}s"
        out = PREVIEW_DIR / f".preview_{pid}.{ext}"
        if out.exists():
            trim_previews[pid] = str(out)
            return {"id": pid, "duration": secs}
        sweep_temp_files()
        budget = int((pick.get("tbr") or 128) * 1024 / 8 * secs * 1.3) + 96 * 1024
        headers = {k: v for k, v in (info.get("http_headers") or {}).items() if k.lower() != "accept-encoding"}
        headers["Range"] = f"bytes=0-{budget - 1}"
        req = urllib.request.Request(pick["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            chunk = r.read(budget)
        if not chunk:
            raise LookupError("empty stream response")
        head = PREVIEW_DIR / f".preview_{pid}_head.{ext}"
        head.write_bytes(chunk)
        r = subprocess.run(
            [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(head),
             "-t", str(secs), "-c", "copy", str(out)],
            capture_output=True, text=True,
        )
        head.unlink(missing_ok=True)
        if not out.exists():
            raise LookupError(f"clip failed: {r.stderr.strip()[:200]}")
        trim_previews[pid] = str(out)
        duration = info.get("duration")
        return {"id": pid, "duration": min(secs, duration) if duration else secs}
    except (yt_dlp.utils.DownloadError, LookupError, OSError) as e:
        if not CONFIG.get("browser"):
            raise HTTPException(status_code=422, detail=f"preview failed: {e}")
        return make_full_url_preview(url)


@app.post("/api/trim-preview")
def trim_preview(req: TrimPreviewRequest):
    if req.trim and (req.trim.start < 0 or req.trim.end <= req.trim.start):
        raise HTTPException(status_code=422, detail="invalid trim range")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=500, detail="ffmpeg not found on PATH")
    if req.file_id:
        src = resolve_file(req.file_id)
        if not src:
            raise HTTPException(status_code=404, detail="file not found")
        pid = req.file_id
        if not req.mp3:
            trim_previews["raw_" + pid] = str(src)
            return {"id": "raw_" + pid, "duration": probe_duration(src)}
        path = make_preview(src, pid)
        if req.trim:
            cut = PREVIEW_DIR / f".preview_{pid}_cut.mp3"
            r = subprocess.run(
                [shutil.which("ffmpeg"), "-y", "-loglevel", "error",
                 "-ss", str(req.trim.start), "-to", str(req.trim.end),
                 "-i", path, "-c", "copy", str(cut)],
                capture_output=True, text=True,
            )
            if r.returncode != 0 or not cut.exists():
                raise HTTPException(status_code=422, detail=f"trim failed: {r.stderr.strip()[:200]}")
            trim_previews["cut_" + pid] = str(cut)
            return {"id": "cut_" + pid, "duration": req.trim.end - req.trim.start}
        trim_previews[pid] = path
        return {"id": pid, "duration": probe_duration(src)}
    if not req.url:
        raise HTTPException(status_code=422, detail="url or file_id required")
    if req.clip:
        return make_url_clip(req.url, req.clip)
    return make_full_url_preview(req.url)


def fmt_compact(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


@app.post("/api/trim-local")
def trim_local(req: TrimLocalRequest):
    if req.trim.start < 0 or req.trim.end <= req.trim.start:
        raise HTTPException(status_code=422, detail="invalid trim range")
    src = resolve_file(req.file_id)
    if not src:
        raise HTTPException(status_code=404, detail="file not found")
    out = src.with_name(f"{src.stem}_{fmt_compact(req.trim.start)}_{fmt_compact(req.trim.end)}{src.suffix}")
    r = subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-loglevel", "error",
         "-ss", str(req.trim.start), "-to", str(req.trim.end),
         "-i", str(src), "-c", "copy", str(out)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not out.exists():
        raise HTTPException(status_code=422, detail=f"trim failed: {r.stderr.strip()[:200]}")
    return {"id": file_id(out), "filename": out.name}


@app.get("/api/trim/{preview_id}")
def get_trim(preview_id: str):
    path = trim_previews.get(preview_id)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="preview not found")
    media = {".opus": "audio/ogg", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
             ".m4a": "audio/mp4", ".webm": "audio/webm"}.get(Path(path).suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


def run_job(job: Job):
    try:
        with yt_dlp.YoutubeDL(build_download_opts(job)) as ydl:
            info = ydl.extract_info(job.url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        job.title = info.get("title") or ""
        requested = info.get("requested_downloads") or []
        job.filepath = requested[0].get("filepath") if requested else None
        if job.filepath and not Path(job.filepath).exists():
            job.filepath = None
        job.status = "done" if job.filepath else "error"
        if not job.filepath:
            job.error = "download finished but output file not found"
        elif job.cover:
            job.cover_path = extract_cover(job, info)
    except yt_dlp.utils.DownloadError as e:
        job.error = str(e)
        job.status = "error"
    except Exception as e:
        job.error = f"{type(e).__name__}: {e}"
        job.status = "error"


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "yt_dlp": yt_dlp.version.__version__,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "js_runtimes": list(detect_js_runtimes()),
    }


@app.post("/api/preview")
def preview(req: PreviewRequest):
    opts = base_ydl_opts()
    opts["extract_flat"] = "in_playlist"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if info.get("_type") == "playlist":
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            thumbs = [t for t in e.get("thumbnails") or [] if t.get("url")]
            entries.append(
                {
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "duration": e.get("duration"),
                    "thumbnail": thumbs[-1]["url"] if thumbs else None,
                    "url": e.get("url") or e.get("webpage_url"),
                }
            )
        return {
            "type": "playlist",
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "count": len(entries),
            "entries": entries,
        }
    return {
        "type": "video",
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
    }


@app.post("/api/download")
def start_download(req: DownloadRequest):
    if req.quality not in QUALITIES:
        raise HTTPException(status_code=422, detail=f"unknown quality: {req.quality}")
    if not shutil.which("ffmpeg") and req.quality in ("opus", "m4a", "mp3"):
        raise HTTPException(status_code=500, detail="ffmpeg not found on PATH")
    urls = req.entries if req.entries else [req.url]
    if not urls:
        raise HTTPException(status_code=422, detail="no entries selected")
    group_id = uuid.uuid4().hex[:12]
    job_ids = []
    with jobs_lock:
        for url in urls:
            job = Job(id=uuid.uuid4().hex[:12], url=url, quality=req.quality, cover=req.cover)
            jobs[job.id] = job
            job_ids.append(job.id)
        groups[group_id] = job_ids
    for job_id in job_ids:
        executor.submit(run_job, jobs[job_id])
    if len(urls) == 1:
        return {"id": job_ids[0], "group": group_id}
    return {"group": group_id, "total": len(job_ids)}


GROUP_DONE_LIMIT = 50
GROUP_ISSUE_LIMIT = 100


def group_summary(group_id: str) -> dict:
    """Aggregates + a bounded sample of items — a 10k-video group must not serialize
    10k snapshots twice a second."""
    ids = groups[group_id]
    total = len(ids)
    finished = failed = 0
    overall = 0.0
    items: list[dict] = []
    issues: list[dict] = []
    for jid in ids:
        job = jobs[jid]
        overall += job.progress
        if job.status == "done":
            finished += 1
            if len(items) < GROUP_DONE_LIMIT:
                items.append(job.snapshot())
        elif job.status == "error":
            finished += 1
            failed += 1
            if len(issues) < GROUP_ISSUE_LIMIT:
                issues.append(job.snapshot())
    return {
        "total": total,
        "done": finished,
        "failed": failed,
        "progress": round(overall / total, 1) if total else 0,
        "finished": finished == total,
        "items": items,
        "issues": issues,
        "truncated": finished > GROUP_DONE_LIMIT or failed > GROUP_ISSUE_LIMIT,
    }


@app.get("/api/group/{group_id}/events")
def group_events(group_id: str):
    if group_id not in groups:
        raise HTTPException(status_code=404, detail="group not found")

    async def stream():
        while True:
            snap = group_summary(group_id)
            yield f"data: {json.dumps(snap)}\n\n"
            if snap["finished"]:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job.snapshot()


@app.get("/api/job/{job_id}/events")
async def job_events(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    async def stream():
        while True:
            yield f"data: {json.dumps(job.snapshot())}\n\n"
            if job.status in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job or job.status != "done" or not job.filepath:
        raise HTTPException(status_code=404, detail="file not ready")
    path = Path(job.filepath)
    if not path.exists():
        raise HTTPException(status_code=410, detail="file expired")
    return FileResponse(path, filename=path.name)


@app.get("/api/file/{job_id}/cover")
def get_cover(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.cover_path:
        raise HTTPException(status_code=404, detail="cover not available")
    path = Path(job.cover_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="file expired")
    return FileResponse(path, filename=path.name)


@app.get("/api/file-by-name/{name}")
def get_file_by_name(name: str):
    path = (DOWNLOADS_DIR / name).resolve()
    if not str(path).startswith(str(DOWNLOADS_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=path.name)


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    if not detect_js_runtimes():
        print("WARNING: no JS runtime (deno/node/quickjs) found on PATH - YouTube may fail")
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found on PATH - m4a/mp3 conversion will fail")
    port = int(CONFIG["port"])
    if FROZEN:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
    uvicorn.run(app, host="127.0.0.1", port=port)
