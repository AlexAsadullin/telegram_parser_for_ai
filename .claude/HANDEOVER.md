# Handover — branch `linux`

Read this before touching transcription, `run.sh` or dependencies on this branch.
Last updated: 2026-11-04.

## What this branch is

`linux` is the project adapted to the owner's **Linux PC with an AMD GPU**. The
code base is shared with the other branches; the difference is `run.sh` (Linux
GPU setup) and the transcription engine selection.

| Branch | Purpose |
|---|---|
| `feature/dm-parsing` | Default/main development branch (PR target) |
| `main` | Hardware-neutral base |
| `mac` | Apple Silicon — MLX as default engine |
| `linux` | **This one** — Linux + AMD Radeon via Vulkan |

### Target machine

- Ubuntu 26.04 LTS, kernel 7.0, Python **3.14** (venv), Node 22
- CPU: Intel Xeon E5-2673 v3 (12c/24t), 16 GB RAM
- GPU: **AMD Radeon RX 580 2048SP** (Polaris 20, gfx803, PCI `1002:6fdf`),
  kernel driver `amdgpu`, Vulkan via Mesa RADV (`RADV POLARIS10`)
- The user has passwordless `sudo`; `/dev/dri/renderD128` is reachable via the
  logind ACL (the user is *not* in the `render`/`video` groups — not needed for
  a desktop session)

## How GPU transcription works (current, working)

Engine order in `backend/transcriber.py` with `WHISPER_BACKEND=auto`:

1. `mlx` — Apple GPU only (irrelevant here)
2. `whispercpp` — `pywhispercpp` built with `GGML_VULKAN=1` → runs on the RX 580
3. `faster-whisper` — CUDA on NVIDIA, otherwise CPU int8 (fallback)

Key behaviours:

- `whispercpp` is chosen when `gpu_detect.vulkan_gpu_likely()` (extension binary
  contains `ggml_vulkan` + a `/dev/dri/renderD*` exists).
- After loading, whisper.cpp's init log (redirected to a temp file) is parsed for
  `using Vulkan0 backend`. If it ended on CPU, the model is dropped and
  faster-whisper is used instead (faster on this CPU). Forcing
  `WHISPER_BACKEND=whispercpp` keeps whisper.cpp even on CPU.
- whisper.cpp inference is serialised with `_infer_lock` (context not
  thread-safe; `dialogs.py` may call `transcribe()` from several threads).
- Audio is always decoded by PyAV (`_decode`), passed as float32 numpy — no ffmpeg.
- Public API used by `dialogs.py`/`main.py` is unchanged: `transcribe(Path)`,
  `warm_up()`, `model_info()`.
- `GET /api/asr-model` → e.g.
  `{"backend":"whispercpp","device":"Vulkan (AMD Radeon RX 580 2048SP)","gpu":true,...}`;
  the frontend badge (`DialogInput.jsx`) shows `⚡` when `gpu` is true.

Measured (46 s Russian speech, model `small`): faster-whisper CPU int8 **37 s**,
whisper.cpp Vulkan **~3 s** (later runs ~1.2 s for 15 s audio).

### Building the GPU engine (`run.sh`)

- Triggered by `./run.sh --with-gpu`, or by an interactive prompt on Linux when a
  render node exists, no NVIDIA card, and no stamp file. Declining writes
  `venv/.gpu-declined` (never asks again).
