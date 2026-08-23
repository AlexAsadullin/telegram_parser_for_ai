import aiosqlite

from .config import DB_PATH


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS parse_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                channel_id TEXT NOT NULL,
                channel_username TEXT,
                channel_title TEXT,
                total_posts INTEGER DEFAULT 0,
                saved_posts INTEGER DEFAULT 0,
                skipped_posts INTEGER DEFAULT 0,
                photos_count INTEGER DEFAULT 0,
                videos_count INTEGER DEFAULT 0,
                voice_count INTEGER DEFAULT 0,
                files_count INTEGER DEFAULT 0,
                links_count INTEGER DEFAULT 0,
                status TEXT CHECK(status IN ('completed', 'failed', 'in_progress')),
                error_message TEXT
            )
        """)
        # Migration for older DBs that lack error_message column
        try:
            await db.execute("ALTER TABLE parse_history ADD COLUMN error_message TEXT")
        except aiosqlite.OperationalError:
            pass  # column already exists
        await db.commit()


async def create_record(channel_id: str, channel_username: str, channel_title: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO parse_history (channel_id, channel_username, channel_title, status)
            VALUES (?, ?, ?, 'in_progress')
            """,
            (channel_id, channel_username, channel_title),
        )
        await db.commit()
        return cursor.lastrowid


async def update_record(
    record_id: int,
    status: str,
    stats: dict,
    error_message: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE parse_history SET
                status = ?,
                channel_id = COALESCE(NULLIF(?, ''), channel_id),
                channel_username = COALESCE(NULLIF(?, ''), channel_username),
                channel_title = COALESCE(NULLIF(?, ''), channel_title),
                total_posts = ?,
                saved_posts = ?,
                skipped_posts = ?,
                photos_count = ?,
                videos_count = ?,
                voice_count = ?,
                files_count = ?,
                links_count = ?,
                error_message = ?
            WHERE id = ?
            """,
            (
                status,
                stats.get("channel_id", "") or "",
                stats.get("channel_username", "") or "",
                stats.get("channel_title", "") or "",
                stats.get("total_posts", 0),
                stats.get("saved_posts", 0),
                stats.get("skipped_posts", 0),
                stats.get("photos_count", 0),
                stats.get("videos_count", 0),
                stats.get("voice_count", 0),
                stats.get("files_count", 0),
                stats.get("links_count", 0),
                error_message,
                record_id,
            ),
        )
        await db.commit()


async def get_history() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM parse_history ORDER BY created_at DESC LIMIT 100"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
