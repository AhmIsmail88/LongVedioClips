# -*- coding: utf-8 -*-
"""Watch a link inside the app: resolve a direct stream and serve it locally.

Two delivery modes:
  http -> the site gives one file URL (mp4/webm). We pass it through a local
          Range-capable proxy so signed headers survive and seeking works.
  mux  -> the site only offers separate video+audio (YouTube DASH). ffmpeg
          copies both into a fragmented mp4 and we stream that to the player.
Nothing is written to disk.
"""
from __future__ import annotations

import http.server
import os
import secrets
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 64 * 1024
MERGED_FORMAT = "bv*[height<={height}]+ba/b[height<={height}]/b"
PREFERRED_FORMAT = ("best[ext=mp4][vcodec!=none][acodec!=none][protocol^=http]"
                    "/best[ext=mp4][protocol^=http]/best[protocol^=http]")


class StreamError(RuntimeError):
    """رسالة صالحة للعرض للمستخدم."""


def _runtime_node():
    try:
        from core.media_download import runtime_path
        return runtime_path()
    except Exception:  # noqa: BLE001 - اختياري
        return None


def _headers_of(info: dict, formats: list) -> dict:
    headers = {}
    for candidate in [info, *formats]:
        headers.update({key: value for key, value in (candidate.get("http_headers") or {}).items() if value})
    return headers


def classify(info: dict) -> dict:
    """يقرر كيفية التشغيل من بيانات yt-dlp — دالة خالصة قابلة للاختبار."""
    if not info:
        raise StreamError("لم أجد فيديو في هذا الرابط")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries"):
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise StreamError("الرابط لا يحتوي فيديو قابلًا للتشغيل")
        info = entries[0]

    formats = [item for item in (info.get("requested_formats") or []) if item]
    video = next((item for item in formats if item.get("vcodec") not in (None, "none")), None)
    audio = next((item for item in formats
                  if item.get("vcodec") in (None, "none") and item.get("acodec") not in (None, "none")), None)
    headers = _headers_of(info, formats)

    result = {
        "mode": "http",
        "url": info.get("url") or (video or {}).get("url") or "",
        "video_url": (video or {}).get("url") or "",
        "audio_url": (audio or {}).get("url") or "",
        "headers": headers,
        "mime": "video/mp4",
        "title": info.get("title") or "فيديو",
        "site": info.get("extractor_key") or "",
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live")),
        "has_audio": any((item.get("acodec") not in (None, "none")) for item in formats) or
                     info.get("acodec") not in (None, "none"),
        "ext": info.get("ext") or "",
        "format_note": info.get("format_note") or "",
    }

    if result["video_url"] and result["audio_url"] and result["video_url"] != result["audio_url"]:
        result["mode"] = "mux"
        result["url"] = ""
    elif not result["url"]:
        raise StreamError("لم أجد رابط بث مباشر لهذا الفيديو")
    if result["mode"] == "mux":
        result["has_audio"] = True
    return result


def resolve(url: str, max_height: int = 720, cookies: str = "") -> dict:
    """يستخرج ما يكفي للتشغيل الفوري بدون تنزيل."""
    url = (url or "").strip()
    if not url:
        raise StreamError("ألصق رابط الفيديو أولًا")
    cookies = (cookies or "").strip()
    if cookies and not Path(cookies).is_file():
        raise StreamError("ملف cookies غير موجود")
    try:
        from yt_dlp import YoutubeDL
    except ImportError as error:  # pragma: no cover - يعتمد على البيئة
        raise StreamError("yt-dlp غير مثبت في هذه البيئة") from error

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "format": MERGED_FORMAT.format(height=max_height),
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": True,
    }
    node = _runtime_node()
    if node:
        options["js_runtimes"] = {"node": {"path": node}}
    if cookies:
        options["cookiefile"] = cookies
    ydl = YoutubeDL(options)
    try:
        info = ydl.extract_info(url, download=False)
        stream = classify(info)
    except Exception as error:  # noqa: BLE001 - رسالة مفهومة للمستخدم
        try:
            ydl.close()
        except Exception:  # noqa: BLE001
            pass
        from core.media_download import describe_failure
        raise StreamError(describe_failure(error)) from error
    # Keep the extractor session: TikTok-style CDNs reject plain HTTP clients.
    stream["session"] = StreamSession(ydl)
    stream["headers"] = stream.get("headers") or {}
    return stream


