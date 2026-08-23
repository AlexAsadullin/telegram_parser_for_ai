"""
Local speech-to-text for voice messages and round video notes.

Uses faster-whisper (CTranslate2). The model is loaded once, lazily, on the
first transcription and reused for the rest of the process — loading "small"
costs ~25s, so it must not happen per message.

Video notes need no separate audio-extraction step: faster-whisper decodes via
PyAV, which reads the audio stream straight out of the .mp4 container.
"""
import asyncio
import logging
import threading
from pathlib import Path

from .config import (
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def _load_model():
    """Load the Whisper model once. Safe to call from several threads."""
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:  # another thread won the race
            return _model
        from faster_whisper import WhisperModel

        logger.info(
            "Loading Whisper model %r (device=%s, compute_type=%s) — first call only",
            WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
        )
        _model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        logger.info("Whisper model loaded")
        return _model


def _transcribe_sync(path: str) -> str:
    model = _load_model()
    segments, _info = model.transcribe(path, language=WHISPER_LANGUAGE, beam_size=1)
    # `segments` is a generator; consuming it is what actually runs inference.
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe(path: Path) -> str | None:
    """
    Transcribe an audio or video file to text.

    Accepts Telegram voice messages (.ogg/opus) and round video notes (.mp4) —
    for the latter only the audio stream is read, the video is ignored.
    Returns None if recognition fails or yields nothing.
    """
    try:
        # Inference is blocking and CPU-bound: keep it off the event loop so
        # progress updates and websockets stay responsive.
        text = await asyncio.to_thread(_transcribe_sync, str(path))
    except Exception as exc:
        logger.warning("Transcription failed for %s: %s", path.name, exc)
        return None

    if not text:
        logger.info("Transcription produced no text for %s", path.name)
        return None
    return text


def model_info() -> dict:
    """Describe the configured model without forcing it to load."""
    return {
        "model": WHISPER_MODEL,
        "device": WHISPER_DEVICE,
        "compute_type": WHISPER_COMPUTE_TYPE,
        "language": WHISPER_LANGUAGE,
        "loaded": _model is not None,
    }
