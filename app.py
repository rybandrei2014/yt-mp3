import asyncio
import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "browser": None,
    "max_concurrent": 2,
    "port": 8000,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg_file = BASE_DIR / "config.json"
    if cfg_file.exists():
        cfg.update(json.loads(cfg_file.read_text(encoding="utf-8")))
    return cfg


CONFIG = load_config()

QUALITIES = {
    "best": {"label": "Best (original Opus, no re-encode)", "ext": "webm"},
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
    status: str = "queued"
    progress: float = 0.0
    title: str = ""
    filepath: str | None = None
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
        }


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=int(CONFIG["max_concurrent"]))

app = FastAPI(title="yt-mp3")


class PreviewRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"


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
    postprocessors = []
    if job.quality in ("m4a", "mp3"):
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": job.quality,
                "preferredquality": "0",
            }
        )
    postprocessors.append({"key": "EmbedThumbnail"})
    postprocessors.append({"key": "FFmpegMetadata"})
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
    try:
        with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    return {
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
    if not shutil.which("ffmpeg") and req.quality in ("m4a", "mp3"):
        raise HTTPException(status_code=500, detail="ffmpeg not found on PATH")
    job = Job(id=uuid.uuid4().hex[:12], url=req.url, quality=req.quality)
    with jobs_lock:
        jobs[job.id] = job
    executor.submit(run_job, job)
    return {"id": job.id}


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


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    if not detect_js_runtimes():
        print("WARNING: no JS runtime (deno/node/quickjs) found on PATH - YouTube may fail")
    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found on PATH - m4a/mp3 conversion will fail")
    uvicorn.run(app, host="127.0.0.1", port=int(CONFIG["port"]))
