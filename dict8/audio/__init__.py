"""Microphone capture while the talk key is held.

`Recorder.start()` opens a sounddevice input stream at the model's sample rate, mono, and
appends every callback block to a list; `stop()` closes it and returns one float32 array,
or None when less than `audio.min_hold_ms` was captured — build.md phase 4: "release with
<300 ms of audio (discard silently)". A short tap of the talk key is not a dictation.

The stream is opened on press and closed on release, never held open between dictations:
an open input stream keeps macOS's orange microphone indicator lit, and "invisible, never in
the way" (CLAUDE.md) does not include a mic that looks permanently live.

`hardware.mic_device` is still TBD (an output of the real-speech bench). Until it is set,
capture uses the system default input and says so through `device_label` — the UI shows
that as a labeled gap (invariant 3), it is not a silent default.

The OS call is `stream_factory` (sounddevice.InputStream by default). Tests pass a fake
factory and drive the callback with synthetic numpy blocks; nothing else is mocked.
"""

from __future__ import annotations

import threading
from typing import Callable


class MicUnavailable(RuntimeError):
    """The input stream could not be opened (no device, device busy, permission)."""


class Recorder:
    def __init__(self, cfg, sample_rate: int, stream_factory: Callable | None = None) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = 1  # Whisper consumes mono; not a tunable
        self.min_hold_ms = float(cfg.require("audio.min_hold_ms"))
        self.device = cfg.get("hardware.mic_device")  # None while TBD -> system default
        self._factory = stream_factory
        self._blocks: list = []
        self._lock = threading.Lock()
        self._stream = None
        self.overflows = 0

    @property
    def device_label(self) -> str:
        return (str(self.device) if self.device
                else "system default input (hardware.mic_device is TBD)")

    def _callback(self, indata, frames, time_info, status) -> None:
        # Runs on PortAudio's thread: copy and return, nothing else.
        if status and getattr(status, "input_overflow", False):
            self.overflows += 1
        with self._lock:
            self._blocks.append(indata.copy())

    def start(self) -> None:
        factory = self._factory
        if factory is None:
            import sounddevice as sd
            factory = sd.InputStream
        with self._lock:
            self._blocks = []
        self.overflows = 0
        try:
            self._stream = factory(samplerate=self.sample_rate, channels=self.channels,
                                   dtype="float32", device=self.device,
                                   callback=self._callback)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise MicUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def _close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def level(self) -> float | None:
        """RMS of the newest block in dBFS, for the overlay's live bars; None when not
        recording. Called from the main thread: it reads one block and leaves the audio
        callback alone."""
        if self._stream is None:
            return None
        with self._lock:
            block = self._blocks[-1] if self._blocks else None
        if block is None:
            return None
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
        return 20 * float(np.log10(max(rms, 1e-10)))

    def cancel(self) -> None:
        self._close()
        with self._lock:
            self._blocks = []

    def stop(self):
        """Close the stream; return (samples, duration_ms) or (None, duration_ms) if too short."""
        self._close()
        with self._lock:
            blocks, self._blocks = self._blocks, []
        return self.finish(blocks)

    def finish(self, blocks):
        import numpy as np

        if not blocks:
            return None, 0.0
        audio = np.concatenate(blocks, axis=0).astype(np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
        ms = len(audio) / self.sample_rate * 1000
        if ms < self.min_hold_ms:
            return None, ms
        return audio, ms


def peak_frame_dbfs(audio, sample_rate: int, frame_ms: float) -> float:
    """Loudest short-frame RMS in the clip, in dBFS (float samples, full scale = 1.0)."""
    import numpy as np

    n = max(1, int(sample_rate * frame_ms / 1000))
    usable = audio[: len(audio) // n * n]
    if usable.size == 0:
        usable = audio
    frames = usable.reshape(-1, n) if usable.size >= n else usable.reshape(1, -1)
    rms = float(np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)).max()) if frames.size else 0.0
    return 20 * float(np.log10(max(rms, 1e-10)))


def is_silent(audio, sample_rate: int, floor_dbfs: float, frame_ms: float) -> bool:
    """The `stt.vad` gate: True when no frame reaches `audio.silence_floor_dbfs`.

    Deliberately crude — an energy floor, not a speech detector. It exists for one failure:
    a muted mic, the wrong input, or the first-use permission prompt swallowing the audio
    hands Whisper pure silence, and Whisper answers silence with invented words ("Thank
    you.") that would then be typed into Claude Code. Invariant 1 forbids that outright.
    """
    return peak_frame_dbfs(audio, sample_rate, frame_ms) < floor_dbfs
