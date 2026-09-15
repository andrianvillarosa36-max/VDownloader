import os
import shutil
import asyncio
import json
import requests
import re
import time
import subprocess
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="VaultDL")

DOWNLOADS_DIR = "downloads"
INCOMING_DIR = "incoming"
COOKIES_FILE = "cookies.txt"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(INCOMING_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

download_tasks = {}
cancel_requested = set()
download_queue = None
queue_order = []  # task_ids waiting their turn, in FIFO order — used to show queue position


class CancelledDownload(Exception):
    """Raised internally when a user cancels an in-progress download."""
    pass


async def download_worker():
    """Pulls one download at a time off the queue and runs it to completion
    before starting the next — this is what makes downloads queue instead
    of all racing to run concurrently."""
    while True:
        url, format_type, quality, task_id = await download_queue.get()
        if task_id in queue_order:
            queue_order.remove(task_id)

        if task_id in cancel_requested:
            # Cancelled while still waiting in line — skip it entirely.
            cancel_requested.discard(task_id)
            download_tasks[task_id] = {"status": "cancelled", "percent": 0, "eta_seconds": 0}
            download_queue.task_done()
            continue

        try:
            await run_in_threadpool(execute_download, url, format_type, quality, task_id)
        except Exception as e:
            download_tasks[task_id] = {"status": "error", "error": f"Unexpected error: {str(e)}"}
        finally:
            download_queue.task_done()


@app.on_event("startup")
async def start_download_worker():
    global download_queue
    download_queue = asyncio.Queue()
    asyncio.create_task(download_worker())

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/media_files", StaticFiles(directory=DOWNLOADS_DIR), name="media_files")
app.mount("/incoming_files", StaticFiles(directory=INCOMING_DIR), name="incoming_files")

@app.get("/sw.js")
def get_service_worker():
    sw_path = os.path.join("static", "sw.js")
    if os.path.exists(sw_path):
        return FileResponse(sw_path, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="Service worker not found")

@app.get("/favicon.ico")
def get_favicon():
    manifest_path = os.path.join("static", "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path)
    raise HTTPException(status_code=404, detail="Favicon not found")

class RenameRequest(BaseModel):
    old_filename: str
    new_filename: str

@app.get("/")
def read_root():
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Index file not found")

def progress_hook(d, task_id):
    if task_id in cancel_requested:
        raise CancelledDownload()

    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
        downloaded = d.get('downloaded_bytes', 0)
        pct = round((downloaded / total) * 100, 1)
        eta = d.get('eta', 0)

        download_tasks[task_id] = {
            "status": "downloading",
            "percent": pct,
            "downloaded_mb": round(downloaded / (1024 * 1024), 2),
            "total_mb": round(total / (1024 * 1024), 2),
            "eta_seconds": eta or 0
        }
    elif d['status'] == 'finished':
        download_tasks[task_id] = {
            "status": "finished",
            "percent": 100,
            "downloaded_mb": round(d.get('total_bytes', 0) / (1024 * 1024), 2),
            "total_mb": round(d.get('total_bytes', 0) / (1024 * 1024), 2),
            "eta_seconds": 0
        }

def resolve_hidden_media_stream(url: str):
    """Scrapes raw web pages and hidden iframes for direct .m3u8 or .mp4 stream links."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": url
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        html = res.text
        
        # 1. Search directly for .m3u8 or .mp4 URLs embedded in JS or HTML
        direct_streams = re.findall(r'https?://[^\s\'"<>]+?\.(?:m3u8|mp4)[^\s\'"<>]*', html)
        if direct_streams:
            return direct_streams[0]
            
        # 2. If not found, inspect all embedded iframe players on the page
        iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        for iframe_url in iframes:
            if iframe_url.startswith('//'):
                iframe_url = 'https:' + iframe_url
            if iframe_url.startswith('http'):
                try:
                    sub_res = requests.get(iframe_url, headers=headers, timeout=8)
                    sub_streams = re.findall(r'https?://[^\s\'"<>]+?\.(?:m3u8|mp4)[^\s\'"<>]*', sub_res.text)
                    if sub_streams:
                        return sub_streams[0]
                except Exception:
                    continue
    except Exception:
        pass
    return None

def download_twitter_vx(url: str, task_id: str, format_type: str):
    """Bypasses Twitter/X guest API restrictions via vxTwitter API."""
    try:
        download_tasks[task_id] = {"status": "downloading", "percent": 0, "eta_seconds": 0}
        
        api_url = re.sub(r'https?://(www\.)?(twitter\.com|x\.com)', 'https://api.vxtwitter.com', url)
        api_url = api_url.split('?')[0]
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(api_url, headers=headers, timeout=15)
        if response.status_code != 200:
            return False, f"API rejected the request (Code {response.status_code})"
            
        data = response.json()
        media_urls = data.get("mediaURLs", [])
        if not media_urls:
            return False, "No playable media found in this tweet."
            
        video_url = media_urls[0]
        author = data.get("user_screen_name", "twitter_user")
        tweet_id = data.get("tweetID", task_id)
        filename = f"{author}_{tweet_id}.mp4"
        filepath = os.path.join(INCOMING_DIR, filename)
        
        file_res = requests.get(video_url, stream=True, timeout=30)
        total_size = int(file_res.headers.get('content-length', 0))
        downloaded = 0
        start_time = time.time()
        
        with open(filepath, "wb") as f:
            for chunk in file_res.iter_content(chunk_size=8192):
                if task_id in cancel_requested:
                    f.close()
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    raise CancelledDownload()
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = (downloaded / total_size) * 100
                        elapsed_time = time.time() - start_time
                        speed = downloaded / elapsed_time if elapsed_time > 0 else 0
                        remaining_bytes = total_size - downloaded
                        eta = int(remaining_bytes / speed) if speed > 0 else 0
                        
                        download_tasks[task_id].update({
                            "percent": round(pct, 1),
                            "downloaded_mb": round(downloaded / (1024 * 1024), 2),
                            "total_mb": round(total_size / (1024 * 1024), 2),
                            "eta_seconds": eta
                        })
        
        if format_type == 'mp3':
            download_tasks[task_id] = {"status": "processing", "percent": 99, "eta_seconds": 0}
            mp3_filepath = filepath.rsplit('.', 1)[0] + '.mp3'
            subprocess.run([
                "ffmpeg", "-y", "-i", filepath, 
                "-q:a", "0", "-map", "a", mp3_filepath
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(filepath)

        return True, None
    except CancelledDownload:
        raise
    except Exception as e:
        return False, str(e)

def cleanup_partial_files(start_ts):
    """Best-effort removal of partial download artifacts left behind by a cancelled yt-dlp run."""
    if not os.path.exists(INCOMING_DIR):
        return
    for f in os.listdir(INCOMING_DIR):
        fp = os.path.join(INCOMING_DIR, f)
        if not os.path.isfile(fp):
            continue
        if (f.endswith(".part") or f.endswith(".ytdl")) and os.path.getmtime(fp) >= start_ts - 1:
            try:
                os.remove(fp)
            except OSError:
                pass

def execute_download(target_url: str, format_type: str, quality: str, task_id: str):
    download_tasks[task_id] = {"status": "starting", "percent": 0}
    start_ts = time.time()

    try:
        # Twitter/X override
        if "twitter.com" in target_url.lower() or "x.com" in target_url.lower():
            try:
                success, err = download_twitter_vx(target_url, task_id, format_type)
            except CancelledDownload:
                download_tasks[task_id] = {"status": "cancelled", "percent": 0, "eta_seconds": 0}
                return
            if success:
                download_tasks[task_id] = {"status": "finished", "percent": 100, "eta_seconds": 0}
                return
            # vxtwitter failed (rate-limited, blocked, down, etc.) — fall through
            # and let yt-dlp's own Twitter/X extractor try the original URL
            # directly, instead of giving up on the whole download.
            download_tasks[task_id] = {"status": "downloading", "percent": 0, "eta_seconds": 0}

        # Standard download options
        ydl_opts = {
            'outtmpl': os.path.join(INCOMING_DIR, '%(title)s.%(ext)s'),
            'progress_hooks': [lambda d: progress_hook(d, task_id)],
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
        }
        if os.path.exists(COOKIES_FILE):
            ydl_opts['cookiefile'] = COOKIES_FILE

        if format_type == 'mp3':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            if quality == '1080p':
                ydl_opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
            elif quality == '720p':
                ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
            else:
                ydl_opts['format'] = 'best'

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])
            download_tasks[task_id]['status'] = 'finished'
        except CancelledDownload:
            download_tasks[task_id] = {"status": "cancelled", "percent": 0, "eta_seconds": 0}
            cleanup_partial_files(start_ts)
        except Exception as initial_error:
            # Auto-Resolver Step: If yt-dlp fails to recognize the webpage, attempt stream scraping
            resolved_stream = resolve_hidden_media_stream(target_url)
            if resolved_stream:
                try:
                    # Add proper stream headers for resolved video links
                    ydl_opts['http_headers'] = {'Referer': target_url}
                    ydl_opts['outtmpl'] = os.path.join(INCOMING_DIR, f'web_stream_{task_id[:8]}.%(ext)s')

                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([resolved_stream])
                    download_tasks[task_id]['status'] = 'finished'
                except CancelledDownload:
                    download_tasks[task_id] = {"status": "cancelled", "percent": 0, "eta_seconds": 0}
                    cleanup_partial_files(start_ts)
                except Exception as stream_error:
                    download_tasks[task_id] = {"status": "error", "error": f"Stream extracted but failed to download: {str(stream_error)}"}
            else:
                download_tasks[task_id] = {"status": "error", "error": f"Unsupported website and no video stream could be found automatically."}
    finally:
        cancel_requested.discard(task_id)

@app.post("/download/cancel/{task_id}")
def cancel_download(task_id: str):
    if task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Unknown task_id")
    current = download_tasks.get(task_id, {})
    if current.get("status") == "queued":
        if task_id in queue_order:
            queue_order.remove(task_id)
        cancel_requested.add(task_id)  # belt-and-suspenders in case the worker already dequeued it
        download_tasks[task_id] = {"status": "cancelled", "percent": 0, "eta_seconds": 0}
    else:
        cancel_requested.add(task_id)
        download_tasks[task_id] = {**current, "status": "cancelling"}
    return {"status": "cancel_requested"}


@app.post("/download")
async def start_download(
    url: str = Query(...),
    format_type: str = Query("mp4"),
    quality: str = Query("best"),
    task_id: str = Query(...)
):
    queue_order.append(task_id)
    position = len(queue_order)
    download_tasks[task_id] = {"status": "queued", "percent": 0, "queue_position": position}
    await download_queue.put((url, format_type, quality, task_id))
    return {"status": "queued", "task_id": task_id, "queue_position": position}

@app.get("/download/progress/{task_id}")
async def get_progress(task_id: str):
    async def event_generator():
        while True:
            task = download_tasks.get(task_id, {"status": "initializing", "percent": 0})
            if task.get("status") == "queued" and task_id in queue_order:
                task = {**task, "queue_position": queue_order.index(task_id) + 1}
            yield f"data: {json.dumps(task)}\n\n"
            if task.get("status") in ["finished", "error", "cancelled"]:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/downloads/list")
def list_downloads():
    files = []
    if os.path.exists(DOWNLOADS_DIR):
        for f in os.listdir(DOWNLOADS_DIR):
            file_path = os.path.join(DOWNLOADS_DIR, f)
            if os.path.isfile(file_path):
                stat = os.stat(file_path)
                ext = f.split('.')[-1].upper() if '.' in f else 'FILE'
                files.append({
                    "name": f,
                    "path": f"/media_files/{f}",
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "size_bytes": stat.st_size,
                    "timestamp": stat.st_mtime,
                    "ext": ext
                })
    return files

@app.get("/incoming/list")
def list_incoming():
    files = []
    if os.path.exists(INCOMING_DIR):
        for f in os.listdir(INCOMING_DIR):
            file_path = os.path.join(INCOMING_DIR, f)
            if os.path.isfile(file_path) and not (f.endswith(".part") or f.endswith(".ytdl")):
                stat = os.stat(file_path)
                ext = f.split('.')[-1].upper() if '.' in f else 'FILE'
                files.append({
                    "name": f,
                    "path": f"/incoming_files/{f}",
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "size_bytes": stat.st_size,
                    "timestamp": stat.st_mtime,
                    "ext": ext
                })
    return files

@app.delete("/incoming/delete")
def delete_incoming_file(filename: str = Query(...)):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(INCOMING_DIR, safe_filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        os.remove(file_path)
    return {"status": "success"}

@app.post("/incoming/save")
def save_incoming_file(filename: str = Query(...)):
    """Moves a file out of the incoming/staging area and into the permanent downloads library."""
    safe_filename = os.path.basename(filename)
    src_path = os.path.join(INCOMING_DIR, safe_filename)
    if not (os.path.exists(src_path) and os.path.isfile(src_path)):
        raise HTTPException(status_code=404, detail="File not found")

    dest_name = safe_filename
    dest_path = os.path.join(DOWNLOADS_DIR, dest_name)
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(safe_filename)
        n = 1
        while os.path.exists(dest_path):
            dest_name = f"{base}_{n}{ext}"
            dest_path = os.path.join(DOWNLOADS_DIR, dest_name)
            n += 1

    shutil.move(src_path, dest_path)
    return {"status": "success", "filename": dest_name, "path": f"/media_files/{dest_name}"}

@app.delete("/downloads/delete")
def delete_single_file(filename: str = Query(...)):
    safe_filename = os.path.basename(filename)
    file_path = os.path.join(DOWNLOADS_DIR, safe_filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        os.remove(file_path)
    return {"status": "success"}

@app.post("/downloads/rename")
def rename_file(req: RenameRequest):
    safe_old = os.path.basename(req.old_filename)
    safe_new = os.path.basename(req.new_filename)
    old_path = os.path.join(DOWNLOADS_DIR, safe_old)
    new_path = os.path.join(DOWNLOADS_DIR, safe_new)

    if not os.path.exists(old_path):
        raise HTTPException(status_code=404, detail="File not found")
    if os.path.exists(new_path):
        raise HTTPException(status_code=400, detail="A file with that name already exists")

    os.rename(old_path, new_path)
    return {"status": "success"}

@app.delete("/downloads/clear")
def clear_downloads():
    if os.path.exists(DOWNLOADS_DIR):
        for f in os.listdir(DOWNLOADS_DIR):
            fp = os.path.join(DOWNLOADS_DIR, f)
            if os.path.isfile(fp):
                os.remove(fp)
    return {"status": "success"}

@app.get("/system/storage")
def storage_info():
    total_size = 0
    count = 0
    for d in (DOWNLOADS_DIR, INCOMING_DIR):
        if os.path.exists(d):
            for f in os.listdir(d):
                fp = os.path.join(d, f)
                if os.path.isfile(fp):
                    total_size += os.path.getsize(fp)
                    count += 1
    total, used, free = shutil.disk_usage("/")
    return {
        "vault_size_mb": round(total_size / (1024 * 1024), 2),
        "file_count": count,
        "disk_free_gb": round(free / (1024 * 1024 * 1024), 2)
    }

