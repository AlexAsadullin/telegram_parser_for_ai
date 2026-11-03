"""Create the SQLite database and its tables without starting the server.

Used by ``run.sh`` so the database exists right after setup, and can be run
manually from the project root::

    python -m backend.init_db

The same schema is created automatically by the backend on startup (see the
lifespan handler in ``backend.main``); this module is idempotent — safe to run
repeatedly, it never touches existing data.
"""

import asyncio

from . import database
from .config import DB_PATH


async def _main() -> None:
    await database.init_db()
    print(f"База данных готова: {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(_main())
