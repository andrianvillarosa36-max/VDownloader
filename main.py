from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import yt_dlp
import os

app = FastAPI(title="Personal Video Downloader")

# Request schema to accept user settings from the UI
class DownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4"  # "mp4" or "mp3"
    quality: str = "best"     # "1080", "720", "480", or "best"

@app.post("/extract")
def extract_info(request: DownloadRequest):
    """Fetches video metadata before downloading."""
    ydl_opts = {
        'quiet': True,
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None[span_5](start_span)[span_5](end_span)
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=False)
            return {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader")
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/download")
def start_download(request: DownloadRequest):
    """Downloads video or audio based on user quality and format settings."""
    out_dir = "downloads"
    os.makedirs(out_dir, exist_ok=True)
    
    # Configure format string based on user settings
    if request.format_type == "mp3":
        format_spec = "bestaudio/best"
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    else:
        postprocessors = []
        if request.quality == "1080":
            format_spec = "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
        elif request.quality == "720":
            format_spec = "bestvideo[height<=720]+bestaudio/best[height<=720]"
        else:
            format_spec = "best"

    ydl_opts = {
        'format': format_spec,
        'outtmpl': f'{out_dir}/%(title)s.%(ext)s',
        'postprocessors': postprocessors,
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None[span_6](start_span)[span_6](end_span)
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=True)
            filename = ydl.prepare_filename(info)
            if request.format_type == "mp3":
                filename = os.path.splitext(filename)[0] + ".mp3"
                
            return {
                "status": "success",
                "filename": os.path.basename(filename),
                "path": filename
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

