
import ipaddress
import os
import shutil
import socket
import tempfile
import threading
import time

from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__)

# Configuration
HAS_FFMPEG = shutil.which("ffmpeg") is not None
DIRECT = bool(os.environ.get("VERCEL") or os.environ.get("VIDGRAB_DIRECT"))
COOKIES_FILE = os.environ.get("VIDGRAB_COOKIES")
MAX_REQUESTS_PER_MIN = int(os.environ.get("VIDGRAB_RATE", "12"))

_hits = defaultdict(deque)
_lock = threading.Lock()


def rate_limited(ip):
    now = time.time()

    with _lock:
        q = _hits[ip]

        while q and now - q[0] > 60:
            q.popleft()

        if len(q) >= MAX_REQUESTS_PER_MIN:
            return True

        q.append(now)

    return False


def check_url(url):
    """Validate the supplied video URL."""

    if not url or len(url) > 2000:
        return "Paste a video link."

    p = urlparse(url)

    if p.scheme not in ("http", "https") or not p.hostname:
        return "That doesn't look like a valid web link."

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(p.hostname, None)
        }

        if not addresses:
            return "Could not find that website."

        if not all(ipaddress.ip_address(a).is_global for a in addresses):
            return "That address is not allowed."

    except (socket.gaierror, ValueError):
        return "Could not validate that website."

    return None


def ydl_base():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }

    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    return opts


def direct_links(data):
    best = {}

    for f in data.get("formats") or []:
        height = f.get("height")
        url = f.get("url")

        if not height or not url:
            continue

        if f.get("vcodec") in (None, "none"):
            continue

        if f.get("acodec") in (None, "none"):
            continue

        if f.get("protocol") not in ("http", "https"):
            continue

        if height not in best or (f.get("tbr") or 0) > (
            best[height].get("tbr") or 0
        ):
            best[height] = f

    links = [
        {
            "height": height,
            "url": best[height]["url"],
            "ext": best[height].get("ext") or "mp4",
        }
        for height in sorted(best, reverse=True)
    ]

    if not links and data.get("url") and data.get("vcodec") != "none":
        links = [{
            "height": data.get("height") or 0,
            "url": data["url"],
            "ext": data.get("ext") or "mp4",
        }]

    return links


def pick_selector(q):
    if q == "audio":
        return "bestaudio/best"

    if q.isdigit():
        height = int(q)

        if HAS_FFMPEG:
            return (
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]/best"
            )

        return f"best[height<={height}]/best"

    return (
        "bestvideo+bestaudio/best"
        if HAS_FFMPEG
        else "best"
    )


@app.before_request
def limit():
    if request.path.startswith("/api/"):
        if rate_limited(request.remote_addr or "?"):
            return jsonify(
                error="Too many requests. Wait a minute and try again."
            ), 429


@app.post("/api/info")
def info():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()

    err = check_url(url)

    if err:
        return jsonify(error=err), 400

    try:
        with yt_dlp.YoutubeDL(ydl_base()) as ydl:
            data = ydl.extract_info(url, download=False)

    except yt_dlp.utils.DownloadError as e:
        msg = str(e).replace("ERROR: ", "")[:300]
        return jsonify(error=f"Could not read that link. {msg}"), 422

    except Exception:
        app.logger.exception("Video information extraction failed")
        return jsonify(error="An unexpected error occurred."), 500

    if data and data.get("entries"):
        data = next(
            (entry for entry in data["entries"] if entry),
            None
        )

    if not data:
        return jsonify(error="No video found at that link."), 404

    heights = sorted({
        f["height"]
        for f in data.get("formats", [])
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }, reverse=True)

    return jsonify(
        title=data.get("title") or "Untitled",
        uploader=data.get("uploader") or "",
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        heights=heights,
        ffmpeg=HAS_FFMPEG,
        direct=DIRECT,
        links=direct_links(data) if DIRECT else [],
    )


@app.get("/api/download")
def download():
    if DIRECT:
        return jsonify(
            error="Server-side download is disabled in direct mode."
        ), 501

    url = (request.args.get("url") or "").strip()
    q = (request.args.get("q") or "best").strip()

    if q not in ("best", "audio") and not (
        q.isdigit() and 1 <= int(q) <= 4320
    ):
        return jsonify(error="Unknown quality."), 400

    err = check_url(url)

    if err:
        return jsonify(error=err), 400

    tmp = tempfile.mkdtemp(prefix="vidgrab_")

    opts = ydl_base()

    opts.update({
        "format": pick_selector(q),
        "playlist_items": "1",
        "outtmpl": os.path.join(
            tmp,
            "%(title).80s [%(id)s].%(ext)s"
        ),
    })

    if q == "audio" and HAS_FFMPEG:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    elif q != "audio" and HAS_FFMPEG:
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [
            f for f in Path(tmp).iterdir()
            if f.is_file()
        ]

        if not files:
            raise RuntimeError("Nothing was downloaded.")

        path = max(files, key=lambda f: f.stat().st_size)

        response = send_file(
            path,
            as_attachment=True,
            download_name=path.name
        )

        response.headers["X-Filename"] = quote(path.name)

        response.call_on_close(
            lambda: shutil.rmtree(tmp, ignore_errors=True)
        )

        return response

    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)

        app.logger.exception("Video download failed")

        return jsonify(
            error=f"Download failed. {str(e)[:300]}"
        ), 502


@app.get("/")
def home():
    public_dir = Path(__file__).resolve().parent / "public"

    return send_from_directory(public_dir, "index.html")


if __name__ == "__main__":

    if not HAS_FFMPEG:
        print(
            "FFmpeg not found: only pre-merged formats "
            "and native audio will be available."
        )

    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        threaded=True,
        debug=False
    )
