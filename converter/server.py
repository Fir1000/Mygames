"""Mygames converter - local server that turns TikTok / YouTube links into MP3 or MP4.

Run with start.bat (Windows) or:  python server.py
Then open http://127.0.0.1:8765
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed. Run start.bat, or: pip install -r requirements.txt")

HOST, PORT = "127.0.0.1", 8765
BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

ALLOWED_HOSTS = ("youtube.com", "youtu.be", "tiktok.com")
MP3_QUALITIES = {"320", "192", "128"}
MP4_QUALITIES = {"2160", "1440", "1080", "720", "480", "360"}


def find_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()
jobs = {}
jobs_lock = threading.Lock()


def platform_of(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    for h in ALLOWED_HOSTS:
        if host == h or host.endswith("." + h):
            return "tiktok" if h == "tiktok.com" else "youtube"
    return None


def friendly_error(exc):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    low = msg.lower()
    if "private" in low:
        return "วิดีโอนี้เป็นส่วนตัว ดาวน์โหลดไม่ได้"
    if "not a bot" in low:
        return "YouTube ขอให้ยืนยันว่าไม่ใช่บอท รอสักพักแล้วลองใหม่"
    if "sign in" in low or "age-restricted" in low or "inappropriate" in low:
        return "วิดีโอนี้ต้องล็อกอินหรือจำกัดอายุ ดาวน์โหลดไม่ได้"
    if "unavailable" in low or "not available" in low or "removed" in low:
        return "ไม่พบวิดีโอ หรือวิดีโอถูกลบ/ไม่เปิดให้ดูในประเทศนี้"
    if "live" in low and "stream" in low:
        return "ยังไม่รองรับไลฟ์สด"
    if "ffmpeg" in low:
        return "ไม่พบ ffmpeg ให้รัน start.bat ใหม่อีกครั้ง"
    if "unable to download" in low or "timed out" in low or "network" in low:
        return "เชื่อมต่อไม่ได้ ตรวจสอบอินเทอร์เน็ตแล้วลองใหม่"
    return "แปลงไม่สำเร็จ: " + msg.replace("ERROR: ", "")[:200]


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "socket_timeout": 20,
        # Use the OS certificate store instead of certifi, so HTTPS-scanning antivirus works.
        "compat_opts": {"no-certifi"},
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    return opts


def get_info(url):
    with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)
    heights = sorted({f.get("height") for f in info.get("formats") or [] if f.get("height")}, reverse=True)
    return {
        "title": info.get("title") or "ไม่มีชื่อ",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "is_live": bool(info.get("is_live")),
        "heights": heights,
    }


def run_job(job_id, url, fmt, quality):
    job = jobs[job_id]

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job.update(
                status="downloading",
                percent=round(done * 100 / total, 1) if total else None,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            job.update(status="processing", percent=100)

    opts = {
        **base_opts(),
        "outtmpl": str(DOWNLOADS / "%(title).80B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
    }
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "FFmpegMetadata"},
        ]
    else:
        h = int(quality)
        # Prefer H.264 + AAC so the file plays everywhere (Windows, phones, TVs).
        opts["format"] = (
            f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/b[height<={h}]/b"
        )
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]

    try:
        if not FFMPEG:
            raise RuntimeError("ffmpeg not found")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist":
            info = next(e for e in info["entries"] if e)
        path = Path(info["requested_downloads"][0]["filepath"])
        job.update(status="done", percent=100, file=str(path), filename=path.name,
                   size=path.stat().st_size, title=info.get("title"))
    except Exception as exc:  # noqa: BLE001 - every failure is shown to the user
        job.update(status="error", error=friendly_error(exc))


class Handler(BaseHTTPRequestHandler):
    server_version = "MygamesConverter/1.0"

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        # Lets the page also work when opened from the game hub (file:// or another port).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/converter.html"):
            return self._send_file(BASE.parent / "converter.html", "text/html; charset=utf-8")
        if path == "/api/health":
            return self._json({"ok": True, "ffmpeg": bool(FFMPEG), "yt_dlp": yt_dlp.version.__version__})
        m = re.fullmatch(r"/api/progress/([0-9a-f]{32})", path)
        if m:
            job = jobs.get(m.group(1))
            if not job:
                return self._json({"error": "ไม่พบงานนี้"}, 404)
            return self._json({k: v for k, v in job.items() if k != "file"})
        m = re.fullmatch(r"/api/file/([0-9a-f]{32})", path)
        if m:
            job = jobs.get(m.group(1))
            if not job or job.get("status") != "done" or not Path(job["file"]).exists():
                return self._json({"error": "ไฟล์ยังไม่พร้อม"}, 404)
            ctype = "audio/mpeg" if job["file"].endswith(".mp3") else "video/mp4"
            return self._send_file(Path(job["file"]), ctype, download_name=job["filename"])
        if path == "/api/open-folder":
            if sys.platform == "win32":
                os.startfile(DOWNLOADS)  # noqa: S606 - local folder only
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        url = (data.get("url") or "").strip()
        if path in ("/api/info", "/api/start"):
            if not platform_of(url):
                return self._json({"error": "รองรับเฉพาะลิงก์ YouTube และ TikTok"}, 400)
        if path == "/api/info":
            try:
                info = get_info(url)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": friendly_error(exc)}, 400)
            info["platform"] = platform_of(url)
            return self._json(info)
        if path == "/api/start":
            fmt = data.get("format")
            quality = str(data.get("quality") or "")
            if fmt == "mp3" and quality not in MP3_QUALITIES:
                quality = "192"
            elif fmt == "mp4" and quality not in MP4_QUALITIES:
                quality = "1080"
            elif fmt not in ("mp3", "mp4"):
                return self._json({"error": "เลือกได้แค่ MP3 หรือ MP4"}, 400)
            job_id = uuid.uuid4().hex
            with jobs_lock:
                jobs[job_id] = {"id": job_id, "status": "queued", "percent": 0,
                                "format": fmt, "quality": quality, "created": time.time()}
            threading.Thread(target=run_job, args=(job_id, url, fmt, quality), daemon=True).start()
            return self._json({"id": job_id})
        self._json({"error": "not found"}, 404)

    def _send_file(self, path, ctype, download_name=None):
        if not path.exists():
            return self._json({"error": "not found"}, 404)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download_name:
            ascii_name = download_name.encode("ascii", "ignore").decode() or "download"
            ascii_name = ascii_name.replace('"', "")
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(download_name)}")
        self._cors()
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Mygames converter running at {url}")
    print(f"Files are saved in: {DOWNLOADS}")
    print("ffmpeg:", FFMPEG or "NOT FOUND (MP3 and HD MP4 will fail)")
    print("Close this window to stop.")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
