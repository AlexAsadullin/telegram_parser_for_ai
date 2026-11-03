"""
Local speech-to-text for voice messages and round video notes.

Two backends, because the right one depends on the machine:

  * mlx      — Apple MLX, runs on the GPU of M-series chips. Measured on an
               M2 Air: 30s of Russian speech in 1.6s (~19x realtime), about
               2.6x faster than the CPU path.
  * faster-whisper — CTranslate2 on CPU. The portable fallback. It cannot use
               an Apple GPU at all: the package ships without CUDA and
               CTranslate2 has no Metal backend, so "mps"/"metal" are rejected
               outright.

"auto" (the default) picks mlx when it imports and falls back to CPU.

Audio is always decoded with PyAV, including for the MLX backend whose own
loader would otherwise shell out to an ffmpeg binary. That keeps ffmpeg out of
the dependency list and lets round video notes (.mp4) be read directly — only
the audio stream is taken, the video is ignored.
"""
import asyncio
import logging
import threading
from pathlib import Path

from .config import (
    WHISPER_BACKEND,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

_model = None
_backend: str | None = None
_model_lock = threading.Lock()


def _mlx_repo(model: str) -> str:
    """Map a plain size name to the MLX community repo that hosts it."""
    if "/" in model:
        return model  # already an explicit repo
    return f"mlx-community/whisper-{model}-mlx"


def _mlx_available() -> bool:
    try:
        import mlx_whisper  # noqa: F401
        import mlx.core as mx

        return mx.default_device().type == mx.DeviceType.gpu
    except Exception:
        return False


def _resolve_backend() -> str:
    """Decide which engine to use. Called once, under the load lock."""
    if WHISPER_BACKEND == "mlx":
        return "mlx"
    if WHISPER_BACKEND == "faster-whisper":
        return "faster-whisper"

    # auto
    if _mlx_available():
        return "mlx"
    logger.info("MLX unavailable — falling back to faster-whisper on CPU")
    return "faster-whisper"


def _load() -> tuple[str, object]:
    """Load the model once. Safe to call from several threads."""
    global _model, _backend
    if _model is not None:
        return _backend, _model

    with _model_lock:
        if _model is not None:  # another thread won the race
            return _backend, _model

        backend = _resolve_backend()

        if backend == "mlx":
            import mlx_whisper

            repo = _mlx_repo(WHISPER_MODEL)
            logger.info("Using MLX backend on Apple GPU, model %r", repo)
            # MLX loads lazily on first transcribe and caches internally;
            # the repo id is all we need to carry around.
            _model = repo
        else:
            from faster_whisper import WhisperModel

            logger.info(
                "Using faster-whisper on %s (compute_type=%s), model %r",
                WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, WHISPER_MODEL,
            )
            _model = WhisperModel(
                WHISPER_MODEL,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )

        _backend = backend
        return _backend, _model


def _decode(path: str):
    """
    Decode any container (.ogg/opus voice, .mp4 round notes) to mono
    16 kHz float32 via PyAV.

    This is a local copy of faster_whisper.audio.decode_audio with one
    difference: `metadata_errors="ignore"` was added to av.open() in PyAV 15,
    and faster-whisper 1.2.x passes it unconditionally. On an older PyAV (e.g.
    14.x, pulled in transitively or left over from a previous install) every
    transcription died with "open() got an unexpected keyword argument
    'metadata_errors'". Probing the signature once keeps us compatible with
    both, so no media file is ever lost to a version mismatch.
    """
    import gc
    import io
    import itertools

    import av
    import numpy as np

    def _ignore_invalid_frames(frames):
        iterator = iter(frames)
        while True:
            try:
                yield next(iterator)
            except StopIteration:
                break
            except av.error.InvalidDataError:
                continue

    def _group_frames(frames, num_samples=None):
        fifo = av.audio.fifo.AudioFifo()
        for frame in frames:
            frame.pts = None  # Ignore timestamp check.
            fifo.write(frame)
            if num_samples is not None and fifo.samples >= num_samples:
                yield fifo.read()
        if fifo.samples > 0:
            yield fifo.read()

    def _resample_frames(frames, resampler):
        for frame in itertools.chain(frames, [None]):
            yield from resampler.resample(frame)

    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=SAMPLE_RATE,
    )

    raw_buffer = io.BytesIO()
    dtype = None

    open_kwargs = {}
    if _AV_SUPPORTS_METADATA_ERRORS:
        open_kwargs["metadata_errors"] = "ignore"

    with av.open(path, mode="r", **open_kwargs) as container:
        frames = container.decode(audio=0)
        frames = _ignore_invalid_frames(frames)
        frames = _group_frames(frames, 500000)
        frames = _resample_frames(frames, resampler)

        for frame in frames:
            array = frame.to_ndarray()
            dtype = array.dtype
            raw_buffer.write(array)

    # The resampler leaks unless the GC is run manually
    # (faster-whisper issue #390).
    del resampler
    gc.collect()

    audio = np.frombuffer(raw_buffer.getbuffer(), dtype=dtype)
    return audio.astype(np.float32) / 32768.0


