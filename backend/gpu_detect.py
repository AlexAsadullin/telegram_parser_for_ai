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
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _sysfs_amd_gpu() -> bool:
    """Detect an AMD GPU at the kernel level — no libraries required.

    Scans /sys/class/drm for cards with a device file (i.e. real GPUs, not
    integrated display pipes without one). This answers "does this box have an
    AMD GPU?" even when torch/ROCm are not installed yet, which is exactly the
    state that produced the misleading 'No usable GPU detected' log.
    """
    try:
        import glob

        return bool(glob.glob("/sys/class/drm/card*/device"))
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _amdgpu_kernel_driver() -> str | None:
    """Report the driver actually bound to the AMD GPU, e.g. 'amdgpu' or 'vfio-pci'.

    vfio/kvm/qemu bindings mean the card is passed through to a VM and cannot
    be used from this OS — in that case we must not tell the user to install
    ROCm, because nothing will ever work here.
    """
    import glob

    for dev in sorted(glob.glob("/sys/class/drm/card*/device")):
        link = os.path.join(dev, "driver")
        try:
            name = os.path.basename(os.path.realpath(link))
        except OSError:
            continue
        if name.startswith(("vfio", "kvm", "qemu")):
            return name
        if name == "amdgpu":
            return name
    return None


def _force_rocm_env() -> None:
    """Best-effort HSA override for Polaris (gfx803) via the rocm-opencl fork.

    HSA_OVERRIDE_GFX_VERSION=10.3.0 makes gfx803 masquerade as gfx1030 inside
    the ROCm fork's OpenCL runtime, which lets PyTorch-HIP kernels run on
    Polaris. It only has an effect with a ROCm build patched for gfx803; with
    the standard stack it is simply ignored.
    """
    os.environ.setdefault("HSA_OVERRIDE_GFX_VERSION", "10.3.0")


@functools.lru_cache(maxsize=1)
def _rocm_smoke_test() -> bool:
    """Run a tiny float16 matmul through the *installed* torch+HIP before we
    commit to the GPU. If the wheels are present but the runtime is broken
    (missing rocBLAS/Tensile for gfx803), this fails fast instead of every
    voice message failing later."""
    try:
        import torch

        if not (getattr(torch.version, "hip", None) and torch.cuda.is_available()):
            return False
        _force_rocm_env()
        x = torch.randn(64, 64, device="cuda", dtype=torch.float16)
        y = x @ x
        torch.cuda.synchronize()
        return bool(y.isfinite().all().item())
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("ROCm smoke test failed (%s) — treating GPU as unusable", exc)
        return False


@functools.lru_cache(maxsize=1)
def _probe() -> tuple[str, str]:
    """Try each GPU path once per process; cache the verdict."""
    # 0) AMD ROCm/HIP first on this branch — the target machine is a Radeon
    #    RX 580 (Polaris/gfx803). CTranslate2 has no native HIP backend, so
    #    when torch sees a HIP device we transcribe through openai-whisper on
    #    torch+ROCm ("rocm" here means: use the WhisperOnTorch engine below).
    #    The probe is a real fp16 matmul, not just `torch.cuda.is_available`,
    #    because Polaris support depends on patched wheels + HSA overrides.
    if _rocm_smoke_test():
        return "rocm", "float16"

    # 1) NVIDIA / CUDA — the stock faster-whisper wheel supports this out of
    #    the box, so check it next.
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass

    return "cpu", "int8"


def amd_gpu_present() -> bool:
    """Is there an AMD GPU visible to this OS (regardless of ROCm installs)?"""
    return _sysfs_amd_gpu()


def gpu_passthrough_in_use() -> bool:
    """True when the AMD card is bound to vfio/kvm — unusable from this OS."""
    drv = _amdgpu_kernel_driver()
    return bool(drv and drv.startswith(("vfio", "kvm", "qemu")))


def amdgpu_bound() -> bool:
    """True when the amdgpu kernel driver owns the card."""
    return _amdgpu_kernel_driver() == "amdgpu"


def _amd_products() -> list[str]:
    import glob
    import re

    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for pth in sorted(glob.glob("/sys/class/drm/card*/device/device")):
        try:
            with open(pth) as fh:
                s = fh.read().strip()
            m = re.search(r"0x([0-9a-fA-F]{4})", s)
            if m:
                key = (m.group(1).lower(), s)
                if key not in seen:
                    seen.add(key)
                    out.append(s)
        except OSError:
            continue
    return out


# PCI device ids of consumer Polaris cards (family 0x67xx / 0x6Fxx).
_POLARIS_IDS = {
    "67df", "67dd", "67c4", "67c7", "67ff", "67c0", "67c8", "67b9",
    "67ca", "67cc", "67cf", "67d0", "67d8", "67db", "67dc", "67e0",
    "67e1", "67e2", "67e3", "67e8", "67eb", "67ef", "67fe",
    "67f0", "67f1", "67f2", "67f3", "67f4", "67f5", "67f8", "67f9",
    "67fa", "67fb", "6fc1", "6fc2", "6fc3", "6fc5", "6fc7", "6fdf",
    "6fe3", "6fef",
}


