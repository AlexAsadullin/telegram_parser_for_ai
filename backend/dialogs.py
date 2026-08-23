"""
Private-message (DM) parsing.

Unlike channel parsing, which keeps only text, a DM run also turns voice
messages and round video notes into text with a local Whisper model, so the
chat reads as one continuous transcript.

Two memory rules drive the design:

  * Messages are pulled and written in chunks of DM_CHUNK_SIZE. Entries are
    appended to messages.jsonl as they are produced and never accumulated in a
    dict, so a chat of any length costs the same memory.
  * Voice/round media is downloaded into a temporary directory and kept there
    for the whole run — the chat is only considered processed once every chunk
    is done, and only then is the directory removed.
"""
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaDocument,
    MessageService,
)

from . import transcriber
from .config import API_HASH, API_ID, DATA_DIR, DM_CHUNK_SIZE, SESSION_PATH
from .models import ParseStats
from .utils import display_name, resolve_dialog, start_client

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, ParseStats], Awaitable[None]]

MESSAGES_FILE = "messages.jsonl"
INFO_FILE = "info.json"


def dialog_dir(dialog_id: str | int) -> Path:
    """Directory holding one parsed dialog. Prefixed so it never collides with a channel."""
    return DATA_DIR / f"dm_{dialog_id}"


def _make_client() -> TelegramClient:
    return TelegramClient(SESSION_PATH, API_ID, API_HASH)


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------

def _voice_kind(message) -> str | None:
    """Return 'voice' for a voice message, 'video_note' for a round video, else None."""
    media = message.media
    if not isinstance(media, MessageMediaDocument):
        return None

    doc = media.document
    for attr in getattr(doc, "attributes", []):
        name = attr.__class__.__name__
        if name == "DocumentAttributeAudio" and getattr(attr, "voice", False):
            return "voice"
        if name == "DocumentAttributeVideo" and getattr(attr, "round_message", False):
            return "video_note"
    return None


def _duration(message) -> int | None:
    media = message.media
    if not isinstance(media, MessageMediaDocument):
        return None
    for attr in getattr(media.document, "attributes", []):
        if attr.__class__.__name__ in ("DocumentAttributeAudio", "DocumentAttributeVideo"):
            return getattr(attr, "duration", None)
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

async def _process_message(
    client: TelegramClient,
    message,
    tmp_dir: Path,
    me_id: int,
    stats: ParseStats,
) -> dict | None:
    """
    Turn one message into a transcript entry, or None if it carries nothing.

    Voice messages and round video notes are downloaded into tmp_dir and
    transcribed; their recognised text lands in the same `text` field a typed
    message uses, which is what keeps the transcript a single sequence.
    """
    if isinstance(message, MessageService):
        return None

    kind = _voice_kind(message)
    text = (message.raw_text or "").strip()

    if kind is None:
        if not text:
            return None
        entry_kind = "text"
        transcribed = False
    else:
        entry_kind = kind
        transcribed = False
        media_path = None
        try:
            media_path = await client.download_media(message.media, file=str(tmp_dir) + "/")
        except Exception as exc:
            logger.warning("Media download failed for msg %s: %s", message.id, exc)

        if media_path:
            if kind == "voice":
                stats.voice_count += 1
            else:
                stats.videos_count += 1
            # For a round video note this reads the audio stream out of the
            # .mp4 directly — the video itself is never needed.
            recognised = await transcriber.transcribe(Path(media_path))
            if recognised:
                text = f"{text}\n{recognised}".strip() if text else recognised
                transcribed = True
                stats.transcribed_count += 1

    return {
        "message_id": message.id,
        "date": message.date.strftime("%Y-%m-%dT%H:%M:%S") if message.date else None,
        "outgoing": bool(message.sender_id == me_id),
        "kind": entry_kind,
        "text": text,
        "transcribed": transcribed,
        "duration": _duration(message) if kind else None,
    }