class StreamSession:
    """غلاف حول YoutubeDL يبقي الكوكيز والانتحال متاحين لوسيط البث."""

    def __init__(self, ydl) -> None:
        self.ydl = ydl

    def open(self, url: str, headers: dict | None = None) -> object:
        from yt_dlp.networking import Request
        request = Request(url, headers=dict(headers or {}), extensions={"timeout": 30})
        return self.ydl.urlopen(request)

    def close(self) -> None:
        try:
            self.ydl.close()
        except Exception:  # noqa: BLE001 - إغلاق هادئ
            pass


class StreamProxy:
    """وسيط محلي على 127.0.0.1 برمز عشوائي؛ يدعم التمرير والدمج عند الطلب."""

    def __init__(self, url=None, headers=None, video_url=None, audio_url=None,
                 mime="video/mp4", ffmpeg=None, session=None):
        self.session = session
        self.url = url or ""
        self.video_url = video_url or ""
        self.audio_url = audio_url or ""
        self.headers = {key: value for key, value in (headers or {}).items() if value}
        self.mime = mime
        self.ffmpeg = ffmpeg
        self.token = secrets.token_urlsafe(18)
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._children: list[subprocess.Popen] = []
        self.port = 0

    @property
    def is_mux(self) -> bool:
        return bool(self.video_url and self.audio_url and not self.url)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> str:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D102 - بلا ضجيج
                return

            def do_HEAD(self):  # noqa: N802
                self._serve(head_only=True)

            def do_GET(self):  # noqa: N802
                self._serve(head_only=False)

            def _serve(self, head_only: bool):
                if self.path.split("?")[0].rstrip("/") != f"/t/{proxy.token}":
                    self.send_error(404)
                    return
                if proxy.is_mux:
                    proxy._serve_mux(self, head_only)
                else:
                    proxy._serve_http(self, head_only)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}/t/{self.token}"

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if self.session is not None:
            self.session.close()
            self.session = None
        for child in list(self._children):
            try:
                if child.poll() is None:
                    child.kill()
            except Exception:  # noqa: BLE001
                pass
        self._children.clear()
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    # -- delivery --------------------------------------------------------
    def _serve_http(self, handler, head_only: bool) -> None:
        request_headers = dict(self.headers)
        request_headers.setdefault("User-Agent", "Mozilla/5.0")
        requested = handler.headers.get("Range")
        if requested:
            request_headers["Range"] = requested
        try:
            if self.session is not None:
                upstream = self.session.open(self.url, request_headers)
            else:
                upstream = urllib.request.urlopen(
                    urllib.request.Request(self.url, headers=request_headers), timeout=30)
        except urllib.error.HTTPError as error:
            body = f"upstream {error.code}".encode()
            handler.send_response(502)
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.end_headers()
            if not head_only:
                handler.wfile.write(body)
            return
        except Exception:  # noqa: BLE001 - شبكة
            handler.send_response(502)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        with upstream:
            status = getattr(upstream, "status", 200)
            length = upstream.headers.get("Content-Length")
            handler.send_response(status if status in (200, 206) else 200)
            handler.send_header("Content-Type", upstream.headers.get("Content-Type") or self.mime)
            if length:
                handler.send_header("Content-Length", length)
            content_range = upstream.headers.get("Content-Range")
            if content_range:
                handler.send_header("Content-Range", content_range)
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Connection", "close")
            handler.end_headers()
            if head_only:
                return
            self._pump(handler, upstream.read)

    def _serve_mux(self, handler, head_only: bool) -> None:
        command = self._mux_command()
        handler.send_response(200)
        handler.send_header("Content-Type", self.mime)
        handler.send_header("Accept-Ranges", "none")
        handler.send_header("Connection", "close")
        handler.end_headers()
        if head_only:
            return
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError:
            return
        self._children.append(process)
        try:
            assert process.stdout is not None
            self._pump(handler, process.stdout.read)
        finally:
            if process.poll() is None:
                process.kill()
            if process in self._children:
                self._children.remove(process)

    def _pump(self, handler, read) -> None:
        try:
            while True:
                chunk = read(CHUNK)
                if not chunk:
                    break
                handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _mux_command(self) -> list:
        ffmpeg = self.ffmpeg
        if not ffmpeg:
            from utils.ffmpeg import get_ffmpeg_binaries
            ffmpeg, _ = get_ffmpeg_binaries()
        header_line = "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error"]
        for source in (self.video_url, self.audio_url):
            if header_line:
                command += ["-headers", header_line]
            command += ["-i", source]
        command += ["-c", "copy", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                    "-f", "mp4", "pipe:1"]
        return command
