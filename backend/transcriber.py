"""
Local speech-to-text for voice messages and round video notes.

Three engines; the right one depends on the machine:

  * mlx            — Apple MLX on the GPU of M-series chips. Measured on an
                     M2 Air: 30 s of Russian speech in 1.6 s.
  * whispercpp     — whisper.cpp (pywhispercpp binding) with the Vulkan
                     backend. This is the GPU path for AMD cards, including
                     the Radeon RX 580 (Polaris) that ROCm no longer supports.
                     Measured on an RX 580: 46 s of speech in ~3 s.
  * faster-whisper — CTranslate2. CUDA on NVIDIA, otherwise CPU int8.

"auto" (the default) tries them in that order. whisper.cpp is only kept when
it really attached to a GPU; if Vulkan initialisation falls back to the CPU,
faster-whisper takes over because its int8 CPU path is faster than
whisper.cpp's.

Audio is always decoded with PyAV, for every engine. That keeps ffmpeg out of
the dependency list and lets round video notes (.mp4) be read directly — only
the audio stream is taken, the video is ignored.
"""
import asyncio
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

from . import gpu_detect
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
_device_in_use: str = "cpu"
_compute_in_use: str = "int8"
_model_lock = threading.Lock()
# whisper.cpp contexts are not thread-safe; serialise inference on them.
_infer_lock = threading.Lock()

_CPU_THREADS = max(1, min(8, os.cpu_count() or 4))


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


def _whispercpp_importable() -> bool:
    try:
        import pywhispercpp.model  # noqa: F401

        return True
    except Exception:
        return False


def _load_whispercpp():
    """Load whisper.cpp and report which device it attached to.

    Returns (model, device_label). device_label is None when whisper.cpp ended
    up on the CPU — the caller then prefers faster-whisper instead.
    """
    from pywhispercpp.model import Model

    fd, log_path = tempfile.mkstemp(prefix="whispercpp_", suffix=".log")
    os.close(fd)
    try:
        model = Model(
            WHISPER_MODEL,
            redirect_whispercpp_logs_to=log_path,
            print_progress=False,
            print_realtime=False,
            print_timestamps=False,
            n_threads=_CPU_THREADS,
        )
        with open(log_path, errors="replace") as fh:
            log = fh.read()
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass

    # whisper.cpp logs e.g.
    #   ggml_vulkan: 0 = AMD Radeon RX 580 2048SP (RADV POLARIS10) (radv) | ...
    #   whisper_backend_init_gpu: using Vulkan0 backend
    used = re.search(r"using (\w+?)(\d*) backend", log)
    if not used or used.group(1).upper() == "CPU":
        return model, None
    name = re.search(r"ggml_vulkan: \d+ = ([^|(]+)", log)
    label = f"{used.group(1)} ({name.group(1).strip()})" if name else used.group(1)
    return model, label


def _load_faster_whisper():
    from faster_whisper import WhisperModel

    if WHISPER_DEVICE in ("cpu", "cuda"):
        device = WHISPER_DEVICE
    else:
        device = "cuda" if gpu_detect.cuda_available() else "cpu"
    if WHISPER_COMPUTE_TYPE not in ("", "auto"):
        compute = WHISPER_COMPUTE_TYPE
    else:
        compute = "float16" if device == "cuda" else "int8"

    logger.info(
        "Using faster-whisper on %s (compute_type=%s), model %r",
        device, compute, WHISPER_MODEL,
    )
    try:
        model = WhisperModel(
            WHISPER_MODEL, device=device, compute_type=compute,
            cpu_threads=_CPU_THREADS,
        )
    except Exception as exc:
        if device == "cpu":
            raise
        logger.warning("CUDA init failed (%s) — falling back to CPU int8", exc)
        device, compute = "cpu", "int8"
        model = WhisperModel(
            WHISPER_MODEL, device=device, compute_type=compute,
            cpu_threads=_CPU_THREADS,
        )
    return model, device, compute


def _load() -> tuple[str, object]:
    """Load the model once. Safe to call from several threads."""
    global _model, _backend, _device_in_use, _compute_in_use
    if _model is not None:
        return _backend, _model

    with _model_lock:
        if _model is not None:  # another thread won the race
            return _backend, _model

        choice = WHISPER_BACKEND
        if choice == "auto":
            if _mlx_available():
                choice = "mlx"
            elif _whispercpp_importable() and gpu_detect.vulkan_gpu_likely():
                choice = "whispercpp"
            else:
                choice = "faster-whisper"

        if choice == "mlx":
            repo = _mlx_repo(WHISPER_MODEL)
            logger.info("Using MLX backend on Apple GPU, model %r", repo)
            # MLX loads lazily on first transcribe; the repo id is enough.
            _model, _backend = repo, "mlx"
            _device_in_use, _compute_in_use = "Apple GPU (MLX)", "float16"
            return _backend, _model

        if choice == "whispercpp":
            try:
                model, label = _load_whispercpp()
            except Exception as exc:
                logger.warning("whisper.cpp failed to load (%s) — using faster-whisper", exc)
                model, label = None, None
            if label is not None or (model is not None and WHISPER_BACKEND == "whispercpp"):
                logger.info(
                    "Using whisper.cpp on %s, model %r",
                    label or "CPU", WHISPER_MODEL,
                )
                _model, _backend = model, "whispercpp"
                _device_in_use = label or "cpu"
                _compute_in_use = "float16" if label else "float32"
                return _backend, _model
            if model is not None:
                logger.warning(
                    "whisper.cpp did not attach to a GPU (Vulkan) — "
                    "faster-whisper on CPU is quicker, switching to it",
                )
                del model

        model, device, compute = _load_faster_whisper()
        _model, _backend = model, "faster-whisper"
        _device_in_use, _compute_in_use = device, compute
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

    if backend == "whispercpp":
        with _infer_lock:
            segments = model.transcribe(audio, language=WHISPER_LANGUAGE)
        return " ".join(s.text.strip() for s in segments).strip()

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


def _preview() -> tuple[str, str, bool, str]:
    """Predict (backend, device, gpu, compute) without loading a model."""
    choice = WHISPER_BACKEND
    if choice in ("auto", "mlx") and _mlx_available():
        return "mlx", "Apple GPU (MLX)", True, "float16"
    if choice in ("auto", "whispercpp") and _whispercpp_importable():
        if gpu_detect.vulkan_gpu_likely():
            name = gpu_detect.gpu_name() or "GPU"
            return "whispercpp", f"Vulkan ({name})", True, "float16"
        if choice == "whispercpp":
            return "whispercpp", "cpu", False, "float32"
    if WHISPER_DEVICE == "cuda" or (WHISPER_DEVICE != "cpu" and gpu_detect.cuda_available()):
        return "faster-whisper", "cuda", True, "float16"
    compute = WHISPER_COMPUTE_TYPE if WHISPER_COMPUTE_TYPE not in ("", "auto") else "int8"
    return "faster-whisper", "cpu", False, compute


def model_info() -> dict:
    """Describe the configured engine without forcing the model to load."""
    if _model is not None:  # loaded — report what actually won
        backend, device, compute = _backend, _device_in_use, _compute_in_use
        gpu = device != "cpu"
    else:
        backend, device, gpu, compute = _preview()
    return {
        "backend": backend,
        "model": _mlx_repo(WHISPER_MODEL) if backend == "mlx" else WHISPER_MODEL,
        "device": device,
        "gpu": gpu,
        "compute_type": compute,
        "language": WHISPER_LANGUAGE,
        "loaded": _model is not None,
    }
