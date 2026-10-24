import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
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
    "synth_tags": False,
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
LYRICS_DIR = DOWNLOADS_DIR / ".lyrics"


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
    lyrics: bool = False
    status: str = "queued"
    progress: float = 0.0
    title: str = ""
    filepath: str | None = None
    cover_path: str | None = None
    lyrics_path: str | None = None
    lrc_path: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=lambda: time.time())

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 1),
            "title": self.title,
            "error": self.error,
            "filename": Path(self.filepath).name if self.filepath else None,
            "cover_filename": Path(self.cover_path).name if self.cover_path else None,
            "lyrics_filename": Path(self.lyrics_path).name if self.lyrics_path else None,
            "lrc_filename": Path(self.lrc_path).name if self.lrc_path else None,
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
    lyrics: bool = False
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


class LyricsRefreshRequest(BaseModel):
    file_id: str


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


# ---------- lyrics: lrclib -> genius -> lyrics.ovh, verified against artist/title/duration ----------
# Genius alone mis-picks short ambiguous titles (Kino "General" -> a different artist entirely),
# lrclib alone 503s under load and misses Cyrillic long tails, so: query all three in order and
# accept only a hit whose artist AND title match (never write the wrong lyrics; no match = no file).

LYRICS_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "Accept-Language": "en,ru;q=0.8",
}
LYRICS_MIN_INTERVAL = 1.2
LYRICS_TEXT_MIN = 40
LYRICS_PAGE_TRIES = 4
LYRICS_TITLE_MIN_SIM = 0.72
LYRICS_ARTIST_MIN_SIM = 0.55
_lyrics_slot_lock = threading.Lock()
_lyrics_slot_next = 0.0

NOISE_WORDS = re.compile(
    r"(?i)official|lyric|video|audio|remaster|mastered|visuali[sz]er|karaoke|explicit|"
    r"\bhd\b|\bhq\b|\b4k\b|\bmv\b|\blive\b|concert|slowed|speed ?up|\bloop\b|"
    r"feat\.?\s|ft\.?\s|featuring|subtitl|текст|официал|видео|аудио|лирик|ремастер|"
    r"караок|клип|концерт|перевод|субтитр"
)
TITLE_SEPS = (" - ", " – ", " — ", " || ", " | ", " · ")


class GeniusLyricsParser(HTMLParser):
    """Genius song pages are React markup: lyrics live in [data-lyrics-container] blocks, the
    header/contributors UI is marked data-exclude-from-selection="true". Some songs ship
    character-reversed anchor text (anti-scrape) — data-string holds it, reversed back here."""

    SKIP_TAGS = ("svg", "script", "style", "button", "noscript")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.in_lyrics = 0
        self.lyrics_depth = 0
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.depth += 1
        if self.skip_depth:
            return
        if a.get("data-exclude-from-selection") == "true" or tag in self.SKIP_TAGS:
            self.skip_depth = self.depth
            return
        if not self.in_lyrics and a.get("data-lyrics-container") == "true":
            self.in_lyrics += 1
            self.lyrics_depth = self.depth
            return
        if not self.in_lyrics:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "a" and a.get("data-string"):
            s = a["data-string"]
            if "reversed" in (a.get("class") or ""):
                s = s[::-1]
            self.parts.append(s)
            self.skip_depth = self.depth

    def handle_endtag(self, tag):
        if self.skip_depth and self.depth == self.skip_depth:
            self.skip_depth = 0
        if self.in_lyrics and self.lyrics_depth == self.depth:
            self.in_lyrics -= 1
            self.parts.append("\n\n")
        self.depth -= 1

    def handle_data(self, data):
        if self.in_lyrics and not self.skip_depth:
            self.parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def lyrics_slot():
    """One lookup per LYRICS_MIN_INTERVAL across ALL jobs — lrclib answers 503
    ServerOverloaded above ~1 req/s, and shared API budget matters for playlist downloads."""
    global _lyrics_slot_next
    with _lyrics_slot_lock:
        if _lyrics_slot_next > time.monotonic():
            time.sleep(_lyrics_slot_next - time.monotonic())
        _lyrics_slot_next = time.monotonic() + LYRICS_MIN_INTERVAL


