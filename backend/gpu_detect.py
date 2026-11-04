"""
GPU detection for the transcription backend.

Which GPU path is realistic depends on the card:

  * Apple M-series   — mlx-whisper (handled in transcriber.py).
  * NVIDIA           — faster-whisper / CTranslate2 with CUDA, out of the box.
  * AMD (and others) — whisper.cpp with the **Vulkan** backend, through the
                       pywhispercpp binding built with GGML_VULKAN=1.

The Vulkan route is what makes old AMD cards such as the Radeon RX 580
(Polaris / gfx803) usable: ROCm dropped gfx803 years ago and current PyTorch
ROCm wheels ship no kernels for it, but Mesa's RADV Vulkan driver supports it
fine. Measured on an RX 580: 46 s of Russian speech in ~3 s with the `small`
model, against ~37 s with faster-whisper int8 on the CPU.

Everything here is cheap and side-effect free: no model is loaded and no GPU
context is created. The final word on whether whisper.cpp got a GPU comes from
its own log at model load time (see transcriber.py).
"""
import functools
import glob
import logging
import os

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def cuda_available() -> bool:
    """NVIDIA GPU visible to CTranslate2 (the faster-whisper engine)."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def whispercpp_vulkan_build() -> bool:
    """pywhispercpp is installed and its extension was compiled with Vulkan.

    The prebuilt PyPI wheel is CPU-only; run.sh rebuilds it from source with
    GGML_VULKAN=1. The Vulkan backend's symbols in the extension tell the two
    apart without initialising anything.
    """
    try:
        import importlib.util

        spec = importlib.util.find_spec("_pywhispercpp")
        if spec is None or not spec.origin:
            return False
        candidates = [spec.origin]
        # A shared-library build keeps ggml backends next to the extension.
        candidates += glob.glob(
            os.path.join(os.path.dirname(spec.origin), "libggml-vulkan*.so*")
        )
        for path in candidates:
            with open(path, "rb") as fh:
                if b"ggml_vulkan" in fh.read():
                    return True
        return False
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def render_node_present() -> bool:
    """A DRM render node exists, i.e. there is a GPU Vulkan could drive."""
    return bool(glob.glob("/dev/dri/renderD*"))


def vulkan_gpu_likely() -> bool:
    """Best guess, before loading a model, that whisper.cpp will use a GPU."""
    return whispercpp_vulkan_build() and render_node_present()


@functools.lru_cache(maxsize=1)
def gpu_name() -> str | None:
    """Human-readable name of the first GPU, from sysfs/lspci-free sources."""
    for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        if "-" in os.path.basename(os.path.dirname(dev)):
            continue  # connector entries like card1-DP-1
        try:
            with open(os.path.join(dev, "vendor")) as fh:
                vendor = fh.read().strip()
        except OSError:
            continue
        return {
            "0x1002": "AMD GPU",
            "0x10de": "NVIDIA GPU",
            "0x8086": "Intel GPU",
        }.get(vendor, "GPU")
    return None
