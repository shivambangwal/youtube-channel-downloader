#!/usr/bin/env python3
"""Local YouTube channel downloader UI (for content you are authorized to save)."""

from __future__ import annotations

import json
import importlib.util
import base64
import hmac
import html
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
RUNTIME_DIR = BASE_DIR / ".runtime"
HOST = os.environ.get("YT_DOWNLOADER_HOST", "127.0.0.1")
PORT = int(os.environ.get("YT_DOWNLOADER_PORT", "8765"))
MAX_PREVIEW_ITEMS = 200
AUTH_USERNAME = os.environ.get("YT_DOWNLOADER_USERNAME", "")
AUTH_PASSWORD = os.environ.get("YT_DOWNLOADER_PASSWORD", "")
PUBLIC_SITE_URL = os.environ.get("YT_DOWNLOADER_PUBLIC_URL", "").strip().rstrip("/")
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("YT_DOWNLOADER_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
ACTIVE_PROCESSES: set[subprocess.Popen] = set()
ACTIVE_PROCESSES_LOCK = threading.RLock()
STREAM_LOCK = threading.Lock()
STREAM_PROGRESS_LOCK = threading.RLock()
STREAM_PROGRESS: dict[str, object] = {"job_id": "", "status": "idle", "percent": 0}
CROP_FILTERS = {
    "4:5": r"crop=trunc(if(gte(iw/ih\,0.8)\,ih*0.8\,iw)/2)*2:trunc(if(gte(iw/ih\,0.8)\,ih\,iw/0.8)/2)*2",
    "1:1": r"crop=trunc(if(gte(iw\,ih)\,ih\,iw)/2)*2:trunc(if(gte(iw\,ih)\,ih\,iw)/2)*2",
    "5:4": r"crop=trunc(if(gte(iw/ih\,1.25)\,ih*1.25\,iw)/2)*2:trunc(if(gte(iw/ih\,1.25)\,ih\,iw/1.25)/2)*2",
}

# Keep third-party tools and packages inside this app folder.
sys.path.insert(0, str(RUNTIME_DIR))


def ffmpeg_executable() -> str | None:
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        return path if Path(path).is_file() else None
    except (ImportError, OSError, RuntimeError):
        return shutil.which("ffmpeg")


def deno_executable() -> str | None:
    try:
        import deno
        path = deno.find_deno_bin()
        return path if path and Path(path).is_file() else None
    except (ImportError, OSError, RuntimeError):
        return shutil.which("deno")


def bundled_tool_status() -> dict[str, bool]:
    return {
        "yt_dlp": importlib.util.find_spec("yt_dlp") is not None,
        "ffmpeg": ffmpeg_executable() is not None,
        "deno": deno_executable() is not None,
    }


def bundled_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    python_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(RUNTIME_DIR) + (os.pathsep + python_path if python_path else "")
    binary_dirs = [str(RUNTIME_DIR / "bin")]
    for executable in (deno_executable(), ffmpeg_executable()):
        if executable:
            binary_dirs.append(str(Path(executable).parent))
    env["PATH"] = os.pathsep.join(binary_dirs + [env.get("PATH", "")])
    return env


def yt_dlp_command() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def ensure_bundled_tools() -> None:
    status = bundled_tool_status()
    if all(status.values()):
        return
    if sys.version_info < (3, 11):
        raise RuntimeError("This bundled runtime requires Python 3.11 or newer.")
    print("Installing the app-local yt-dlp, Deno, and FFmpeg tools (first run only)…")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade",
        "--target", str(RUNTIME_DIR), "yt-dlp[default,deno]", "imageio-ffmpeg",
    ])
    importlib.invalidate_caches()
    status = bundled_tool_status()
    if result.returncode != 0 or not all(status.values()):
        missing = ", ".join(name for name, ready in status.items() if not ready)
        raise RuntimeError(f"Could not install the app-local tools ({missing or 'pip failed'}). Check your internet connection and try again.")


