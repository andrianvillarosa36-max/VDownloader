# Personal Video Downloader — Backend

A tiny FastAPI server that wraps `yt-dlp`. Your future mobile app calls this
instead of talking to YouTube/TikTok/Instagram/X directly, so all the messy
site-specific extraction logic lives here (and updates with a single
`pip install -U yt-dlp` instead of an app rebuild).

## 1. Run it locally

```bash
cd downloader-backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

export DOWNLOADER_API_KEY="pick-something-long-and-random"
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 2. Test it with curl

```bash
# Ask what formats are available for a URL
curl -X POST "http://localhost:8000/extract?key=pick-something-long-and-random" \
  -H "Content-Type: application/json" \
  -d '{"url": "PASTE_A_VIDEO_URL_HERE"}'

# Download a specific format (use "best" to just grab the best quality)
curl -o video.mp4 \
  "http://localhost:8000/download?key=pick-something-long-and-random&url=PASTE_A_VIDEO_URL_HERE&format_id=best"
```

If `/extract` returns a list of formats and `/download` saves a playable
file, the backend is working — the mobile app is just a UI on top of this.

## 3. Reach it from your phone securely

Don't port-forward this straight to the public internet — the API key
helps, but it's a personal tool, not something hardened for the open web.
Instead, install [Tailscale](https://tailscale.com) on both the server and
your phone (free for personal use). It creates a private network between
your own devices, so your phone can reach `http://your-server-name:8000`
from anywhere, without the server ever being reachable by anyone else.

## 4. Keeping it working over time

Platforms change things and yt-dlp ships fixes fast, often within days.
Update every so often with:

```bash
pip install -U yt-dlp
```

## Notes

- This only works on content that isn't DRM-protected, which covers normal
  public posts on YouTube/TikTok/Instagram/X.
- Private/login-gated content needs your own session cookies passed to
  yt-dlp (`--cookies-from-browser` or a cookies file) — ask me if you want
  this wired in; it's a small addition once the basic flow works.
- Built for your own personal use on your own devices. These platforms'
  Terms of Service don't permit downloading content generally, so keep this
  to content you own or have the right to save, and don't redistribute it.