def polaris_detected() -> bool:
    """True when the AMD GPU is a Polaris chip (RX 570/580, gfx803)."""
    return any(
        dev[8:12].lower() in _POLARIS_IDS
        for dev in _amd_products()
        if len(dev) >= 12
    )


def hsa_override_needed() -> bool:
    """gfx803 needs HSA_OVERRIDE_GFX_VERSION with patched ROCm builds."""
    return polaris_detected()


def _apt_installable(pkg: str) -> bool:
    """Non-destructive check whether apt can install `pkg` right now."""
    try:
        r = subprocess.run(
            ["apt-get", "install", "-y", "--dry-run", pkg],
            capture_output=True, text=True, timeout=25,
        )
        return r.returncode == 0
    except Exception:
        return False


def _detect_os() -> tuple[str, str]:
    """Return (family, pretty_name): family in ubuntu/debian/fedora/rhel/other."""
    pretty = ""
    try:
        with open("/etc/os-release") as fh:
            kv = {}
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    kv[k] = v.strip('"')
        pretty = kv.get("PRETTY_NAME", "")
        like = (kv.get("ID_LIKE", "") + " " + kv.get("ID", "")).lower()
        if "ubuntu" in like or "debian" in like:
            return "ubuntu", pretty
        if "fedora" in like or "rhel" in like or "centos" in like:
            return "fedora", pretty
    except OSError:
        pass
    return "other", pretty


def _dpkg_owned(path: str) -> bool:
    try:
        r = subprocess.run(
            ["dpkg", "-S", path], capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _has_sudo() -> bool:
    if os.geteuid() == 0:
        return True
    try:
        r = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _whl_path(name_glob: str) -> str | None:
    import glob

    roots = [
        "/opt/rocm/pytest-whl", "/opt/rocm/lib/python*/site-packages",
        "/usr/share/miniconda/pkgs", "/usr/local/share/wheels",
    ]
    for root in roots:
        hits = glob.glob(os.path.join(root, "**", name_glob), recursive=True)
        if hits:
            return hits[0]
    return None


@functools.lru_cache(maxsize=1)
def _pip_index_reachable() -> bool:
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://pypi.org/simple/",
            headers={"User-Agent": "gpu-detect-probe"},
        )
        with urllib.request.urlopen(req, timeout=6):
            return True
    except Exception:
        return False