def resolve_youtube_selection(raw_url: str, media_type: str) -> tuple[str, str, bool, str, bool]:
    """Resolve a channel tab, Shorts hashtag page, or one YouTube video URL."""
    if media_type not in {"videos", "shorts"}:
        raise ValueError("Choose either Videos or Shorts.")
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("Paste a YouTube channel, video, or Shorts link first.")
    candidate = raw_url if "://" in raw_url else "https://" + raw_url
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        raise ValueError("Use a YouTube channel, video, or Shorts link.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 3 and parts[0].lower() == "hashtag" and parts[2].lower() == "shorts":
        hashtag = unquote(parts[1]).lstrip("#")
        if not hashtag or len(hashtag) > 100 or any(ch.isspace() for ch in hashtag):
            raise ValueError("That Shorts hashtag link is not valid.")
        safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", hashtag).strip("._-") or "hashtag"
        hashtag_url = f"https://www.youtube.com/hashtag/{quote(hashtag, safe='')}/shorts"
        return hashtag_url, f"hashtag-{safe_tag}", False, "shorts", True
    video_id = ""
    is_short = False
    is_single_url = False
    if host in {"youtu.be", "www.youtu.be"}:
        is_single_url = True
        video_id = parts[0] if parts else ""
    elif parts and parts[0] == "watch":
        is_single_url = True
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    elif len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
        is_single_url = True
        is_short = parts[0] == "shorts"
        video_id = parts[1]
    if is_single_url:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("That YouTube video link does not contain a valid video ID.")
        resolved_type = "shorts" if is_short else "videos"
        label = f"{'short' if is_short else 'video'}-{video_id}"
        return f"https://www.youtube.com/watch?v={video_id}", label, True, resolved_type, False
    if not parts:
        raise ValueError("That link does not point to a channel.")
    if parts[0].startswith("@"):
        root = "/" + parts[0]
        label = parts[0]
    elif len(parts) >= 2 and parts[0] in {"channel", "c", "user"}:
        root = "/" + "/".join(parts[:2])
        label = parts[1]
    else:
        raise ValueError("Use a channel link like youtube.com/@name, or paste one video or Shorts link.")
    label = re.sub(r"[^A-Za-z0-9._@-]+", "_", label).strip("._") or "channel"
    return f"https://www.youtube.com{root}/{media_type}", label, False, media_type, False


def crop_filter_for(crop_ratio: str) -> str | None:
    if crop_ratio == "original":
        return None
    if not isinstance(crop_ratio, str) or crop_ratio not in CROP_FILTERS:
        raise ValueError("Choose Original, 4:5, 1:1, or 5:4 framing.")
    return CROP_FILTERS[crop_ratio]


def parse_trim_timestamp(value: str) -> int:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid timestamp: {value}")
    numbers = [int(part) for part in parts]
    if (len(numbers) == 1 and numbers[0] > 59) or (len(numbers) == 2 and numbers[1] > 59) or (len(numbers) == 3 and (numbers[1] > 59 or numbers[2] > 59)):
        raise ValueError(f"Invalid timestamp: {value}")
    total = numbers[0] if len(numbers) == 1 else numbers[0] * 60 + numbers[1] if len(numbers) == 2 else numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if total > 172800:
        raise ValueError("Trim timestamps must be within 48 hours.")
    return total


def format_trim_timestamp(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_trim_ranges(raw_text: str) -> list[tuple[int, int]]:
    ranges = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9:]+)\s*-\s*([0-9:]+)(?:\s*\|.*)?", line)
        if not match:
            raise ValueError("Enter one timestamp range per line, such as 0:00 - 1:06 | Intro.")
        start, end = parse_trim_timestamp(match.group(1)), parse_trim_timestamp(match.group(2))
        if end <= start:
            raise ValueError("Each trim range must end after it starts.")
        ranges.append((start, end))
        if len(ranges) > 100:
            raise ValueError("Choose no more than 100 trim ranges at a time.")
    if not ranges:
        raise ValueError("Enter at least one timestamp range.")
    if any(ranges[index][0] < ranges[index - 1][1] for index in range(1, len(ranges))):
        raise ValueError("Timestamp ranges must be listed in order without overlapping.")
    return ranges


