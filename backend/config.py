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

# Ensure parent directories exist
Path(SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