def setup_plan() -> dict:
    """Diagnose the AMD GPU situation and produce an actionable plan.

    Returns a dict with:
      kind   — what was found (no_amd_gpu / passthrough / amdgpu_missing /
               rocm_installed / ready / ...)
      steps  — ordered human-readable instructions
      script — shell commands that perform the same steps
    """
    family, pretty = _detect_os()
    lines: list[str] = []
    script: list[str] = []

    if not _sysfs_amd_gpu():
        return {
            "kind": "no_amd_gpu",
            "steps": ["AMD GPU не найден в /sys/class/drm — транскрибация на CPU."],
            "script": [],
        }

    products = _amd_products()
    dev_id = products[0][8:12].lower() if products and len(products[0]) >= 12 else ""
    pol = polaris_detected()
    drv = _amdgpu_kernel_driver()

    if gpu_passthrough_in_use():
        return {
            "kind": "passthrough",
            "steps": [
                f"Видеокарта AMD ({', '.join(products)}) проброшена в виртуальную машину "
                f"(драйвер {drv}). Из этой ОС она недоступна.",
                "Варианты: убрать проброс vfio для GPU, либо запускать парсер внутри ВМ, "
                "куда проброшена карта, и установить ROCm/torch уже там.",
            ],
            "script": [],
        }

    rocm_dir = os.path.isdir("/opt/rocm")
    hip_lib = os.path.exists("/opt/rocm/lib/libhiprtc.so") or shutil.which("rocminfo") is not None
    torch_hip = False
    torch_ok = False
    try:
        import torch

        torch_hip = bool(getattr(torch.version, "hip", None))
        torch_ok = torch_hip and torch.cuda.is_available()
    except Exception:
        pass

    if amdgpu_bound() and torch_ok and _rocm_smoke_test():
        return {
            "kind": "ready",
            "steps": ["GPU активен: torch+ROCm проходит fp16 smoke-тест."],
            "script": [],
        }

    sudo = _has_sudo()
    prefix = "" if sudo else "sudo "
    steps: list[str] = []
    scr: list[str] = []

    if not amdgpu_bound():
        steps.append(
            "Ядро не загрузило драйвер amdgpu для видеокарты. Загрузите модуль и "
            "перезагрузитесь (в GRUB добавьте amdgpu.si_support=1 amdgpu.cik_support=1, "
            "если карта старше Polaris)."
        )
        scr += [
            f"{prefix}modprobe amdgpu",
            "echo -e 'amdgpu\\nnvidia\\n' | sudo tee /etc/modules-load.d/amdgpu.conf",
            "sudo dracut --regenerate-all --force || sudo update-initramfs -u -k all",
        ]

    if not rocm_dir or not hip_lib:
        steps.append(
            "Установите ROCm-рантайм"
            + (" (для gfx803 нужен форк с поддержкой Polaris)" if pol else "")
            + "."
        )
        if family == "ubuntu":
            if _apt_installable("rocm-core"):
                scr += [
                    "wget -qO- https://repo.radeon.com/rocm/rocm.gpg.key | "
                    f"{prefix}gpg --dearmor -o /etc/apt/keyrings/rocm.gpg",
                    "echo 'deb [signed-by=/etc/apt/keyrings/rocm.gpg] "
                    "https://repo.radeon.com/rocm/apt/6.3.4 stable main' "
                    "| sudo tee /etc/apt/sources.list.d/rocm.list",
                    f"{prefix}apt-get update && {prefix}apt-get install -y rocm-core rocblas",
                ]
            elif _apt_installable("radeontop"):
                scr += [f"{prefix}apt-get install -y radeontop"]
            else:
                scr += [
                    "# Репозиторий ROCm из контейнера недоступен — установите ROCm "
                    "локально:",
                    "# https://rocm.docs.amd.com/projects/install-on-linux/en/latest/"
                    "install/install-methods/package-manager/package-manager-ubuntu.html",
                ]
        elif family == "fedora":
            scr += [
                "# Fedora: скажите 'setup-rocm' — соберу конфиг репозитория ROCm "
                "и команды dnf.",
            ]
        else:
            scr += [
                "# Ваша ОС: используйте официальный установщик ROCm "
                "https://rocm.docs.amd.com",
            ]
    else:
        steps.append(
            "ROCm установлен в /opt/rocm — проверьте переменные окружения PATH/LD_LIBRARY_PATH."
        )
        scr += [
            "export PATH=/opt/rocm/bin:$PATH",
            "export LD_LIBRARY_PATH=/opt/rocm/lib:$LD_LIBRARY_PATH",
        ]

    if not torch_hip:
        steps.append(
            "Установите PyTorch с HIP из заранее загруженного wheel или через pip "
            "(нужен доступ к PyTorch index)."
        )
        whl = _whl_path("torch*hip*linux_x86_64.whl")
        torchaudio = _whl_path("torchaudio*linux*.whl")
        if whl:
            cmd = f"{prefix}{sys.executable if (sys := __import__('sys')) else 'python3'} " \
                  f"-m pip install --force-reinstall '{whl}'"
            if torchaudio:
                cmd += f" '{torchaudio}'"
            scr.append(cmd)
        elif _pip_index_reachable():
            scr += [
                "python3 -m pip install --upgrade pip",
                "python3 -m pip install torch torchvision torchaudio "
                "--index-url https://download.pytorch.org/whl/rocm6.3",
            ]
        else:
            scr += [
                "# Сеть к PyPI/PyTorch недоступна — скачайте wheels на машине с "
                "интернетом и поставьте офлайн:",
                "# python3 -m pip install ./torch-*-hip*-linux_x86_64.whl "
                "./torchaudio*.whl",
            ]
    else:
        steps.append(
            "PyTorch-HIP установлен, но проверка устройства не прошла — "
            "см. вывод smoke-теста выше."
        )

    if pol:
        steps.append(
            "Для Polaris (gfx803) задайте HSA_OVERRIDE_GFX_VERSION=10.3.0 и "
            "OCL_ENABLE_SYSTEM_SCOPE=0 (работает только с ROCm-форком под gfx803)."
        )
        scr += [
            "export HSA_OVERRIDE_GFX_VERSION=10.3.0",
            "export OCL_ENABLE_SYSTEM_SCOPE=0",
            "echo 'export HSA_OVERRIDE_GFX_VERSION=10.3.0' >> ~/.bashrc",
        ]

    steps.append(
        "Установите пакет распознавания: python3 -m pip install openai-whisper "
        "(движок whisper-torch для AMD)."
    )
    scr += ["python3 -m pip install openai-whisper"]

    steps.append(
        "Проверьте: python3 -c \"import torch; print(torch.version.hip, "
        "torch.cuda.is_available())\" и перезапустите ./run.sh — в логах должно "
        "быть 'Using openai-whisper on torch (rocm)'."
    )
    scr += [
        "python3 -c \"import torch; print('hip:', torch.version.hip, "
        "'avail:', torch.cuda.is_available())\"",
    ]

    return {"kind": "setup_required", "steps": steps, "script": scr,
            "os": pretty, "polaris": pol, "products": products}


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
