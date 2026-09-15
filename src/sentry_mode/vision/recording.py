"""Photos, videos and audio from the shared camera and microphones, kept in one bounded folder."""

import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from sentry_node.audio.playback import command
from sentry_node.core.errors import HardwareError

MAX_CAPTURES = 200
VIDEO_MAX_WIDTH = 1280
VIDEO_FPS = 10
MAX_MESSAGE_SECONDS = 120
MAX_MESSAGE_BYTES = 5 * 1024 * 1024
CAPTURE_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]{3}-[a-z0-9-]{1,40}\.(jpg|mp4|m4a)$")
MEDIA_TYPES = {"jpg": "image/jpeg", "mp4": "video/mp4", "m4a": "audio/mp4"}
KINDS = {".jpg": "photo", ".mp4": "video", ".m4a": "audio"}
AAC = ["-ac", "1", "-c:a", "aac", "-b:a", "96k"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "capture"


class Captures:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.Lock()

    def _new_path(self, rule: str, extension: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        return self.directory / f"{stamp}-{slug(rule)}.{extension}"

    def path(self, name: str) -> Path:
        path = self.directory / name
        if not CAPTURE_NAME.fullmatch(name) or not path.is_file():
            raise LookupError("Capture not found.")
        return path

    def listing(self) -> dict:
        try:
            found = [p for p in self.directory.iterdir() if CAPTURE_NAME.fullmatch(p.name)]
        except OSError:
            found = []
        captures = []
        for path in sorted(found, key=lambda p: p.name, reverse=True):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            stamp = datetime.strptime(path.name[:19], "%Y%m%d-%H%M%S-%f")
            captures.append(
                {
                    "name": path.name,
                    "kind": KINDS[path.suffix],
                    "rule": path.stem[20:],
                    "time": stamp.isoformat(timespec="seconds"),
                    "size": size,
                }
            )
        return {"captures": captures}

    def delete(self, name: str) -> dict:
        with self.lock:
            self.path(name).unlink()
        return self.listing()

    def _prune(self):
        with self.lock:
            try:
                found = sorted(
                    p for p in self.directory.iterdir() if CAPTURE_NAME.fullmatch(p.name)
                )
            except OSError:
                return
            for old in found[:-MAX_CAPTURES]:
                old.unlink(missing_ok=True)

    def save_photo(self, frame, rule: str) -> str:
        import cv2

        path = self._new_path(rule, "jpg")
        partial = path.with_name(path.name + ".part")
        try:
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise HardwareError("Photo encoding failed.")
            partial.write_bytes(encoded.tobytes())
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        self._prune()
        return path.name

    @staticmethod
    def _start_audio(microphone: list[str], seconds: float, output: Path) -> subprocess.Popen:
        return subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", *microphone,
             "-t", f"{seconds:g}", *AAC, "-movflags", "+faststart", str(output)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )  # fmt: skip

    @staticmethod
    def _finish_audio(process: subprocess.Popen, output: Path) -> str | None:
        """Stop a microphone recording so ffmpeg writes a playable file; return any error."""
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            _, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return "the microphone stopped responding"
        # ffmpeg exits with 255 after finishing a file on SIGINT.
        if process.returncode in (0, 255) and output.is_file() and output.stat().st_size:
            return None
        detail = (errors or b"").decode(errors="replace").strip()[-300:]
        return detail or f"ffmpeg exited with {process.returncode}"

    def record_audio(
        self, microphone: list[str], seconds: int, rule: str, stop_event: threading.Event
    ) -> str:
        """Record the node microphone; stopping early keeps what was recorded."""
        path = self._new_path(rule, "m4a")
        partial = path.with_name(path.stem + ".part.m4a")
        process = self._start_audio(microphone, seconds, partial)
        try:
            deadline = time.monotonic() + seconds + 10
            while process.poll() is None and time.monotonic() < deadline:
                if stop_event.wait(0.1):
                    break
            error = self._finish_audio(process, partial)
            if error:
                raise HardwareError(f"Audio recording failed: {error}")
            partial.replace(path)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            partial.unlink(missing_ok=True)
        self._prune()
        return path.name

    def save_message(self, data: bytes) -> str:
        """Convert a message recorded in the browser (WebM, Ogg or MP4) to AAC audio."""
        if not data:
            raise ValueError("The recorded message is empty.")
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError(f"Messages are limited to {MAX_MESSAGE_BYTES // 1048576} MB.")
        path = self._new_path("phone message", "m4a")
        partial = path.with_name(path.stem + ".part.m4a")
        try:
            with tempfile.TemporaryDirectory(prefix="sentry-node-message-") as directory:
                source = Path(directory) / "message"
                source.write_bytes(data)
                command(
                    ["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
                     "-i", str(source), "-vn", "-t", str(MAX_MESSAGE_SECONDS), *AAC,
                     "-movflags", "+faststart", str(partial)],
                    timeout=60,
                )  # fmt: skip
            if not partial.is_file() or not partial.stat().st_size:
                raise HardwareError("no audio was decoded")
            partial.replace(path)
        except (HardwareError, OSError) as exc:
            raise ValueError(f"Could not save the recorded message: {exc}") from exc
        finally:
            partial.unlink(missing_ok=True)
        self._prune()
        return path.name

    def record_video(
        self,
        frames,
        seconds: int,
        rule: str,
        stop_event: threading.Event,
        size: tuple[int, int] | None = None,
        microphone: list[str] | None = None,
    ) -> tuple[str, str | None]:
        """Sample the latest frame at a fixed rate so the file lasts as long as asked.

        `frames` returns the newest camera frame or None. Stopping early keeps what was recorded.
        `size` fixes the output width and height, since the camera may still be switching up.
        `microphone` adds sound; if it fails the video is kept silent and the error returned.
        """
        import cv2

        first = None
        deadline = time.monotonic() + 3
        while first is None:
            first = frames()
            if first is None and (stop_event.wait(0.05) or time.monotonic() > deadline):
                raise HardwareError("No camera frame is available for recording.")
        width, height = size or (first.shape[1], first.shape[0])
        if width > VIDEO_MAX_WIDTH:
            width, height = VIDEO_MAX_WIDTH, round(height * VIDEO_MAX_WIDTH / width)
        width, height = width - width % 2, height - height % 2  # yuv420p needs even sizes.
        path = self._new_path(rule, "mp4")
        partial = path.with_name(path.stem + ".part.mp4")
        process = subprocess.Popen(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
                "-r", str(VIDEO_FPS), "-i", "-",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )  # fmt: skip
        audio_part = path.with_name(path.stem + ".part-audio.m4a")
        muxed = path.with_name(path.stem + ".part-mux.mp4")
        audio = audio_error = None
        written = 0
        try:
            if microphone:
                audio = self._start_audio(microphone, seconds + 5, audio_part)
            start = time.monotonic()
            frame = first
            while written < seconds * VIDEO_FPS:
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))
                process.stdin.write(frame.tobytes())
                written += 1
                if stop_event.wait(max(0, start + written / VIDEO_FPS - time.monotonic())):
                    break
                frame = frames()
                if frame is None:
                    break
            _, errors = process.communicate(timeout=30)
            if process.returncode:
                detail = errors.decode(errors="replace").strip()[-300:]
                raise HardwareError(f"Video encoding failed: {detail or process.returncode}")
            if audio is not None:
                audio_error = self._finish_audio(audio, audio_part)
            if audio is not None and not audio_error:
                try:
                    command(
                        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(partial),
                         "-i", str(audio_part), "-map", "0:v", "-map", "1:a", "-c", "copy",
                         "-t", f"{written / VIDEO_FPS:g}", "-movflags", "+faststart", str(muxed)],
                        timeout=60,
                    )  # fmt: skip
                    muxed.replace(partial)
                except (HardwareError, OSError) as exc:
                    audio_error = str(exc)
            partial.replace(path)
        except (BrokenPipeError, subprocess.TimeoutExpired) as exc:
            raise HardwareError(f"Video encoding failed: {exc}") from exc
        finally:
            for running in (process, audio):
                if running is not None and running.poll() is None:
                    running.kill()
                    running.wait()
            for leftover in (partial, audio_part, muxed):
                leftover.unlink(missing_ok=True)
        self._prune()
        return path.name, audio_error
