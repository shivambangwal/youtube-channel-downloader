#!/usr/bin/env python3
"""Local YouTube channel downloader UI (for content you are authorized to save)."""

from __future__ import annotations

import json
import importlib.util
import base64
import hmac
import mimetypes
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DOWNLOADS_DIR = Path(os.environ.get("YT_DOWNLOADER_DOWNLOAD_DIR", str(Path.home() / "Downloads"))).expanduser()
RUNTIME_DIR = BASE_DIR / ".runtime"
HOST = os.environ.get("YT_DOWNLOADER_HOST", "127.0.0.1")
PORT = int(os.environ.get("YT_DOWNLOADER_PORT", "8765"))
MAX_PREVIEW_ITEMS = 200
AUTH_USERNAME = os.environ.get("YT_DOWNLOADER_USERNAME", "")
AUTH_PASSWORD = os.environ.get("YT_DOWNLOADER_PASSWORD", "")
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("YT_DOWNLOADER_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
CROP_FILTERS = {
    "4:5": r"crop=trunc(if(gte(iw/ih\,0.8)\,ih*0.8\,iw)/2)*2:trunc(if(gte(iw/ih\,0.8)\,ih\,iw/0.8)/2)*2",
    "1:1": r"crop=trunc(if(gte(iw\,ih)\,ih\,iw)/2)*2:trunc(if(gte(iw\,ih)\,ih\,iw)/2)*2",
    "5:4": r"crop=trunc(if(gte(iw/ih\,1.25)\,ih*1.25\,iw)/2)*2:trunc(if(gte(iw/ih\,1.25)\,ih\,iw/1.25)/2)*2",
}

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.RLock()

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


def resolve_youtube_selection(raw_url: str, media_type: str) -> tuple[str, str, bool, str]:
    """Resolve a channel tab or one YouTube video/Shorts URL."""
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
        return f"https://www.youtube.com/watch?v={video_id}", label, True, resolved_type
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
    return f"https://www.youtube.com{root}/{media_type}", label, False, media_type


def crop_filter_for(crop_ratio: str) -> str | None:
    if crop_ratio == "original":
        return None
    if not isinstance(crop_ratio, str) or crop_ratio not in CROP_FILTERS:
        raise ValueError("Choose Original, 4:5, 1:1, or 5:4 framing.")
    return CROP_FILTERS[crop_ratio]


def crop_postprocessor_args(crop_filter: str) -> str:
    args = [
        # Keep the same CRF quality target while choosing x264's fastest preset.
        # This trades encoding efficiency (larger files) for much shorter crop times.
        "-c:v", "libx264", "-crf", "18", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "192k", "-vf", crop_filter,
    ]
    return "VideoConvertor+ffmpeg_o:" + shlex.join(args)


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "media_type": job["media_type"],
        "channel": job["channel"],
        "progress": job.get("progress"),
        "current": job.get("current", ""),
        "output_dir": str(job["output_dir"]),
        "message": job.get("message", ""),
        "logs": job.get("logs", [])[-40:],
    }


def add_log(job_id: str, line: str) -> None:
    line = line.strip()
    if not line:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.setdefault("logs", []).append(line)
        job["logs"] = job["logs"][-100:]
        if "Destination:" in line:
            job["current"] = line.split("Destination:", 1)[1].strip()
            if "[VideoConvertor] Converting video from" in line:
                job["progress"] = 100
                job["message"] = "The source is downloaded. Cropping and converting to MP4 now; this can take a while."
            else:
                job["progress"] = 0
                if "[Merger]" in line:
                    job["message"] = "The source is downloaded. Assembling the video and audio…"
        match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if match:
            job["progress"] = float(match.group(1))
        if "has already been downloaded" in line:
            job["message"] = "Skipping a file already in the download folder."


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


