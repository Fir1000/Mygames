"""Mygames converter - local server that turns TikTok / YouTube links into MP3 or MP4.

Run with start.bat (Windows) or:  python server.py
Then open http://127.0.0.1:8765
"""
import io
import json
import os
import re
import shutil
import socket
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

# 0.0.0.0 = also reachable from phones on the same Wi-Fi (scan the QR code on the PC page).
HOST, PORT = "0.0.0.0", 8765
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


def find_lan_ip():
    """IP of the network card that reaches the internet (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


FFMPEG = find_ffmpeg()
LAN_IP = find_lan_ip()
LAN_URL = f"http://{LAN_IP}:{PORT}" if LAN_IP else None
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
    if "unsupported url" in low:
        return "ลิงก์นี้ไม่ใช่ลิงก์คลิป หรือลิงก์หมดอายุ ลองคัดลอกลิงก์ใหม่จากแอป"
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


# ---------- Dual subtitles ----------
# Page language -> YouTube subtitle codes to look for (first match wins).
SUB_LANGS = {
    "en": ("en", "en-US", "en-GB"),
    "th": ("th",),
    "ko": ("ko",),
    "zh": ("zh-Hans", "zh-CN", "zh", "zh-SG", "zh-Hant", "zh-TW", "zh-HK"),
    "ja": ("ja",),
}
GT_CODE = {"en": "en", "th": "th", "ko": "ko", "zh": "zh-CN", "ja": "ja"}
subs_cache = {}  # (video_id, lang) -> track


def lang_of(code):
    """'zh-Hans' -> 'zh', 'en-US' -> 'en', 'en-orig' -> 'en'."""
    return code.split("-")[0].lower()


def match_track(tracks, lang):
    for code in SUB_LANGS[lang]:
        if tracks.get(code):
            return code
    for code in tracks:
        if lang_of(code) == lang and not code.endswith("-orig") and tracks[code]:
            return code
    return None


def parse_json3(raw):
    data = json.loads(raw)
    cues = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs or ev.get("aAppend"):
            continue
        text = re.sub(r"\s+", " ", "".join(s.get("utf8", "") for s in segs)).strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0)
        cues.append([start, start + ev.get("dDurationMs", 2000), text])
    cues.sort(key=lambda c: c[0])
    # Auto captions roll two lines at once; cut each line off when the next one starts.
    for a, b in zip(cues, cues[1:]):
        if a[0] < b[0] < a[1]:
            a[1] = b[0]
    return cues


def fetch_track(ydl, formats):
    fmt = next((f for f in formats if f.get("ext") == "json3"), None)
    if not fmt:
        raise RuntimeError("no json3 subtitles")
    return parse_json3(ydl.urlopen(fmt["url"]).read())


def translate_lines(ydl, lines, lang):
    """Free Google Translate endpoint. Lines are sent in batches, one per row."""
    from concurrent.futures import ThreadPoolExecutor
    from yt_dlp.networking import Request
    from urllib.parse import urlencode

    def call(text):
        body = urlencode({"client": "gtx", "sl": "auto", "tl": GT_CODE[lang], "dt": "t", "q": text}).encode()
        req = Request("https://translate.googleapis.com/translate_a/single", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        res = json.loads(ydl.urlopen(req).read())
        return "".join(part[0] for part in res[0] if part and part[0])

    def batch(chunk):
        out = call("\n".join(chunk)).split("\n")
        if len(out) == len(chunk):
            return [s.strip() for s in out]
        return [call(line).strip() for line in chunk]  # rows got merged: translate one by one

    chunks, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > 3500:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    with ThreadPoolExecutor(4) as pool:
        return [t for part in pool.map(batch, chunks) for t in part]


def get_dual_subs(url, langs):
    with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        vid = info["id"]
        manual = info.get("subtitles") or {}
        manual.pop("live_chat", None)
        auto = info.get("automatic_captions") or {}
        # YouTube's own auto-translated tracks are rate-limited (HTTP 429); only the
        # speech-recognition track in the video's spoken language ("xx-orig") is reliable.
        asr_code = next((c for c in auto if c.endswith("-orig")), None)
        if not asr_code and info.get("language") in auto:
            asr_code = info["language"]
        orig_lang = lang_of(info.get("language") or (asr_code or "x"))

        loaded = {}

        def load(kind, code):
            if (kind, code) not in loaded:
                loaded[(kind, code)] = fetch_track(ydl, (manual if kind == "manual" else auto)[code])
            return loaded[(kind, code)]

        def own_track(lang):
            code = match_track(manual, lang)
            if code:
                return "manual", code
            if asr_code and lang_of(asr_code) == lang:
                return "auto", asr_code
            return None

        # What to translate from when a language has no subtitles of its own.
        base = None
        for lang in [*langs, orig_lang]:
            if lang in SUB_LANGS and own_track(lang):
                base = own_track(lang)
                break
        if not base and asr_code:
            base = ("auto", asr_code)
        if not base and manual:
            base = ("manual", next(iter(manual)))
        if not base:
            raise RuntimeError("คลิปนี้ไม่มีซับไตเติลหรือคำบรรยายเลย เลยแปลไม่ได้")

        tracks = []
        for lang in langs:
            key = (vid, lang)
            if key not in subs_cache:
                own = own_track(lang)
                if own:
                    cues = load(*own)
                    source = "manual" if own[0] == "manual" else "auto"
                else:
                    base_cues = load(*base)
                    texts = translate_lines(ydl, [c[2] for c in base_cues], lang)
                    cues = [[c[0], c[1], t] for c, t in zip(base_cues, texts)]
                    source = "translated"
                subs_cache[key] = {"lang": lang, "source": source, "cues": cues}
            tracks.append(subs_cache[key])

    return {
        "id": vid,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "embeddable": info.get("playable_in_embed") is not False,
        "tracks": tracks,
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

    def _is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1") or self.client_address[0] == LAN_IP

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
        if path == "/subtitles.html":
            return self._send_file(BASE.parent / "subtitles.html", "text/html; charset=utf-8")
        # App icon + manifest, so "Add to Home Screen" on a phone gets the MyFiR icon.
        if path == "/phone-connect.js":
            return self._send_file(BASE.parent / "phone-connect.js", "text/javascript; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self._send_file(BASE.parent / "manifest.webmanifest", "application/manifest+json")
        m = re.fullmatch(r"/icons/([\w-]+\.(png|svg))", path)
        if m:
            ctype = "image/svg+xml" if m.group(2) == "svg" else "image/png"
            return self._send_file(BASE.parent / "icons" / m.group(1), ctype)
        if path == "/api/health":
            return self._json({"ok": True, "ffmpeg": bool(FFMPEG), "yt_dlp": yt_dlp.version.__version__,
                               "local": self._is_local(), "lan_url": LAN_URL})
        if path == "/api/qr.svg":
            if not LAN_URL:
                return self._json({"error": "no network"}, 404)
            import segno
            buf = io.BytesIO()
            page = "/subtitles.html" if "p=subtitles" in (urlparse(self.path).query or "") else ""
            segno.make(LAN_URL + page, error="m").save(buf, kind="svg", scale=6, border=2,
                                                 dark="#111", light="#fff", xmldecl=False)
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return None
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
        if path == "/api/jobs":
            # Lets the page restore its list after a reload.
            with jobs_lock:
                items = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
                return self._json([{k: v for k, v in j.items() if k != "file"} for j in items])
        if path == "/api/open-folder":
            if sys.platform == "win32" and self._is_local():
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
        if path == "/api/subs":
            langs = [l for l in (data.get("langs") or []) if l in SUB_LANGS][:2]
            if platform_of(url) != "youtube":
                return self._json({"error": "ซับสองภาษาใช้ได้กับลิงก์ YouTube เท่านั้น"}, 400)
            if not langs:
                return self._json({"error": "เลือกภาษาก่อน"}, 400)
            try:
                return self._json(get_dual_subs(url, langs))
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "429" in msg or "too many" in msg.lower():
                    msg = "YouTube หรือระบบแปลภาษาขอให้รอสักพัก ลองใหม่ในอีก 1–2 นาที"
                elif not re.search(r"[฀-๿]", msg):
                    msg = friendly_error(exc)
                return self._json({"error": msg}, 400)
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
                jobs[job_id] = {"id": job_id, "status": "queued", "percent": 0, "url": url,
                                "title": str(data.get("title") or url)[:300],
                                "format": fmt, "quality": quality, "created": time.time()}
            threading.Thread(target=run_job, args=(job_id, url, fmt, quality), daemon=True).start()
            return self._json({"id": job_id})
        if path == "/api/clear":
            with jobs_lock:
                for k in [k for k, j in jobs.items() if j["status"] in ("done", "error")]:
                    del jobs[k]
            return self._json({"ok": True})
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


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second copy bind the same port; we want it to fail instead.
    allow_reuse_address = sys.platform != "win32"


def main():
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        print("Mygames converter is already running in another window.")
        sys.exit(3)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Mygames converter running at {url}")
    if LAN_URL:
        print(f"On your phone (same Wi-Fi): {LAN_URL}  - or scan the QR code on the page")
        print("If Windows Firewall asks, click Allow so your phone can connect.")
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