def http_get(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=LYRICS_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except (OSError, ValueError):
        return 0, ""


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    if (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 4:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def unwrap_noise(title: str) -> str:
    def repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        return "" if (NOISE_WORDS.search(inner) or re.fullmatch(r"\d{4}", inner)) else inner

    t = re.sub(r"[\(\[{]([^\)\]}]*)[\)\]}]", repl, title)
    t = re.sub(r"[()\[\]{}]", " ", t)
    return re.sub(r"^[\s\-–—|:,.]+|[\s\-–—|:,.]+$", "", t).strip()


def title_pairs(title: str) -> list[tuple[str, str]]:
    pairs = []
    for sep in TITLE_SEPS:
        if sep not in title:
            continue
        for idx in {title.index(sep), title.rindex(sep)}:
            a, t = title[:idx].strip(), title[idx + len(sep):].strip()
            if len(norm_text(a)) >= 3 and len(norm_text(t)) >= 2:
                pairs.append((a, t))
    return pairs


def lyrics_queries(info: dict) -> list[tuple[str, str]]:
    """artist/track straight from yt-dlp when it has them (YouTube "Topic" uploads do), else
    split the video title — NEVER the filename: uploader is a random re-upload channel there."""
    title = (info.get("title") or "").strip()
    uploader = re.sub(r"(?i)\s*-\s*Topic$", "", (info.get("uploader") or info.get("channel") or "").strip())
    artist = (info.get("artist") or info.get("creator") or "").strip()
    track = (info.get("track") or "").strip()
    raw: list[tuple[str, str]] = []
    if artist and track:
        raw.append((artist, track))
    for source in (title, unwrap_noise(title)):
        raw.extend(title_pairs(source))
    clean = unwrap_noise(title)
    if uploader and clean:
        raw.append((uploader, clean))
    out, seen = [], set()
    for a, t in raw:
        key = (norm_text(a), norm_text(t))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        out.append((a, t))
    return out[:4]


def clean_lyrics(text: str) -> str:
    lines = [l.strip() for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    kept: list[str] = []
    for l in lines:
        if not l and (not kept or not kept[-1]):
            continue
        kept.append(l)
    return "\n".join(kept).strip()


# Structure markers. lrclib/lyrics.ovh plain text usually has NONE of them; Genius has them but in
# the page language ("Куплет 1", "후렴", "Estribillo"). Everything recognizable is rewritten to
# English; anything unknown is left untouched — a bracketed line is often a sung word
# ("(Take on me)"), not structure, and inventing tags is worse than having none.
SECTION_MARKERS = {
    # english (canonical, but normalizes case/variants)
    "verse": "Verse", "chorus": "Chorus", "refrain": "Chorus", "pre-chorus": "Pre-Chorus",
    "prechorus": "Pre-Chorus", "post-chorus": "Post-Chorus", "postchorus": "Post-Chorus",
    "bridge": "Bridge", "intro": "Intro", "outro": "Outro", "hook": "Hook", "hook 1": "Hook",
    "instrumental": "Instrumental", "instrumental break": "Instrumental Break",
    "guitar solo": "Guitar Solo", "solo": "Solo", "interlude": "Interlude",
    "instrumental break": "Instrumental Break", "pause instrumentale": "Instrumental Break",
    "pont instrumental": "Instrumental Break", "间奏独奏": "Instrumental Break",
    "breakdown": "Breakdown", "drop": "Drop", "refrain 1": "Refrain", "tag": "Tag",
    "ending": "Outro", "finale": "Outro", "coda": "Outro", "chorus 1": "Chorus",
    # slavic
    "куплет": "Verse", "куплета": "Verse", "приспів": "Chorus", "припев": "Chorus",
    "предприпев": "Pre-Chorus", "предприспів": "Pre-Chorus", "пост-припев": "Post-Chorus",
    "бридж": "Bridge", "брайдж": "Bridge", "мост": "Bridge", "интро": "Intro", "вступление": "Intro",
    "вступ": "Intro", "аутро": "Outro", "заключение": "Outro", "кода": "Outro",
    "концовка": "Outro", "проигрыш": "Instrumental", "проіграш": "Instrumental",
    "соло": "Solo", "гитарное соло": "Guitar Solo", "инструментал": "Instrumental",
    "хор": "Chorus", "припевка": "Chorus", "zwrotka": "Verse", "refren": "Chorus",
    "most": "Bridge", "wstęp": "Intro", "wyjście": "Outro",
    # romance
    "verso": "Verse", "vers": "Verse", "estrofa": "Verse", "couplet": "Verse",
    "strophe": "Verse", "strofa": "Verse", "estribillo": "Chorus", "coro": "Chorus",
    "refrão": "Chorus", "refrao": "Chorus", "ritornello": "Chorus", "chororus": "Chorus",
    "puente": "Bridge", "ponte": "Bridge", "pont": "Bridge", "introducción": "Intro",
    "introduction": "Intro", "introdução": "Intro", "cierre": "Outro", "final": "Outro",
    "finale": "Outro", "outro": "Outro", "intro": "Intro", "pre-estribillo": "Pre-Chorus",
    # east asian
    "후렴": "Chorus", "サビ": "Chorus", "大サビ": "Chorus", "ラスサビ": "Chorus", "副歌": "Chorus",
    "1절": "Verse 1", "2절": "Verse 2", "3절": "Verse 3", "主歌": "Verse", "Ａメロ": "Verse",
    "Ａメロ 1": "Verse 1", "1番": "Verse 1", "間奏": "Instrumental", "间奏": "Instrumental",
    "간주": "Instrumental", "브릿지": "Bridge", "桥段": "Bridge", "cメロ": "Bridge",
    "イントロ": "Intro", "인트로": "Intro", "前奏": "Intro", "アウトロ": "Outro",
    "아웃트로": "Outro", "尾奏": "Outro",
    # other
    "giriş": "Intro", "nakarat": "Chorus", "kıta": "Verse", "kanta": "Verse",
    "säkeistö": "Verse", "kertaus": "Chorus", "куplet": "Verse",
}
SECTION_DROP = re.compile(r"(?i)^\s*(текст песн[иыяі]|текст пісн|paroles\b|tekst piosenki|lyrics of|"
                          r"about this song|genius translations?|перевод)\b")
MARKER_LINE = re.compile(r"^\s*[\[({]\s*([^()\[\]{}]{1,60}?)\s*[\])}]\s*$")
MARKER_TOKEN = re.compile(r"\[([^\]\[]{1,60})\]")
SEARCH_SCRIPT = re.compile(r"[^A-Za-z\u0400-\u04ff0-9\s'&,./()+-]")


def normalize_marker(inner: str) -> str | None:
    key = re.sub(r"\s+", " ", inner.strip().lower().strip(".:")).strip()
    if not key:
        return None
    base_key, num = key, ""
    parts = re.match(r"^(.+?)[\s\-–—]*(\d{1,2}|[ivx]{1,4})$", key)
    if parts:
        base_key, num = parts.group(1).strip(), parts.group(2).upper()
    base = SECTION_MARKERS.get(base_key) or SECTION_MARKERS.get(key)
    if base is None:
        return None
    return f"{base} {num}".strip()


def normalize_markers(text: str) -> str:
    """Whole-line structure tags → [English]; Genius page-title lines (Текст песни «…») dropped."""
    out = []
    for line in text.split("\n"):
        m = MARKER_LINE.match(line)
        if not m:
            out.append(line)
            continue
        inner = m.group(1)
        if SECTION_DROP.match(inner):
            continue
        repl = normalize_marker(inner)
        out.append(f"[{repl}]" if repl else line.strip())
    return "\n".join(out)


def normalize_markers_inline(text: str) -> str:
    """Same, for markers sitting inside .lrc lines after a timestamp ([00:19.15] [Куплет] →
    [00:19.15] [Verse]); timestamps themselves never normalize."""
    return MARKER_TOKEN.sub(
        lambda m: f"[{normalize_marker(m.group(1))}]" if normalize_marker(m.group(1)) else m.group(0),
        text,
    )


def has_known_markers(text: str) -> bool:
    return any(normalize_marker(m.group(1)) for m in MARKER_TOKEN.finditer(text))


def texts_similar(a: str, b: str) -> bool:
    wa, wb = set(norm_text(a).split()), set(norm_text(b).split())
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.55


def norm_line(line: str) -> str:
    return re.sub(r"[^\w\s]+", " ", norm_text(line))


def synth_tags(text: str) -> str:
    """Last resort when NO source publishes structure tags (config "synth_tags"): the longest
    block of >=2 lines repeated >=2 times (non-overlapping) is the chorus, the gaps between
    repeats are verses. Deliberately narrow — a song without a repeating block gets no tags."""
    lines = text.split("\n")
    content = [i for i, l in enumerate(lines) if l.strip()]
    if len(content) < 4:
        return text
    key = [norm_line(lines[i]) for i in content]
    best: tuple[int, int, list[int]] | None = None  # (span, block_len, [content indexes])
    for size in range(6, 1, -1):
        blocks: dict[tuple[str, ...], list[int]] = {}
        for pos in range(len(content) - size + 1):
            chunk = tuple(key[pos:pos + size])
            if not any(chunk):
                continue
            blocks.setdefault(chunk, []).append(pos)
        for chunk, starts in blocks.items():
            picks: list[int] = []
            for s in starts:
                if not picks or s - picks[-1] >= size:
                    picks.append(s)
            if len(picks) < 2:
                continue
            span = len(picks) * size
            if best is None or (span, size) > (best[0], best[1]):
                best = (span, size, picks)
    if best is None:
        return text
    _, size, picks = best
    spans = [(content[p], content[p + size - 1]) for p in picks]
    markers = {start: "[Chorus]" for start, _ in spans}
    verse, prev_end = 1, -1
    for start, end in spans:
        gap_first = next((i for i in content if prev_end < i < start), None)
        if gap_first is not None:
            markers.setdefault(gap_first, f"[Verse {verse}]")
            verse += 1
        prev_end = end
    out: list[str] = []
    for i, line in enumerate(lines):
        if i in markers:
            out.append(markers[i])
        out.append(line)
    return "\n".join(out)


def lrclib_hits(queries: list[tuple[str, str]], duration: float | None) -> list[dict]:
    hits, seen = [], set()
    for artist, title in queries:
        params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
        for attempt in range(2):
            lyrics_slot()
            status, body = http_get(f"https://lrclib.net/api/search?{params}")
            if status == 200:
                break
            if status != 503 or attempt:
                body = ""
                break
            time.sleep(2.0)
        try:
            found = json.loads(body)
        except ValueError:
            continue
        for e in found[:8]:
            if e.get("id") in seen or e.get("instrumental"):
                continue
            plain = clean_lyrics(e.get("plainLyrics") or "")
            if len(plain) < LYRICS_TEXT_MIN:
                continue
            seen.add(e.get("id"))
            hits.append({
                "source": "lrclib",
                "artist": e.get("artistName") or "",
                "title": e.get("trackName") or "",
                "duration": e.get("duration"),
                "text": plain,
                "synced": clean_lyrics(e.get("syncedLyrics") or "") or None,
            })
    return hits


def genius_hits(queries: list[tuple[str, str]], duration: float | None) -> list[dict]:
    hits, seen = [], set()
    for artist, title in queries:
        lyrics_slot()
        # CJK/hangul in the query makes Genius search return nothing at all
        safe = " ".join(SEARCH_SCRIPT.sub(" ", f"{artist} {title}").split())
        q = urllib.parse.urlencode({"q": safe})
        status, body = http_get(f"https://genius.com/api/search/multi?{q}")
        if status != 200:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for section in (data.get("response") or {}).get("sections") or []:
            for h in section.get("hits") or []:
                r = h.get("result") or {}
                if h.get("type") != "song" or not r.get("url"):
                    continue
                # Genius repeats the same song across search sections — without this the page
                # budget is burned on copies of one (often untagged live) page
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                hits.append({
                    "source": "genius",
                    "artist": r.get("artist_names") or (r.get("primary_artist") or {}).get("name") or "",
                    "title": r.get("title") or "",
                    "duration": None,
                    "url": r["url"],
                })
    return hits


def genius_page_lyrics(url: str) -> str:
    status, page = http_get(url)
    if status != 200 or "data-lyrics-container" not in page:
        return ""
    parser = GeniusLyricsParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        return ""
    return clean_lyrics(parser.text)


def lyrics_ovh_hits(queries: list[tuple[str, str]], duration: float | None) -> list[dict]:
    hits = []
    for artist, title in queries:
        lyrics_slot()
        path = "/".join(urllib.parse.quote(x, safe="") for x in (artist, title))
        status, body = http_get(f"https://api.lyrics.ovh/v1/{path}")
        if status != 200:
            continue
        try:
            text = clean_lyrics(json.loads(body).get("lyrics") or "")
        except ValueError:
            continue
        if len(text) < LYRICS_TEXT_MIN:
            continue
        hits.append({
            "source": "lyrics.ovh", "artist": artist, "title": title,
            "duration": None, "text": text, "synced": None,
        })
    return hits


LYRICS_PROVIDERS = (lrclib_hits, genius_hits, lyrics_ovh_hits)


def score_hit(hit: dict, queries: list[tuple[str, str]], duration: float | None) -> float:
    raw_title = hit.get("title") or ""
    h_titles = [norm_text(raw_title)]
    # Genius titles for non-English songs carry an inline translation: "Туман (Mist)"
    translated = norm_text(re.sub(r"[\(\[{][^\)\]}]*[\)\]}]", " ", raw_title))
    if translated and translated not in h_titles:
        h_titles.append(translated)
    if not any(h_titles):
        return 0.0
    h_artist = norm_text(hit.get("artist") or "")
    best = 0.0
    for a, t in queries:
        qt = norm_text(t)
        title_score = max(name_sim(qt, ht) for ht in h_titles)
        artist_score = name_sim(norm_text(a), h_artist) if h_artist else 0.0
        if title_score < LYRICS_TITLE_MIN_SIM or artist_score < LYRICS_ARTIST_MIN_SIM:
            continue
        s = title_score + artist_score
        hd = hit.get("duration")
        if duration and hd:
            diff = abs(float(duration) - float(hd))
            s += 1.0 if diff <= 5 else (0.4 if diff <= 25 else -1.2)
        best = max(best, s)
    return best


def lyrics_cache_key(queries: list[tuple[str, str]], duration: float | None) -> str:
    basis = "|".join(f"{a}|{t}" for a, t in queries) + f"|{int(duration or 0)}"
    basis += f"|synth={int(bool(CONFIG.get('synth_tags')))}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def save_lyrics_cache(key: str, hit: dict) -> dict:
    try:
        (LYRICS_DIR / f"{key}.txt").write_text(hit["text"] + "\n", encoding="utf-8")
        if hit.get("synced"):
            (LYRICS_DIR / f"{key}.lrc").write_text(hit["synced"] + "\n", encoding="utf-8")
    except OSError:
        pass
    return hit


def genius_pages(scored: list[tuple[float, dict]]) -> dict | None:
    """Highest-scoring Genius page that actually carries structure tags: the top hit is often a
    live/alternate version whose contributors never tagged it, while the studio page has them."""
    picked = None
    for _, h in scored[:LYRICS_PAGE_TRIES]:
        lyrics_slot()
        text = clean_lyrics(normalize_markers(genius_page_lyrics(h["url"])))
        if len(text) < LYRICS_TEXT_MIN:
            continue
        h["text"], h["synced"] = text, None
        if picked is None:
            picked = h
        if has_known_markers(text):
            return h
    return picked


def fetch_lyrics(info: dict) -> dict | None:
    queries = lyrics_queries(info)
    if not queries:
        return None
    duration = info.get("duration")
    key = lyrics_cache_key(queries, duration)
    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    cached = LYRICS_DIR / f"{key}.txt"
    if cached.exists():
        lrc = LYRICS_DIR / f"{key}.lrc"
        return {
            "source": "cache",
            "text": cached.read_text(encoding="utf-8").strip(),
            "synced": lrc.read_text(encoding="utf-8").strip() if lrc.exists() else None,
        }
    candidates: list[tuple[float, dict]] = []
    for search in LYRICS_PROVIDERS:
        try:
            hits = search(queries, duration)
        except Exception:
            continue
        scored = sorted(
            ((score_hit(h, queries, duration), h) for h in hits),
            key=lambda x: x[0],
            reverse=True,
        )
        scored = [(s, h) for s, h in scored if s > 0]
        if not scored:
            continue
        if scored[0][1].get("text"):  # lrclib / lyrics.ovh already carry the text
            for _, h in scored[:2]:
                h["text"] = clean_lyrics(normalize_markers(h["text"]))
                if h.get("synced"):
                    h["synced"] = normalize_markers_inline(h["synced"])
            candidates.extend(scored[:2])
        else:
            page = genius_pages(scored)
            if page:
                candidates.append((scored[0][0], page))
        if any(has_known_markers(h["text"]) for _, h in candidates):
            break  # structure found — no point spending more API budget

    # a source that publishes structure tags wins even against a better-scoring untagged one
    # (Blind Guardian: the top Genius page is an untagged live version, the studio page is tagged);
    # when nothing is tagged, take the best match and write the text without tags
    pool = [c for c in candidates if has_known_markers(c[1]["text"])] or candidates
    if not pool:
        return None
    best = max(pool, key=lambda c: c[0])[1]
    if not best.get("synced"):
        for _, h in candidates:
            if h.get("synced") and texts_similar(best["text"], h["text"]):
                best["synced"] = h["synced"]  # keep the timed .lrc of the same song
                break
    if CONFIG.get("synth_tags") and not has_known_markers(best["text"]):
        best["text"] = synth_tags(best["text"])
    return save_lyrics_cache(key, best)


def sidecar_path(path: Path, ext: str) -> Path:
    stem = path.name[: len(path.name) - len(path.suffix)] if path.suffix else path.name
    return path.with_name(stem + ext)


def write_lyrics_files(path: Path, hit: dict) -> tuple[str | None, str | None]:
    txt = sidecar_path(path, ".txt")
    try:
        txt.write_text(hit["text"] + "\n", encoding="utf-8")
    except OSError:
        return None, None
    lrc = None
    if hit.get("synced"):
        lrc = sidecar_path(path, ".lrc")
        try:
            lrc.write_text(hit["synced"] + "\n", encoding="utf-8")
        except OSError:
            lrc = None
    return str(txt), str(lrc) if lrc else None


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
    for sidecar in (sidecar_path(f, ".png"), sidecar_path(f, ".txt"), sidecar_path(f, ".lrc")):
        if sidecar.exists():
            sidecar.unlink()
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


def lyrics_info_from_name(path: Path) -> dict:
    """Rebuild a lookup query from the library filename (outtmpl is "uploader - title", and the
    uploader is junk — lyrics_queries also splits the title itself and verification rejects
    mismatches). Duration comes from ffprobe, which makes the match much stricter."""
    name = path.name[: len(path.name) - len(path.suffix)]
    uploader, _, rest = name.partition(" - ")
    return {
        "title": (rest or name).strip(),
        "uploader": uploader.strip(),
        "duration": probe_duration(path),
    }


@app.post("/api/lyrics/refresh")
def refresh_lyrics(req: LyricsRefreshRequest):
    f = resolve_file(req.file_id)
    if not f:
        raise HTTPException(status_code=404, detail="file not found")
    try:
        hit = fetch_lyrics(lyrics_info_from_name(f))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"lyrics lookup failed: {type(e).__name__}: {e}")
    if not hit:
        return {"found": False, "filename": f.name}
    txt, lrc = write_lyrics_files(f, hit)
    return {
        "found": True,
        "filename": f.name,
        "lyrics_filename": Path(txt).name if txt else None,
        "lrc_filename": Path(lrc).name if lrc else None,
        "tagged": has_known_markers(hit["text"]),
        "source": hit.get("source"),
    }


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
    for ext in (".png", ".txt", ".lrc"):
        src_side = sidecar_path(src, ext)
        if src_side.exists():
            shutil.copyfile(src_side, sidecar_path(out, ext))
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
        if not job.filepath:
            job.error = "download finished but output file not found"
            job.status = "error"
            return
        if job.cover:
            job.cover_path = extract_cover(job, info)
        if job.lyrics:
            # sidecars must exist BEFORE the job reports "done" — SSE consumers stop at the
            # first done snapshot and would never see lyrics_filename appear
            try:
                hit = fetch_lyrics(info)
            except Exception:
                hit = None
            if hit:
                job.lyrics_path, job.lrc_path = write_lyrics_files(Path(job.filepath), hit)
        job.status = "done"
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
            job = Job(id=uuid.uuid4().hex[:12], url=url, quality=req.quality, cover=req.cover,
                      lyrics=req.lyrics)
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


@app.middleware("http")
async def revalidate_html(request: Request, call_next):
    # StaticFiles sends ETag/Last-Modified but no Cache-Control -> browsers cache
    # index.html heuristically and keep serving a stale UI for hours after an update.
    response = await call_next(request)
    if request.url.path in ("", "/") or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
