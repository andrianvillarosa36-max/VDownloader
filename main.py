import os
import re
import json
import asyncio
import sqlite3
import shutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DB_PATH = "vault.db"
COOKIES_PATH = "cookies.txt"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            filename TEXT,
            format_type TEXT,
            quality TEXT,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_saved_to_phone INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()
main_loop = None

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()

@app.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

class DownloadRequest(BaseModel):
    url: str
    format_type: str = "mp4"
    quality: str = "best"

def clean_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', str(text)).strip() if text else ""

def create_progress_hook():
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            
            percentage = round((downloaded / total) * 100, 1) if total > 0 else 0
            speed = clean_ansi(d.get('_speed_str', '0 KB/s'))
            eta = clean_ansi(d.get('_eta_str', '--'))

            payload = {
                "status": "downloading",
                "percentage": percentage,
                "speed": speed if speed else "Calculating...",
                "eta": eta if eta else "--"
            }

            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast(json.dumps(payload)),
                    main_loop
                )

        elif d['status'] == 'finished':
            payload = {"status": "processing", "percentage": 100, "speed": "Done", "eta": "0s"}
            if main_loop and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast(json.dumps(payload)),
                    main_loop
                )

    return hook

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

@app.get("/manifest.json")
def get_manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")

@app.get("/files/{filename}")
def get_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")

@app.post("/download")
async def download_video(req: DownloadRequest):
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
        'progress_hooks': [create_progress_hook()],
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'twitter': {
                'api': ['graphql', 'syndication', 'legacy']
            }
        }
    }

    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH

    if req.format_type == "mp3":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        if req.quality != "best":
            ydl_opts['format'] = f"bestvideo[height<={req.quality}]+bestaudio/best[height<={req.quality}]/best"
        else:
            ydl_opts['format'] = "best"

    loop = asyncio.get_event_loop()
    
    def run_dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            filename = ydl.prepare_filename(info)
            if req.format_type == "mp3":
                filename = os.path.splitext(filename)[0] + ".mp3"
            return os.path.basename(filename), info.get('title', 'Downloaded Media')

    try:
        filename, title = await loop.run_in_executor(None, run_dl)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO downloads (title, filename, format_type, quality) VALUES (?, ?, ?, ?)",
            (title, filename, req.format_type, req.quality)
        )
        conn.commit()
        conn.close()

        return {"status": "success", "filename": filename, "title": title}
    except Exception as e:
        clean_error = clean_ansi(str(e))
        raise HTTPException(status_code=500, detail=clean_error)

@app.get("/history")
def get_history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM downloads ORDER BY id DESC")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/save-to-phone/{file_id}")
def save_to_phone(file_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM downloads WHERE id = ?", (file_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Item not found")

    filename = row[0]
    src_path = os.path.join(DOWNLOAD_DIR, filename)
    dest_dir = "/sdcard/Download"
    dest_path = os.path.join(dest_dir, filename)

    if not os.path.exists(src_path):
        conn.close()
        raise HTTPException(status_code=404, detail="File deleted from server")

    shutil.copy(src_path, dest_path)
    cursor.execute("UPDATE downloads SET is_saved_to_phone = 1 WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()

    return {"status": "success", "path": dest_path}

@app.delete("/history/{file_id}")
def delete_history(file_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM downloads WHERE id = ?", (file_id,))
    row = cursor.fetchone()

    if row:
        file_path = os.path.join(DOWNLOAD_DIR, row[0])
        if os.path.exists(file_path):
            os.remove(file_path)

    cursor.execute("DELETE FROM downloads WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

