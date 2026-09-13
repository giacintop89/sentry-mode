"""One bounded, continuous PCM stream from a phone to the configured speaker."""

import os
import queue
import secrets
import select
import subprocess
import tempfile
import threading
import time

from sentry_node.audio.effects import VoiceEffects
from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.hardware.speaker import Speaker

RATE = 48000
MAX_CHUNK = 9600  # 100 ms of mono PCM16, little endian.
MAX_SECONDS = 60
IDLE_SECONDS = 3


class TalkStream:
    def __init__(self, config: Settings, lock: threading.Lock):
        self.config = config
        self.lock = lock
        self.guard = threading.Lock()
        self.token: str | None = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=12)
        self.sequence = 0
        self.accepting = False

    def start(self, effects: VoiceEffects | None = None) -> dict:
        effects = effects or VoiceEffects()
        volume = self.config.speaker.volume if effects.volume is None else effects.volume
        with self.guard:
            if not self.lock.acquire(blocking=False):
                raise BlockingIOError("Speaker is busy; wait for the current audio operation.")
            try:
                device = Speaker(self.config.speaker).device()
                if device.backend == "pipewire":
                    args = [
                        "pw-play",
                        "--target",
                        device.name,
                        "--raw",
                        "--rate",
                        str(RATE),
                        "--channels",
                        "1",
                        "--format",
                        "s16",
                        "--latency",
                        f"{self.config.speaker.pipewire_latency_ms}ms",
                        "--volume",
                        str(volume / 100),
                        "-",
                    ]
                elif device.backend == "pulse":
                    args = [
                        "paplay",
                        "--raw",
                        "--rate=48000",
                        "--channels=1",
                        "--format=s16le",
                        f"--volume={int(volume * 655.36)}",
                    ]
                    if device.name != "auto":
                        args.append(f"--device={device.name}")
                else:
                    args = [
                        "aplay",
                        "-D",
                        device.name,
                        "-t",
                        "raw",
                        "-f",
                        "S16_LE",
                        "-r",
                        str(RATE),
                        "-c",
                        "1",
                    ]
                errors = tempfile.TemporaryFile()
                process = effect_process = None
                try:
                    process = subprocess.Popen(
                        args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors
                    )
                    pitch_filter = effects.pitch_filter()
                    filters = [pitch_filter] if pitch_filter else []
                    if device.backend == "alsa" and volume != 100:
                        filters.append(f"volume={volume / 100:g}")
                    if filters:
                        effect_process = subprocess.Popen(
                            [
                                "ffmpeg",
                                "-nostdin",
                                "-v",
                                "error",
                                "-probesize",
                                "32",
                                "-analyzeduration",
                                "0",
                                "-f",
                                "s16le",
                                "-ar",
                                str(RATE),
                                "-ac",
                                "1",
                                "-i",
                                "pipe:0",
                                "-af",
                                ",".join(filters),
                                "-ar",
                                str(RATE),
                                "-ac",
                                "1",
                                "-f",
                                "s16le",
                                "-flush_packets",
                                "1",
                                "pipe:1",
                            ],
                            stdin=subprocess.PIPE,
                            stdout=process.stdin,
                            stderr=errors,
                        )
                        assert process.stdin is not None
                        process.stdin.close()
                except BaseException:
                    if process is not None:
                        process.terminate()
                        process.wait(timeout=2)
                        assert process.stdin is not None
                        process.stdin.close()
                    errors.close()
                    raise
                self.token = secrets.token_urlsafe(24)
                self.error = None
                self.sequence = 0
                self.accepting = True
                self.chunks = queue.Queue(maxsize=12)
                self.cancelled.clear()
                self.thread = threading.Thread(
                    target=self._play,
                    args=(process, errors, effect_process),
                    name="sentry-node-talk",
                )
                self.thread.start()
                return {
                    "token": self.token,
                    "sample_rate": RATE,
                    "max_seconds": MAX_SECONDS,
                    "effects": {
                        "preset": effects.preset,
                        "pitch": effects.semitones,
                        "volume": volume,
                    },
                }
            except BaseException:
                self.lock.release()
                raise

    def _check(self, token: str) -> None:
        if not self.token or not secrets.compare_digest(token, self.token):
            raise ValueError("Unknown push-to-talk session.")
        if self.error:
            raise HardwareError(self.error)

    def chunk(self, token: str, sequence: int, data: bytes) -> dict:
        if not data or len(data) > MAX_CHUNK or len(data) % 2:
            raise ValueError("Send 1 to 4800 mono PCM16 samples per audio chunk.")
        with self.guard:
            self._check(token)
            if not self.accepting:
                raise ValueError("Push-to-talk session has ended.")
            if sequence != self.sequence:
                raise ValueError("Audio chunks must arrive in sequence.")
            try:
                self.chunks.put_nowait(data)
            except queue.Full as exc:
                self.cancelled.set()
                raise BlockingIOError(
                    "Audio connection is too slow; release and try again."
                ) from exc
            self.sequence += 1
            return {"accepted": sequence}

    def finish(self, token: str, *, cancel: bool = False) -> dict:
        with self.guard:
            self._check(token)
            if cancel:
                self.cancelled.set()
            elif self.accepting:
                try:
                    self.chunks.put_nowait(None)
                except queue.Full:
                    self.cancelled.set()
                    raise BlockingIOError("Audio connection is too slow; try again.") from None
            self.accepting = False
            thread = self.thread
        if thread:
            thread.join(timeout=6)
            if thread.is_alive():
                self.cancelled.set()
                raise HardwareError("Push-to-talk playback did not stop in time.")
        with self.guard:
            self._check(token)
        return {"message": "Transmission cancelled." if cancel else "Transmission finished."}

    def close(self) -> None:
        self.cancelled.set()
        if self.thread:
            self.thread.join(timeout=6)

    def _play(self, process, errors, effect_process=None) -> None:
        started = last_chunk = time.monotonic()
        input_process = effect_process or process
        try:
            os.set_blocking(input_process.stdin.fileno(), False)

            def write(data: bytes) -> None:
                remaining = memoryview(data)
                deadline = time.monotonic() + 2
                while remaining:
                    if self.cancelled.is_set():
                        raise InterruptedError
                    if process.poll() is not None or input_process.poll() is not None:
                        raise HardwareError("Speaker disconnected during push-to-talk.")
                    if time.monotonic() > deadline:
                        raise HardwareError("Speaker stopped consuming audio.")
                    if select.select([], [input_process.stdin], [], 0.1)[1]:
                        try:
                            count = os.write(input_process.stdin.fileno(), remaining)
                            remaining = remaining[count:]
                        except BlockingIOError:
                            continue

            # Pre-roll opens the Bluetooth transport and buffers incoming phone packets.
            write(bytes(RATE * 2 * min(self.config.speech.lead_in_ms, 1000) // 1000))
            while not self.cancelled.is_set():
                now = time.monotonic()
                if now - started > MAX_SECONDS + 3 or now - last_chunk > IDLE_SECONDS:
                    raise HardwareError("Transmission expired; hold the button to try again.")
                try:
                    chunk = self.chunks.get(timeout=0.1)
                except queue.Empty:
                    continue
                if chunk is None:
                    write(bytes(RATE * 2 * min(self.config.speech.tail_ms, 750) // 1000))
                    input_process.stdin.close()
                    if effect_process is not None:
                        effect_process.wait(timeout=3)
                        if effect_process.returncode:
                            raise HardwareError(
                                "Voice effect failed; check FFmpeg rubberband support."
                            )
                    process.wait(timeout=3)
                    if process.returncode:
                        raise HardwareError("Speaker playback failed.")
                    break
                last_chunk = time.monotonic()
                write(chunk)
        except InterruptedError:
            pass
        except (OSError, HardwareError, subprocess.SubprocessError) as exc:
            self.error = str(exc)
        finally:
            for child in [effect_process, process]:
                if child is None:
                    continue
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                child.stdin.close()
            errors.close()
            with self.guard:
                self.accepting = False
                self.lock.release()
