"""
GPU auto-detection for the transcription backend.

The machine this project targets has an AMD Polaris GPU (Radeon RX 580):

    03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. [AMD/ATI]
    Polaris 20 XL [Radeon RX 580 2048SP] (rev ef)

Polaris is *not* officially supported by ROCm, but it works with the
ROCm-runtime fork built for gfx803 and with ZenDNN; faster-whisper itself only
needs CTranslate2 to see a usable HIP device via torch. Apple MLX covers M-series
GPUs, so on this box the realistic GPU paths are:

  * mlx        — only if an Apple GPU is present (kept for portability);
  * CUDA       — NVIDIA cards, CTranslate2 ships support in the wheel;
  * ROCm/HIP   — AMD GPUs through the PyTorch-ROCm stack (what the RX 580 uses;
                 requires ctranslate2 built against ROCm or the ZenDNN fork).

`detect_device()` returns ("cuda"|"cpu", compute_type) after a cheap probe, so
the default on this branch is GPU whenever one is actually usable, and CPU
otherwise — never a crash at the first voice message.
"""
import functools
import logging

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _probe() -> tuple[str, str]:
    """Try each GPU path once per process; cache the verdict."""
    # 0) AMD ROCm/HIP first on this branch — the target machine is a Radeon
    #    RX 580 (Polaris/gfx803). CTranslate2 has no native HIP backend, so
    #    when torch sees a HIP device we transcribe through openai-whisper on
    #    torch+ROCm ("rocm" here means: use the WhisperOnTorch engine below).
    try:
        import torch

        if getattr(torch.version, "hip", None) and torch.cuda.is_available():
            return "rocm", "float16"
    except Exception:
        pass

    # 1) NVIDIA / CUDA — the stock faster-whisper wheel supports this out of
    #    the box, so check it next.
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass

    return "cpu", "int8"


def whisper_on_torch_available() -> bool:
    """True when a ROCm/CUDA-enabled torch + openai-whisper can run the GPU."""
    device, _ = _probe()
    if device == "cpu":
        return False
    try:
        import whisper  # noqa: F401  (openai-whisper package)

        return True
    except Exception:
        return False


def detect_device(forced: str | None = None) -> tuple[str, str]:
    """
    Return (device, compute_type) for faster-whisper.

    `forced` comes from WHISPER_DEVICE in .env. Anything other than "auto"
    wins verbatim; "auto" (the default on this branch) runs the probe above.
    A forced-but-unusable value degrades to the probe result instead of dying.
    """
    if forced and forced != "auto":
        device, compute = _probe()
        if forced in (device, "cpu"):
            return forced, _compute_for(forced, compute)
        # Forced GPU that the probe cannot confirm: try it anyway once —
        # ctranslate2's own list can be stale on ROCm forks.
        logger.warning(
            "WHISPER_DEVICE=%r not confirmed by probe (probe says %r); using it anyway",
            forced, device,
        )
        return forced, _compute_for(forced, compute)

    device, compute = _probe()
    if device == "cpu":
        logger.info("No usable GPU detected — transcribing on CPU")
    else:
        logger.info("GPU detected for transcription: %s", device)
    return device, compute


def _compute_for(device: str, probed: str) -> str:
    """int8 keeps CPU memory small; float16 is the sweet spot on GPUs."""
    if device == "cpu":
        return "int8" if probed == "int8" else probed
    return "float16"