def _av_supports_metadata_errors() -> bool:
    try:
        import inspect

        import av
        # PyAV is a compiled extension; its open() exposes the accepted
        # keywords through __doc__ rather than an introspectable signature.
        doc = getattr(av.open, "__doc__", "") or ""
        if "metadata_errors" in doc:
            return True
        sig = inspect.signature(av.open)
        if "metadata_errors" in sig.parameters:
            return True
        # Anything **kwargs-ish cannot be probed reliably — assume modern.
        return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    except Exception:
        return False


_AV_SUPPORTS_METADATA_ERRORS = _av_supports_metadata_errors()


def _transcribe_sync(path: str) -> str:
    backend, model = _load()
    audio = _decode(path)

    if backend == "mlx":
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=model, language=WHISPER_LANGUAGE, verbose=None
        )
        return (result.get("text") or "").strip()

    segments, _info = model.transcribe(audio, language=WHISPER_LANGUAGE, beam_size=1)
    # `segments` is a generator; consuming it is what actually runs inference.
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe(path: Path) -> str | None:
    """
    Transcribe an audio or video file to text.

    Accepts Telegram voice messages (.ogg/opus) and round video notes (.mp4).
    Returns None if recognition fails or yields nothing.
    """
    try:
        # Inference is blocking and compute-bound: keep it off the event loop so
        # progress updates and websockets stay responsive.
        text = await asyncio.to_thread(_transcribe_sync, str(path))
    except Exception as exc:
        logger.warning("Transcription failed for %s: %s", path.name, exc)
        return None

    if not text:
        logger.info("Transcription produced no text for %s", path.name)
        return None
    return text


async def warm_up() -> None:
    """
    Load the model before the first voice message needs it.

    Called at the start of a DM run so the one-off load cost is paid while the
    UI is already showing progress, instead of stalling the first transcription.
    """
    try:
        await asyncio.to_thread(_load)
    except Exception as exc:
        logger.warning("Model warm-up failed: %s", exc)


def model_info() -> dict:
    """Describe the configured engine without forcing the model to load."""
    backend = _backend or ("mlx" if (WHISPER_BACKEND in ("auto", "mlx") and _mlx_available())
                           else "faster-whisper")
    on_gpu = backend == "mlx"
    return {
        "backend": backend,
        "model": _mlx_repo(WHISPER_MODEL) if on_gpu else WHISPER_MODEL,
        "device": "Apple GPU (MLX)" if on_gpu else WHISPER_DEVICE,
        "gpu": on_gpu,
        "compute_type": "float16" if on_gpu else WHISPER_COMPUTE_TYPE,
        "language": WHISPER_LANGUAGE,
        "loaded": _model is not None,
    }