- apt deps (only if missing): `cmake glslc g++ libvulkan-dev mesa-vulkan-drivers`.
- Build command (keep these flags):
  ```bash
  GGML_VULKAN=1 \
  CMAKE_ARGS="-DBUILD_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DGGML_VULKAN=ON -DGGML_NATIVE=ON" \
  venv/bin/pip install --force-reinstall --no-deps --no-binary pywhispercpp --no-cache-dir pywhispercpp==1.5.1
  ```
  **Static libs are required**: a shared build installs `libwhisper.so.1.8.5`
  but not the `libwhisper.so.1` soname symlink (wheels can't hold symlinks), so
  `import _pywhispercpp` fails with `libwhisper.so.1: cannot open shared object file`.
- Success writes stamp `venv/.gpu-vulkan-1.5.1`. Rebuild:
  `rm venv/.gpu-vulkan-* && ./run.sh --setup-only --with-gpu` (~5 min).
- `pywhispercpp` is intentionally **not** in `requirements.txt` (PyPI wheel is CPU-only).
- ggml models are cached in `~/.local/share/pywhispercpp/models` (`ggml-small.bin`, 466 MB);
  faster-whisper's model is in `~/.cache/huggingface` (464 MB).

## What did NOT work — do not retry

A previous agent (commits `5a9ad6a`, `126f0d1`, `550dc56`) tried ROCm + PyTorch(HIP) +
openai-whisper. It cannot work on this machine:

- ROCm dropped gfx803 (Polaris) long ago; PyTorch-ROCm wheels have no gfx803 kernels.
- `HSA_OVERRIDE_GFX_VERSION=10.3.0` does not help — GCN3 ≠ RDNA2 ISA.
- `amdgpu-install` 6.2 supports only Ubuntu jammy/noble, not 26.04.
- `openai-whisper` in `requirements.txt` pulled **CUDA** torch + ~5 GB of `nvidia-*`.
- Bugs in that code: torch device passed as `"rocm"` (torch uses `"cuda"` for HIP),
  broken Polaris PCI-id parsing, `TG_API_ID=dummy` crashing `int()` in `run.sh`.

All of that was removed in commit `8b69cf7` ("linux: GPU transcription via
whisper.cpp + Vulkan, drop ROCm/torch path"). Never installed: ROCm (`/opt/rocm`
does not exist), no AMD apt repo.

## Environment changes made (2026-11-04)

- apt installed: `cmake`, `cmake-data`, `libvulkan-dev`, `glslc`, `libshaderc1`,
  `spirv-headers`, `libjsoncpp26`, `librhash1`, `vulkan-tools` (~110 MB);
  `espeak-ng` (test only — synthetic Russian speech, removable).
- pip removed from venv: `openai-whisper`, `torch`, `triton`, all `nvidia-*`,
  `numba`, `llvmlite`, `tiktoken`, `sympy`, `mpmath`, `networkx`, `Jinja2`,
  `MarkupSafe`, `regex`, `more-itertools`. venv 6.1 GB → 595 MB; `pip check` clean.
- pip added: `pywhispercpp==1.5.1` (Vulkan build, 59 MB) + `platformdirs`.
- `~/.cache/pip` still holds ~3.1 GB (likely old torch/CUDA wheels) — not purged;
  ask the user before running `venv/bin/pip cache purge`.

## Testing recipes

```bash
# engine status without loading a model
TG_API_ID=0 TG_API_HASH=0 venv/bin/python -c 'from backend import transcriber as t; print(t.model_info())'

# GPU visible to Vulkan?
vulkaninfo --summary | grep deviceName

# fallbacks
WHISPER_BACKEND=faster-whisper ...        # forced CPU engine
GGML_VK_VISIBLE_DEVICES="" ...            # hide GPU → must switch to faster-whisper
```

There are no media files in `data/` (Telegram media is downloaded to a temp dir
and deleted). For a test clip: `espeak-ng -v ru -w x.wav "..."` and pass the
path to `transcriber.transcribe(Path(...))`. Don't start a real DM parse
(`/api/parse-dm`) without asking — it reads the user's private chats and
overwrites `data/dm_*`.

Use `TG_API_ID=0` (not `dummy`) when importing `backend.*` with fake credentials —
`config.py` does `int(os.environ["TG_API_ID"])`.

## Open items / gotchas

- `frontend/package-lock.json` has a local modification that predates this work —
  left uncommitted on purpose; ask the user.
- Git has no `user.name`/`user.email` configured on this machine; commit `8b69cf7`
  was authored with `-c user.name=AlexAsadullin -c user.email=alexasadullin@icloud.com`.
  Older commits are authored by `qwen.ai[bot]`.
- No GitHub credentials (HTTPS remote, no `gh`): `git push` fails from the agent.
  The user pushes manually (`git push origin linux`).
- `run.sh`'s `die()` exits through the EXIT trap → `shutdown` → `exit 0`, so
  failures report exit code 0. Pre-existing; not fixed.