def run_download(job_id: str, command: list[str]) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] == "cancelled":
            return
        job["status"] = "running"
        job["message"] = "Connecting to the channel…"
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=(os.name == "posix"),
            env=bundled_runtime_env(),
        )
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            cancelled = not job or job["status"] == "cancelled"
            if job:
                job["process"] = process
        if cancelled:
            stop_process_tree(process)
        assert process.stdout is not None
        for line in process.stdout:
            add_log(job_id, line)
        return_code = process.wait()
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            if job["status"] == "cancelled":
                job["message"] = "Download cancelled. Files already completed are kept."
            elif return_code == 0:
                job["status"] = "complete"
                job["progress"] = 100
                job["message"] = "Download finished."
            else:
                job["status"] = "complete_with_errors"
                job["message"] = "The download ended with some errors. Check the log below."
            job.pop("process", None)
    except FileNotFoundError:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["message"] = "A bundled downloader tool could not be started. Restart the app to repair its local tools."
    except Exception as exc:  # Keep failures visible in the local UI.
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["message"] = f"Download failed: {exc}"


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
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self.require_authentication():
            return
        path = urlsplit(self.path).path
        if path == "/api/status":
            status = bundled_tool_status()
            status["download_dir"] = str(DOWNLOADS_DIR)
            json_response(self, status)
            return
        file_route = re.fullmatch(r"/api/jobs/([a-f0-9]+)/files(?:/(.*))?", path)
        if file_route:
            job_id, encoded_name = file_route.groups()
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                output_dir = job["output_dir"] if job else None
            if not output_dir:
                json_response(self, {"error": "Download job not found."}, 404)
                return
            if encoded_name is None:
                files = []
                for file_path in sorted(output_dir.iterdir(), key=lambda item: item.name.lower()):
                    if file_path.is_file() and not file_path.name.endswith((".part", ".ytdl")):
                        files.append({
                            "name": file_path.name,
                            "size": file_path.stat().st_size,
                            "url": f"/api/jobs/{job_id}/files/{quote(file_path.name)}",
                        })
                json_response(self, {"files": files})
                return
            file_name = unquote(encoded_name)
            if not file_name or Path(file_name).name != file_name:
                json_response(self, {"error": "Invalid file name."}, 400)
                return
            file_path = (output_dir / file_name).resolve()
            if file_path.parent != output_dir.resolve() or not file_path.is_file():
                json_response(self, {"error": "Downloaded file not found."}, 404)
                return
            self.send_download(file_path)
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job:
                    json_response(self, public_job(job))
                else:
                    json_response(self, {"error": "Download job not found."}, 404)
            return
        if path in {"/", "/index.html"}:
            self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        self.send_error(404)

    def send_file(self, file_path: Path, content_type: str) -> None:
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, file_path: Path) -> None:
        size = file_path.stat().st_size
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        safe_ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_path.name) or "download"
        encoded_name = quote(file_path.name)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename=\"{safe_ascii_name}\"; filename*=UTF-8''{encoded_name}")
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        with file_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_POST(self) -> None:  # noqa: N802
        if not self.require_authentication():
            return
        path = urlsplit(self.path).path
        try:
            if not self.has_local_origin():
                json_response(self, {"error": "Request origin is not allowed."}, 403)
                return
            if path == "/api/preview":
                if not bundled_tool_status()["yt_dlp"]:
                    json_response(self, {"error": "The app-local yt-dlp tool is not available. Restart the app to install it."}, 503)
                    return
                data = self.read_json()
                tab_url, selection_label, is_single, resolved_media_type = resolve_youtube_selection(
                    data.get("url", ""), data.get("media_type", "videos"),
                )
                command = [*yt_dlp_command(), "--dump-single-json", "--no-warnings"]
                if is_single:
                    command.append("--no-playlist")
                else:
                    command.extend(["--flat-playlist", "--playlist-end", str(MAX_PREVIEW_ITEMS)])
                command.append(tab_url)
                result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=bundled_runtime_env())
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
                videos = []
                for entry in entries:
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
                    })
                json_response(self, {
                    "channel": (listing.get("title") if is_single else listing.get("channel")) or listing.get("uploader") or selection_label,
                    "media_type": resolved_media_type,
                    "single": is_single,
                    "count": len(videos),
                    "total_count": listing.get("playlist_count"),
                    "truncated": not is_single and len(videos) >= MAX_PREVIEW_ITEMS and (not listing.get("playlist_count") or listing.get("playlist_count") > len(videos)),
                    "videos": videos,
                })
                return
            if path == "/api/download":
                tools = bundled_tool_status()
                if not tools["yt_dlp"] or not tools["deno"]:
                    json_response(self, {"error": "The app-local YouTube tools are not available. Restart the app to install them."}, 503)
                    return
                ffmpeg_path = ffmpeg_executable()
                if not ffmpeg_path:
                    json_response(self, {"error": "The app-local FFmpeg tool is not available. Restart the app to install it."}, 503)
                    return
                data = self.read_json()
                tab_url, selection_label, is_single, media_type = resolve_youtube_selection(
                    data.get("url", ""), data.get("media_type", "videos"),
                )
                crop_ratio = data.get("crop_ratio", "original")
                crop_filter = crop_filter_for(crop_ratio)
                safe_channel = re.sub(r"[^A-Za-z0-9._@-]+", "_", selection_label)
                output_dir = (
                    DOWNLOADS_DIR / "single-links" / media_type / safe_channel
                    if is_single else DOWNLOADS_DIR / safe_channel / media_type
                )
                crop_folder = {"4:5": "crop-4x5", "1:1": "crop-1x1", "5:4": "crop-5x4"}.get(crop_ratio)
                if crop_folder:
                    output_dir = output_dir / crop_folder
                output_dir.mkdir(parents=True, exist_ok=True)
                command = [
                    *yt_dlp_command(),
                    "--ignore-errors",
                    "--no-overwrites",
                    "--continue",
                    "--newline",
                    "--ffmpeg-location", ffmpeg_path,
                    "--output", str(output_dir / "%(title).180B [%(id)s].%(ext)s"),
                ]
                if crop_filter:
                    command.extend([
                        "--merge-output-format", "mkv", "--remux-video", "mkv",
                        "--recode-video", "mp4", "--postprocessor-args", crop_postprocessor_args(crop_filter),
                    ])
                else:
                    command.extend(["--merge-output-format", "mp4", "--remux-video", "mp4"])
                command.append("--no-playlist" if is_single else "--yes-playlist")
                command.extend(["--format", "bv*+ba/b", tab_url])
                job_id = uuid.uuid4().hex[:12]
                job = {
                    "id": job_id,
                    "status": "queued",
                    "media_type": media_type,
                    "single": is_single,
                    "crop_ratio": crop_ratio,
                    "channel": selection_label,
                    "output_dir": output_dir,
                    "progress": 0,
                    "current": "",
                    "message": "Preparing download…",
                    "logs": [],
                }
                with JOBS_LOCK:
                    JOBS[job_id] = job
                threading.Thread(target=run_download, args=(job_id, command), daemon=True).start()
                json_response(self, {"id": job_id, "output_dir": str(output_dir)}, 202)
                return
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if not job:
                        json_response(self, {"error": "Download job not found."}, 404)
                        return
                    process = job.get("process")
                    should_cancel = job["status"] not in {"complete", "complete_with_errors", "error", "cancelled"}
                    if should_cancel:
                        job["status"] = "cancelled"
                        job["message"] = "Download cancelled. Files already completed are kept."
                if process and should_cancel:
                    stop_process_tree(process)
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    json_response(self, public_job(job))
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
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"YouTube Channel Downloader is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server…")
        with JOBS_LOCK:
            active_processes = []
            for job in JOBS.values():
                process = job.get("process")
                if process and job["status"] not in {"complete", "complete_with_errors", "error", "cancelled"}:
                    job["status"] = "cancelled"
                    job["message"] = "Server stopped; active download cancelled."
                    active_processes.append(process)
        for process in active_processes:
            stop_process_tree(process)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
