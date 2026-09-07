"""
Personal video-download backend.

Wraps yt-dlp behind a small HTTP API so your own mobile app can call it.
This is meant to run on a machine YOU control (home computer, Raspberry Pi,
or a personal VPS) and be reached only from your own devices — see the
README for how to lock it down with a key + Tailscale.
"""

import os
import re
import tempfile
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Personal Video Downloader")

# Strips terminal color codes (e.g. "\x1b[0;31m") that yt-dlp includes in some
# error messages -- harmless in a terminal, but ugly/broken if shown in a browser.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_error(e: Exception) -> str:
    return _ANSI_RE.sub("", str(e))


# If you've added a cookies.txt (exported while logged into a site like X),
# every request automatically uses it. If the file isn't there, everything
# just falls back to anonymous access like before -- nothing breaks either way.
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")


def _ydl_opts(extra: dict) -> dict:
    opts = {"quiet": True, "no_color": True, "noplaylist": True, **extra}
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts

# Simple shared-secret so random people can't hit your server if it's ever
# reachable from outside your own devices. Set a long random value via env var:
#   export DOWNLOADER_API_KEY="something-long-and-random"
API_KEY = os.environ.get("DOWNLOADER_API_KEY", "change-me")

DOWNLOAD_DIR = tempfile.gettempdir()


class ExtractRequest(BaseModel):
    url: str


def _check_key(key: str) -> None:
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.post("/extract")
def extract(req: ExtractRequest, key: str = Query(...)):
    """Look up a URL and return available formats without downloading anything."""
    _check_key(key)

    ydl_opts = _ydl_opts({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read that URL: {_clean_error(e)}")

    formats = [
        {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        }
        for f in info.get("formats", [])
        if f.get("vcodec") not in (None, "none")  # skip audio-only entries
    ]

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "formats": formats,
    }


@app.get("/download")
def download(url: str, format_id: str = "best", key: str = Query(...)):
    """Download the chosen format and hand the file back to the app."""
    _check_key(key)

    file_id = str(uuid.uuid4())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = _ydl_opts({"format": format_id, "outtmpl": outtmpl})
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {_clean_error(e)}")

    if not os.path.exists(filename):
        raise HTTPException(status_code=500, detail="File missing after download")

    return FileResponse(filename, filename=os.path.basename(filename))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/share-target")
def share_target(url: str = "", text: str = ""):
    """Android's Web Share Target sends the shared link here; bounce it to the
    main page with the URL pre-filled so sharing from another app just works."""
    shared = url or text
    return RedirectResponse(f"/?url={shared}")


# Serves static/index.html at "/" plus manifest.json and sw.js.
# Mounted last so it never shadows the API routes above.
app.mount("/", StaticFiles(directory="static", html=True), name="static")

