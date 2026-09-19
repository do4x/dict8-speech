"""Local speech-to-text, model held warm in-process (ADR-001, `stt.keep_resident`).

One backend today: `stt.backend: mlx-whisper`, picked by the provisional quick bench in
`bench/stt.md` (synthesized speech, 2026-09-19). The real-speech bench is still Denis's.

Invariant 7: audio never leaves the machine. The model is resolved from the local Hugging
Face cache first; mlx-whisper's own `load_model` would otherwise ask the Hub for the latest
snapshot on every load. A download happens only when the weights are not cached at all, and
it moves weights *to* this machine, never audio or text away from it.

Invariant 1 starts here: `transcribe()` returns the model's text with leading/trailing
whitespace stripped and nothing else touched — no casing, no punctuation, no "cleanup".

Needs the `app` extra (`uv run --extra app ...`). Nothing in the base install imports this.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

SUPPORTED_BACKENDS = ("mlx-whisper",)


class STTUnavailable(RuntimeError):
    """The configured backend cannot run here (extra not installed, unknown backend)."""


class STT:
    """A Whisper model loaded once and reused for every dictation."""

    def __init__(self, cfg) -> None:
        backend = str(cfg.require("stt.backend"))
        if backend not in SUPPORTED_BACKENDS:
            raise STTUnavailable(f"stt.backend {backend!r} is not implemented "
                                 f"(have: {', '.join(SUPPORTED_BACKENDS)})")
        self.backend = backend
        self.model_id = str(cfg.require("stt.model"))
        self.language = cfg.require("stt.language")
        self.compute = str(cfg.require("stt.compute"))
        self.quantization = str(cfg.require("stt.quantization"))
        self.keep_resident = bool(cfg.require("stt.keep_resident"))
        self._model_path: str | None = None
        self.load_ms: float | None = None

    # -- loading ---------------------------------------------------------------------

    def _resolve_local(self) -> str:
        """Local snapshot directory for `stt.model`, downloading only if it is not cached."""
        from huggingface_hub import snapshot_download

        try:
            return snapshot_download(repo_id=self.model_id, local_files_only=True)
        except Exception:
            log.warning("stt: %s not in the local cache — downloading weights once", self.model_id)
            return snapshot_download(repo_id=self.model_id)

    def load(self) -> float:
        """Load the weights and run one silent warm-up pass. Returns wall ms for both.

        The warm-up exists because MLX compiles its Metal kernels lazily: without it the
        first real dictation pays that cost inside release-to-text.
        """
        try:
            import mlx.core as mx
            import numpy as np
            from mlx_whisper.audio import SAMPLE_RATE
            tr = _transcribe_module()
        except ImportError as exc:
            raise STTUnavailable(f"mlx-whisper is not installed ({exc}); run with "
                                 f"`uv run --extra app ...`") from exc

        if self.compute == "cpu":
            mx.set_default_device(mx.cpu)
        elif self.compute == "metal":
            mx.set_default_device(mx.gpu)
        else:
            raise STTUnavailable(f"stt.compute {self.compute!r} is not a mlx device (metal|cpu)")

        t0 = time.perf_counter()
        self._model_path = self._resolve_local()
        dtype = mx.float16 if self.quantization == "fp16" else mx.float32
        tr.ModelHolder.get_model(self._model_path, dtype)
        self._run(np.zeros(SAMPLE_RATE, dtype=np.float32))  # one second of silence
        self.load_ms = (time.perf_counter() - t0) * 1000
        return self.load_ms

    @property
    def sample_rate(self) -> int:
        """The rate the model consumes. A property of Whisper, not a tunable."""
        from mlx_whisper.audio import SAMPLE_RATE
        return int(SAMPLE_RATE)

    # -- inference -------------------------------------------------------------------

    def _run(self, audio) -> str:
        import mlx_whisper

        out = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._model_path,
            language=self.language,
            fp16=(self.quantization == "fp16"),
            verbose=None,
        )
        return str(out.get("text", ""))

    def transcribe(self, audio) -> tuple[str, float]:
        """float32 mono samples at `sample_rate` -> (text, wall ms).

        Leading/trailing whitespace is stripped (Whisper prefixes a space to every
        segment); every other character is returned exactly as the model produced it.
        """
        if self._model_path is None:
            self.load()
        t0 = time.perf_counter()
        text = self._run(audio)
        ms = (time.perf_counter() - t0) * 1000
        if not self.keep_resident:
            self.unload()
        return text.strip(), ms

    def unload(self) -> None:
        tr = _transcribe_module()
        tr.ModelHolder.model = None
        tr.ModelHolder.model_path = None
        self._model_path = None


def _transcribe_module():
    """`mlx_whisper.transcribe` the MODULE (the package re-exports a function by that name,
    so `from mlx_whisper import transcribe` gets the function, not `ModelHolder`)."""
    import importlib

    return importlib.import_module("mlx_whisper.transcribe")


def read_wav(path) -> tuple["object", int]:
    """16-bit PCM WAV -> (float32 mono samples in [-1, 1], sample rate). Stdlib + numpy.

    Deliberately not mlx-whisper's `load_audio`, which shells out to ffmpeg (not installed
    here, and a subprocess on the hot path for no gain when the input is already PCM).
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: {8 * width}-bit WAV; need 16-bit PCM")
    a = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    return a, rate
