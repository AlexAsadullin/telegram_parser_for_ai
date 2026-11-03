import os
from pathlib import Path

from dotenv import load_dotenv

# Project layout:
#   PROJECT_ROOT/       <- relative paths in .env are resolved from here
#       .env
#       data/
#       backend/        <- BACKEND_DIR
#           config.py
#           session/
#           history.db
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")


def _resolve(env_value: str) -> Path:
    """Return absolute path: env value as-is if absolute, else relative to PROJECT_ROOT."""
    p = Path(env_value)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


API_ID: int = int(os.environ["TG_API_ID"])
API_HASH: str = os.environ["TG_API_HASH"]

SESSION_PATH: str = str(_resolve(os.getenv("TG_SESSION", "backend/session/anon")))
DATA_DIR: Path = _resolve(os.getenv("DATA_DIR", "data"))
DB_PATH: str = str(_resolve(os.getenv("DB_PATH", "backend/history.db")))

# --- Local speech recognition (private-message parsing) ---------------------
# "small" keeps Russian accurate at ~460 MB on disk; "base" is ~140 MB but
# garbles terms.
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "small")
WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "ru")

# Engine: on the `mac` branch the default is "mlx" — Apple MLX runs Whisper
# on the GPU of M-series chips. Set WHISPER_BACKEND=auto (or "faster-whisper")
# in .env to force the CTranslate2 path instead; auto still prefers MLX here.
WHISPER_BACKEND: str = os.getenv("WHISPER_BACKEND", "mlx")

# Device for the faster-whisper backend. "mps" maps to the Apple GPU through
# MLX; for the CTranslate2 fallback use "cpu" (default here) or "auto" to run
# the CUDA/ROCm probe from gpu_detect.py.
WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "mps")
# Compute type override; "auto" picks float16 on GPU, int8 on CPU.
WHISPER_COMPUTE_TYPE: str = os.getenv("WHISPER_COMPUTE_TYPE", "auto")

# Messages are pulled and processed in chunks so a long chat never has to fit
# in memory all at once.
DM_CHUNK_SIZE: int = int(os.getenv("DM_CHUNK_SIZE", "200"))

# Ensure parent directories exist
Path(SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