async def parse_dialog(
    dialog_input: str,
    progress_cb: ProgressCallback,
    stats_out: ParseStats,
) -> ParseStats:
    logger.info("=== Starting DM parse for input: %r ===", dialog_input)
    client = _make_client()
    await start_client(client)
    logger.info("Telegram client connected")

    tmp_dir = Path(tempfile.mkdtemp(prefix="tg_dm_media_"))
    logger.info("Temporary media directory: %s", tmp_dir)

    try:
        entity = await resolve_dialog(client, dialog_input)
        me = await client.get_me()

        out_dir = dialog_dir(entity.id)
        out_dir.mkdir(parents=True, exist_ok=True)

        name = display_name(entity)
        info = {
            "id": entity.id,
            "kind": "dm",
            "title": name,
            "username": getattr(entity, "username", None) or "",
            "phone": getattr(entity, "phone", None) or "",
            "is_self": bool(getattr(entity, "is_self", False)),
        }
        _write_json_atomic(out_dir / INFO_FILE, info)

        stats_out.channel_id = str(entity.id)
        stats_out.channel_username = info["username"]
        stats_out.channel_title = name

        total = (await client.get_messages(entity, limit=0)).total
        logger.info("Dialog %r: %s messages reported by Telegram", name, total)

        messages_path = out_dir / MESSAGES_FILE
        current = 0
        chunk: list = []

        # Truncate any previous run before streaming this one in.
        with messages_path.open("w", encoding="utf-8") as sink:

            async def flush(chunk_messages: list) -> None:
                """Process one chunk end to end, then let it go out of scope."""
                nonlocal current
                for msg in chunk_messages:
                    current += 1
                    entry = await _process_message(client, msg, tmp_dir, me.id, stats_out)
                    stats_out.total_posts += 1
                    if entry is None:
                        stats_out.skipped_posts += 1
                        continue
                    sink.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    stats_out.saved_posts += 1
                sink.flush()
                await progress_cb(current, total, stats_out)
                logger.info(
                    "Chunk done: %s/%s processed, saved=%s transcribed=%s",
                    current, total, stats_out.saved_posts, stats_out.transcribed_count,
                )

            async for message in client.iter_messages(entity, reverse=True):
                chunk.append(message)
                if len(chunk) >= DM_CHUNK_SIZE:
                    await flush(chunk)
                    chunk = []

            if chunk:
                await flush(chunk)

        await progress_cb(current, total, stats_out)
        logger.info(
            "=== DM parse finished: saved=%s skipped=%s voice=%s video_notes=%s transcribed=%s ===",
            stats_out.saved_posts, stats_out.skipped_posts,
            stats_out.voice_count, stats_out.videos_count, stats_out.transcribed_count,
        )

    finally:
        # The whole chat is done (or has failed) — only now drop the media.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Temporary media directory removed: %s", tmp_dir)
        await client.disconnect()
        logger.info("Telegram client disconnected")

    return stats_out


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def iter_messages(messages_path: Path):
    """
    Yield transcript entries one at a time, in chronological order.

    Reading stays streaming so exporting a long chat costs no more memory than
    parsing it did.
    """
    with messages_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line in %s", messages_path)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

async def list_telegram_dialogs(limit: int = 100) -> list[dict]:
    """List private chats on the account so the UI can offer them for parsing."""
    client = _make_client()
    await start_client(client)
    try:
        out = []
        async for dialog in client.iter_dialogs(limit=limit):
            if not dialog.is_user:
                continue
            entity = dialog.entity
            if getattr(entity, "bot", False):
                continue
            out.append({
                "id": str(entity.id),
                "name": display_name(entity),
                "username": getattr(entity, "username", None) or "",
                "last_message_at": dialog.date.strftime("%Y-%m-%d %H:%M") if dialog.date else "",
                "parsed": dialog_dir(entity.id).joinpath(INFO_FILE).exists(),
            })
        return out
    finally:
        await client.disconnect()


def list_parsed_dialogs() -> list[dict]:
    """Metadata for every dialog already parsed to disk."""
    dialogs = []
    if not DATA_DIR.exists():
        return dialogs

    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("dm_"):
            continue
        info_path = d / INFO_FILE
        if not info_path.exists():
            continue

        with info_path.open(encoding="utf-8") as f:
            info = json.load(f)

        messages = 0
        transcribed = 0
        messages_path = d / MESSAGES_FILE
        if messages_path.exists():
            # Counted line by line: never loads the chat into memory.
            for entry in iter_messages(messages_path):
                messages += 1
                if entry.get("transcribed"):
                    transcribed += 1

        dialogs.append({
            "dialog_id": str(info.get("id", d.name.removeprefix("dm_"))),
            "title": info.get("title", ""),
            "username": info.get("username", ""),
            "messages_count": messages,
            "transcribed_count": transcribed,
        })

    return dialogs


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