def validate_public_site_url(value: str) -> str:
    """Require a canonical HTTPS origin without a path, query, or credentials."""
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise SystemExit("YT_DOWNLOADER_PUBLIC_URL must be an HTTPS origin, such as https://downloads.example.com.")
    return value


def stream_downloader_args(crop_filter: str | None) -> str:
    args = ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov"]
    if crop_filter:
        args.extend([
            # Preserve the CRF quality target; ultrafast trades smaller files for speed.
            "-c:v", "libx264", "-crf", "18", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "192k", "-vf", crop_filter,
        ])
    return "ffmpeg_o:" + shlex.join(args)


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def update_stream_progress(job_id: str, **updates: object) -> None:
    with STREAM_PROGRESS_LOCK:
        if STREAM_PROGRESS.get("job_id") != job_id:
            STREAM_PROGRESS.clear()
            STREAM_PROGRESS["job_id"] = job_id
        STREAM_PROGRESS.update(updates)


def stop_process_tree(process: subprocess.Popen) -> None:
    """Stop yt-dlp and any ffmpeg child in its isolated process group."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            if process.poll() is not None:
                return
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    else:
        if os.name == "posix":
            try:
                os.killpg(process.pid, 0)
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalChannelDownloader/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print("[server] " + fmt % args)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length).decode("utf-8")
        values = parse_qs(raw, keep_blank_values=True, max_num_fields=8)
        return {key: items[0] for key, items in values.items() if items}

    def has_local_origin(self) -> bool:
        """Allow local requests or the exact HTTPS origin configured for hosting."""
        origin = self.headers.get("Origin")
        if not origin:  # CLI requests have no Origin header.
            return True
        if origin.rstrip("/") in ALLOWED_ORIGINS:
            return True
        parsed_origin = urlsplit(origin)
        request_host = urlsplit("//" + self.headers.get("Host", ""))
        allowed_hosts = {"127.0.0.1", "localhost"}
        return (
            parsed_origin.scheme == "http"
            and parsed_origin.hostname in allowed_hosts
            and parsed_origin.hostname == request_host.hostname
            and parsed_origin.port == PORT
        )

    def is_authorized(self) -> bool:
        if not AUTH_USERNAME and not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return False
        try:
            supplied = base64.b64decode(token, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied, f"{AUTH_USERNAME}:{AUTH_PASSWORD}")

    def require_authentication(self) -> bool:
        if self.is_authorized():
            return True
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Channel Downloader", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            if path == "/index.html":
                self.send_response(301)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                return
            self.send_landing_page()
            return
        if path == "/robots.txt":
            self.send_robots_txt()
            return
        if path == "/sitemap.xml":
            self.send_sitemap()
            return
        if not self.require_authentication():
            return
        if path == "/app/api/status":
            status = bundled_tool_status()
            json_response(self, status)
            return
        if path == "/app/api/download-progress":
            requested_id = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
            with STREAM_PROGRESS_LOCK:
                progress = dict(STREAM_PROGRESS) if STREAM_PROGRESS.get("job_id") == requested_id else {}
            if not progress:
                progress = {
                    "job_id": requested_id,
                    "status": "idle",
                    "percent": 0,
                    "current_index": 0,
                    "total": 0,
                    "current": "",
                    "message": "Waiting for the download to start…",
                }
            json_response(self, progress)
            return
        if path in {"/app", "/app/", "/app/index.html"}:
            if path == "/app":
                self.send_response(301)
                self.send_header("Location", "/app/")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self.send_file(
                WEB_DIR / "index.html", "text/html; charset=utf-8",
                extra_headers={"X-Robots-Tag": "noindex, nofollow"},
            )
            return
        self.send_error(404)

    def send_landing_page(self) -> None:
        source = (WEB_DIR / "landing.html").read_text(encoding="utf-8")
        if PUBLIC_SITE_URL:
            safe_url = html.escape(PUBLIC_SITE_URL, quote=True)
            source = source.replace("{{PUBLIC_SITE_URL}}", safe_url)
            schema = json.dumps({
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": "Channel Downloader",
                "url": PUBLIC_SITE_URL + "/",
                "description": "A browser based YouTube channel and Shorts downloader for videos you own or are authorized to save.",
            }, ensure_ascii=False).replace("</", "<\\/")
            source = source.replace("<!-- SITE STRUCTURED DATA -->", f'<script type="application/ld+json">{schema}</script>')
        else:
            source = source.replace('<link rel="canonical" href="{{PUBLIC_SITE_URL}}/">', "")
            source = source.replace('<meta property="og:url" content="{{PUBLIC_SITE_URL}}/">', "")
            source = source.replace("<!-- SITE STRUCTURED DATA -->", "")
        self.send_bytes(source.encode("utf-8"), "text/html; charset=utf-8", cache_control="public, max-age=300")

    def send_robots_txt(self) -> None:
        lines = ["User-agent: *", "Allow: /", "Disallow: /app/api/"]
        if PUBLIC_SITE_URL:
            lines.append(f"Sitemap: {PUBLIC_SITE_URL}/sitemap.xml")
        self.send_bytes(("\n".join(lines) + "\n").encode("utf-8"), "text/plain; charset=utf-8", cache_control="public, max-age=3600")

    def send_sitemap(self) -> None:
        if not PUBLIC_SITE_URL:
            self.send_error(404, "Set YT_DOWNLOADER_PUBLIC_URL to enable the sitemap.")
            return
        loc = xml_escape(PUBLIC_SITE_URL + "/")
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{loc}</loc></url></urlset>\n"
        )
        self.send_bytes(body.encode("utf-8"), "application/xml; charset=utf-8", cache_control="public, max-age=3600")

    def send_bytes(self, body: bytes, content_type: str, cache_control: str = "no-store", extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, file_path: Path, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        body = file_path.read_bytes()
        self.send_bytes(body, content_type, extra_headers=extra_headers)

    def send_stream_error(self, message: str, status: int = 400, progress_job_id: str = "") -> None:
        if progress_job_id:
            update_stream_progress(
                progress_job_id,
                status="error",
                message=message,
            )
        origin = self.headers.get("Origin") or "*"
        body = (
            "<!doctype html><meta charset=\"utf-8\"><script>"
            "parent.postMessage({type:'channel-downloader-error',message:"
            + json.dumps(message)
            + "},"
            + json.dumps(origin)
            + ");</script>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream_channel_zip(self) -> None:
        if not STREAM_LOCK.acquire(blocking=False):
            self.send_stream_error("A channel download is already running. Wait for it to finish before starting another.", 429)
            return
        try:
            self._stream_channel_zip()
        finally:
            STREAM_LOCK.release()

    def _stream_channel_zip(self) -> None:
        data = self.read_form()
        job_id = data.get("job_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            job_id = uuid.uuid4().hex
        update_stream_progress(
            job_id, status="preparing", current_index=0, total=0, percent=0,
            current="", message="Reading the selected YouTube link…",
        )
        try:
            tab_url, selection_label, is_single, media_type, is_hashtag = resolve_youtube_selection(
                data.get("url", ""), data.get("media_type", "videos"),
            )
        except ValueError as exc:
            self.send_stream_error(str(exc), 400, job_id)
            return
        crop_ratio = data.get("crop_ratio", "original")
        try:
            crop_filter = crop_filter_for(crop_ratio)
        except ValueError as exc:
            self.send_stream_error(str(exc), 400, job_id)
            return
        tools = bundled_tool_status()
        if not tools["yt_dlp"] or not tools["deno"] or not tools["ffmpeg"]:
            self.send_stream_error("yt-dlp, Deno, or FFmpeg is unavailable on the server.", 503, job_id)
            return
        update_stream_progress(job_id, status="preparing", message="Loading the selected videos…")
        listing_command = [*yt_dlp_command(), "--dump-single-json", "--no-warnings"]
        if is_single:
            listing_command.append("--no-playlist")
        elif is_hashtag:
            listing_command.append("--flat-playlist")
        else:
            listing_command.extend(["--flat-playlist", "--yes-playlist"])
        listing_command.append(tab_url)
        try:
            result = subprocess.run(
                listing_command, capture_output=True, text=True, timeout=600,
                env=bundled_runtime_env(),
            )
        except subprocess.TimeoutExpired:
            self.send_stream_error("The channel took too long to load. Try again.", 504, job_id)
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Could not read this channel.").strip()
            self.send_stream_error(detail[-1200:], 422, job_id)
            return
        try:
            listing = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.send_stream_error("yt-dlp returned an unreadable channel listing.", 502, job_id)
            return
        raw_entries = [listing] if is_single and listing.get("id") else (listing.get("entries") or [])
        entries = [entry for entry in raw_entries if entry and entry.get("id")]
        trim_mode = data.get("trim_mode", "none")
        if trim_mode not in {"none", "chapters", "timestamps"}:
            self.send_stream_error("Choose chapters or timestamp ranges for trimming.", 400, job_id)
            return
        trim_ranges: list[tuple[int, int, str]] = []
        if trim_mode != "none":
            if not is_single or not entries:
                self.send_stream_error("Trim mode works with one video or Shorts link at a time.", 400, job_id)
                return
            if trim_mode == "chapters":
                chapters = listing.get("chapters") or []
                for chapter_index, chapter in enumerate(chapters):
                    try:
                        start = int(float(chapter.get("start_time", -1)))
                        next_start = chapters[chapter_index + 1].get("start_time") if chapter_index + 1 < len(chapters) else listing.get("duration")
                        end = int(float(chapter.get("end_time") if chapter.get("end_time") is not None else next_start if next_start is not None else -1))
                    except (TypeError, ValueError):
                        continue
                    if start >= 0 and end > start:
                        trim_ranges.append((start, end, str(chapter.get("title") or f"Chapter {len(trim_ranges) + 1}")))
                if not trim_ranges:
                    self.send_stream_error("This video has no usable chapters to split.", 422, job_id)
                    return
            else:
                try:
                    trim_ranges = [
                        (start, end, f"{format_trim_timestamp(start)}-{format_trim_timestamp(end)}")
                        for start, end in parse_trim_ranges(data.get("trim_timestamps", ""))
                    ]
                except ValueError as exc:
                    self.send_stream_error(str(exc), 400, job_id)
                    return
        if is_hashtag:
            ranked_entries = []
            seen_ids: set[str] = set()
            for entry in entries:
                video_id = str(entry.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or video_id in seen_ids:
                    continue
                seen_ids.add(video_id)
                try:
                    view_count = max(0, int(entry.get("view_count") or 0))
                except (TypeError, ValueError):
                    view_count = 0
                ranked_entries.append({**entry, "id": video_id, "view_count": view_count})
            entries = sorted(ranked_entries, key=lambda entry: entry["view_count"], reverse=True)
        if not entries:
            self.send_stream_error(f"No downloadable {media_type} were found for this selection.", 404, job_id)
            return
        download_items = (
            [{"entry": entries[0], "section": section} for section in trim_ranges]
            if trim_ranges else [{"entry": entry, "section": None} for entry in entries]
        )
        update_stream_progress(
            job_id, status="downloading", current_index=0, total=len(download_items), percent=0,
            current="", message=f"Found {len(download_items)} items. Preparing the first video…",
        )

        archive_label = (listing.get("title") or selection_label) if is_single else selection_label
        archive_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(archive_label)).strip("._") or "video"
        crop_suffix = {"4:5": "-crop-4x5", "1:1": "-crop-1x1", "5:4": "-crop-5x4"}.get(crop_ratio, "")
        filename = f"{archive_name}{'' if is_single else '-' + media_type}{crop_suffix}.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        failures: list[str] = []
        seen_ids: set[str] = set()
        try:
            with zipfile.ZipFile(self.wfile, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                readme_text = (
                    f"MP4 videos are streamed through the server and packaged for your device. Framing: {crop_ratio}. "
                    + ("Hashtag Shorts are ordered from highest to lowest view count. " if is_hashtag else "")
                    + "The server does not keep a copy of this ZIP or the video files.\n"
                )
                archive.writestr("README.txt", readme_text)
                self.wfile.flush()
                for index, item in enumerate(download_items, start=1):
                    entry = item["entry"]
                    section = item["section"]
                    video_id = str(entry.get("id") or "")
                    if not video_id or (not section and video_id in seen_ids):
                        continue
                    if not section:
                        seen_ids.add(video_id)
                    title = str(entry.get("title") or f"video_{index}")
                    part_title = str(section[2]) if section else ""
                    update_stream_progress(
                        job_id, status="downloading", current_index=index, total=len(download_items),
                        percent=0, current=f"{title} — {part_title}" if part_title else title,
                        message=f"Part {index} of {len(download_items)}" if section else f"Video {index} of {len(download_items)}",
                    )
                    safe_title = re.sub(r"[\\/\x00-\x1f:]+", "_", title).strip(" .")[:180] or f"video_{index}"
                    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", video_id) or str(index)
                    framing_suffix = {"4:5": " [4x5 crop]", "1:1": " [1x1 crop]", "5:4": " [5x4 crop]"}.get(crop_ratio, "")
                    safe_part = re.sub(r"[\\/\x00-\x1f:]+", "_", part_title).strip(" .")[:100]
                    trim_suffix = f" - {index:02d} {safe_part}" if section else ""
                    member_name = f"{safe_title} [{safe_id}]{trim_suffix}{framing_suffix}.mp4"
                    video_url = f"https://www.youtube.com/watch?v={quote(video_id)}"
                    command = [
                        *yt_dlp_command(), "--no-playlist", "--no-warnings", "--newline",
                        "--format", "bv*+ba/b", "--merge-output-format", "mp4",
                        "--ffmpeg-location", ffmpeg_executable() or "ffmpeg",
                        "--downloader", "ffmpeg",
                        "--downloader-args", stream_downloader_args(crop_filter),
                        "--output", "-", video_url,
                    ]
                    if section:
                        command[command.index(video_url):command.index(video_url)] = [
                            "--download-sections", f"*{format_trim_timestamp(section[0])}-{format_trim_timestamp(section[1])}",
                        ]
                    stderr_tail: list[str] = []
                    process = None
                    try:
                        with archive.open(member_name, mode="w", force_zip64=True) as member:
                            process = subprocess.Popen(
                                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=(os.name == "posix"), env=bundled_runtime_env(),
                            )
                            with ACTIVE_PROCESSES_LOCK:
                                ACTIVE_PROCESSES.add(process)
                            assert process.stdout is not None and process.stderr is not None

                            def drain_stderr(pipe, captured: list[str]) -> None:
                                for raw_line in iter(pipe.readline, b""):
                                    line = raw_line.decode("utf-8", errors="replace").strip()
                                    if line:
                                        captured.append(line)
                                        del captured[:-20]
                                        match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
                                        if match:
                                            update_stream_progress(
                                                job_id,
                                                percent=min(100, float(match.group(1))),
                                                current_index=index,
                                                total=len(download_items),
                                                current=title,
                                                message=f"Part {index} of {len(download_items)}" if section else f"Video {index} of {len(download_items)}",
                                            )

                            stderr_thread = threading.Thread(
                                target=drain_stderr, args=(process.stderr, stderr_tail), daemon=True,
                            )
                            stderr_thread.start()
                            for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                                member.write(block)
                                self.wfile.flush()
                            return_code = process.wait()
                            stderr_thread.join(timeout=5)
                        if return_code != 0:
                            failures.append(f"{title} [{video_id}]: {' '.join(stderr_tail)[-500:] or 'download failed'}")
                            update_stream_progress(
                                job_id, percent=100, current_index=index, total=len(download_items),
                                current=title, message=f"Part {index} failed; continuing." if section else f"Video {index} failed; continuing to the next item.",
                            )
                        else:
                            update_stream_progress(
                                job_id, percent=100, current_index=index, total=len(download_items),
                                current=title, message=f"Part {index} of {len(download_items)} finished." if section else f"Video {index} of {len(download_items)} finished.",
                            )
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        if process and process.poll() is None:
                            stop_process_tree(process)
                        raise
                    except Exception as exc:
                        if process and process.poll() is None:
                            stop_process_tree(process)
                        failures.append(f"{title} [{video_id}]: {exc}")
                        update_stream_progress(
                            job_id, percent=100, current_index=index, total=len(download_items),
                            current=title, message=f"Video {index} failed; continuing to the next item.",
                        )
                    finally:
                        if process:
                            with ACTIVE_PROCESSES_LOCK:
                                ACTIVE_PROCESSES.discard(process)
                if failures:
                    archive.writestr(
                        "DOWNLOAD-REPORT.txt",
                        "Some items did not complete successfully. Remove any partial file with the same name.\n\n"
                        + "\n".join(failures),
                    )
            update_stream_progress(
                job_id,
                status="complete_with_errors" if failures else "complete",
                current_index=len(download_items),
                total=len(download_items),
                percent=100,
                current="Finished",
                message=(
                    f"ZIP sent to your device. {len(failures)} item(s) had errors; see DOWNLOAD-REPORT.txt."
                    if failures else "ZIP sent to your device."
                ),
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            print("[stream] Browser disconnected; ending the ZIP stream.")
            update_stream_progress(
                job_id, status="cancelled", message="The browser stopped or disconnected from the download.",
            )
        except Exception as exc:
            print(f"[stream] ZIP stream failed: {exc}")
            update_stream_progress(job_id, status="error", message="The ZIP transfer could not be completed.")

    def do_POST(self) -> None:  # noqa: N802
        if not self.require_authentication():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/app/api/download-stream":
                if not self.headers.get("Origin") or not self.has_local_origin():
                    self.send_stream_error("The download request origin is not allowed.", 403)
                    return
                try:
                    self.stream_channel_zip()
                except ValueError as exc:
                    self.send_stream_error(str(exc), 400)
                return
            if not self.has_local_origin():
                json_response(self, {"error": "Request origin is not allowed."}, 403)
                return
            if path == "/app/api/preview":
                if not bundled_tool_status()["yt_dlp"]:
                    json_response(self, {"error": "The app-local yt-dlp tool is not available. Restart the app to install it."}, 503)
                    return
                data = self.read_json()
                tab_url, selection_label, is_single, resolved_media_type, is_hashtag = resolve_youtube_selection(
                    data.get("url", ""), data.get("media_type", "videos"),
                )
                command = [*yt_dlp_command(), "--dump-single-json", "--no-warnings"]
                if is_single:
                    command.append("--no-playlist")
                elif is_hashtag:
                    command.append("--flat-playlist")
                else:
                    command.extend(["--flat-playlist", "--playlist-end", str(MAX_PREVIEW_ITEMS)])
                command.append(tab_url)
                result = subprocess.run(command, capture_output=True, text=True, timeout=300 if is_hashtag else 120, env=bundled_runtime_env())
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "Could not read this channel.").strip()
                    json_response(self, {"error": detail[-1200:]}, 422)
                    return
                try:
                    listing = json.loads(result.stdout)
                except json.JSONDecodeError:
                    json_response(self, {"error": "yt-dlp returned an unreadable channel listing."}, 502)
                    return
                entries = [listing] if is_single and listing.get("id") else (listing.get("entries") or [])
                if is_hashtag:
                    seen_ids: set[str] = set()
                    ranked_entries = []
                    for entry in entries:
                        if not entry:
                            continue
                        video_id = str(entry.get("id") or "")
                        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or video_id in seen_ids:
                            continue
                        seen_ids.add(video_id)
                        try:
                            view_count = max(0, int(entry.get("view_count") or 0))
                        except (TypeError, ValueError):
                            view_count = 0
                        ranked_entries.append({**entry, "id": video_id, "view_count": view_count})
                    entries = sorted(ranked_entries, key=lambda entry: entry["view_count"], reverse=True)
                preview_entries = entries if is_single else entries[:MAX_PREVIEW_ITEMS]
                videos = []
                for entry in preview_entries:
                    if not entry:
                        continue
                    video_id = entry.get("id") or ""
                    thumbnails = entry.get("thumbnails") or []
                    thumbnail = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else "")
                    if not thumbnail and video_id:
                        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    videos.append({
                        "id": video_id,
                        "title": entry.get("title") or "Untitled video",
                        "url": entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else ""),
                        "thumbnail": thumbnail,
                        "duration": entry.get("duration"),
                        "upload_date": entry.get("upload_date"),
                        "view_count": entry.get("view_count"),
                    })
                json_response(self, {
                    "channel": (listing.get("title") if is_single else listing.get("channel")) or listing.get("uploader") or selection_label,
                    "media_type": resolved_media_type,
                    "single": is_single,
                    "duration": listing.get("duration") if is_single else None,
                    "chapters": listing.get("chapters") if is_single else [],
                    "sorted_by_views": is_hashtag,
                    "count": len(videos),
                    "total_count": len(entries) if is_hashtag else listing.get("playlist_count"),
                    "truncated": (
                        len(entries) > MAX_PREVIEW_ITEMS if is_hashtag else
                        not is_single and len(videos) >= MAX_PREVIEW_ITEMS and (not listing.get("playlist_count") or listing.get("playlist_count") > len(videos))
                    ),
                    "videos": videos,
                })
                return
            json_response(self, {"error": "Not found."}, 404)
        except ValueError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except subprocess.TimeoutExpired:
            json_response(self, {"error": "The channel took too long to respond. Try again."}, 504)
        except (json.JSONDecodeError, UnicodeDecodeError):
            json_response(self, {"error": "Invalid request."}, 400)
        except Exception as exc:
            json_response(self, {"error": f"Request failed: {exc}"}, 500)


def main() -> None:
    global PUBLIC_SITE_URL
    PUBLIC_SITE_URL = validate_public_site_url(PUBLIC_SITE_URL)
    if bool(AUTH_USERNAME) != bool(AUTH_PASSWORD):
        raise SystemExit("Set both YT_DOWNLOADER_USERNAME and YT_DOWNLOADER_PASSWORD, or leave both unset.")
    if HOST not in {"127.0.0.1", "localhost", "::1"} and not AUTH_USERNAME:
        raise SystemExit("Refusing to listen on a public interface without HTTP Basic authentication.")
    if HOST not in {"127.0.0.1", "localhost", "::1"} and len(AUTH_PASSWORD) < 20:
        raise SystemExit("For a hosted install, YT_DOWNLOADER_PASSWORD must be at least 20 characters long.")
    try:
        ensure_bundled_tools()
    except RuntimeError as exc:
        raise SystemExit(f"Startup failed: {exc}") from exc
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"YouTube Channel Downloader is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server…")
        with ACTIVE_PROCESSES_LOCK:
            active_processes = list(ACTIVE_PROCESSES)
        for process in active_processes:
            stop_process_tree(process)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
