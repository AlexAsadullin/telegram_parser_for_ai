"""
Private-message (DM) parsing.

Unlike channel parsing, which keeps only text, a DM run also turns voice
messages and round video notes into text with a local Whisper model, so the
chat reads as one continuous transcript.

Two memory rules drive the design:

  * Messages are pulled and processed in chunks of DM_CHUNK_SIZE. Each chunk
    is written to its own JSONL part under chunks/ (see chunk_store.py) and
    never accumulated in a dict, so a chat of any length costs the same
    memory — and an interrupted run keeps every chunk it already finished.
    At the end of a successful run the parts are merged into one
    messages.jsonl, which is what the exporters read.
  * Voice/round media is downloaded into a temporary directory and kept there
    for the whole run — the chat is only considered processed once every chunk
    is done, and only then is the directory removed.
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaDocument,
    MessageService,
)

from . import chunk_store, transcriber
from .config import API_HASH, API_ID, DATA_DIR, DM_CHUNK_SIZE, SESSION_PATH
from .models import ParseStats
from .utils import display_name, resolve_dialog, start_client

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., Awaitable[None]]

MESSAGES_FILE = chunk_store.MERGED_FILE
INFO_FILE = "info.json"

# Plain text messages fly by; media messages are slow and each one deserves an
# update, so the counter only throttles the cheap ones.
TEXT_PROGRESS_EVERY = 10

# How many media files may be fetched at once, ahead of the recogniser.
# Bounds both open connections and how far the prefetch may run ahead.
DOWNLOAD_CONCURRENCY = 4

_KIND_RU = {"voice": "голосовое", "video_note": "кружок"}


def _clock(seconds: float | None) -> str:
    """Format a duration the way a chat client shows it: 0:07, 2:31."""
    if not seconds:
        return ""
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


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
    """
    Duration in whole seconds.

    Telegram types these differently: DocumentAttributeAudio.duration is an int
    but DocumentAttributeVideo.duration is a float, so round video notes used to
    reach the formatter as e.g. 8.44. Normalised here, at the single place the
    value enters the code.
    """
    media = message.media
    if not isinstance(media, MessageMediaDocument):
        return None
    for attr in getattr(media.document, "attributes", []):
        if attr.__class__.__name__ in ("DocumentAttributeAudio", "DocumentAttributeVideo"):
            raw = getattr(attr, "duration", None)
            return int(raw) if raw is not None else None
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

async def _download_media(
    client: TelegramClient,
    message,
    tmp_dir: Path,
    sem: asyncio.Semaphore,
    stats: ParseStats,
) -> str | None:
    """
    Fetch one voice/round file. Runs ahead of transcription, several at a time.

    The semaphore is what keeps "ahead" bounded: without it a chunk of 200
    messages would open 200 simultaneous downloads.
    """
    async with sem:
        try:
            path = await client.download_media(message.media, file=str(tmp_dir) + "/")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Media download failed for msg %s: %s", message.id, exc)
            return None
    if path:
        # Downloaded and waiting for the recogniser — this is the queue depth
        # the UI shows, and the proof that the two stages overlap.
        stats.queued_media += 1
    return path


async def _process_message(
    client: TelegramClient,
    message,
    tmp_dir: Path,
    me_id: int,
    stats: ParseStats,
    report=None,
    download=None,
) -> dict | None:
    """
    Turn one message into a transcript entry, or None if it carries nothing.

    Voice messages and round video notes are transcribed; their recognised text
    lands in the same `text` field a typed message uses, which is what keeps the
    transcript a single sequence.

    `download` is the task started for this message earlier by the caller, so by
    the time we get here the file is usually already on disk and only the
    recognition remains. Messages are still consumed strictly in order, which is
    what keeps the output chronological.

    `report(stage, detail)` is awaited around the slow steps so the UI can say
    which message is being downloaded or recognised instead of sitting still.
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
        label = _KIND_RU[kind]
        duration = _duration(message) or 0
        stamp = _clock(duration)
        described = f"{label} {stamp}".strip()

        if kind == "voice":
            stats.voice_count += 1
        else:
            stats.videos_count += 1
        stats.voice_seconds += duration

        media_path = None
        if download is not None:
            # Only announce a wait if there is actually one: usually the
            # prefetch finished while an earlier message was being recognised.
            if not download.done() and report:
                await report("downloading", f"жду загрузку: {described}")
            media_path = await download
            if media_path:
                stats.queued_media = max(0, stats.queued_media - 1)

        if media_path:
            if report:
                await report("transcribing", f"распознаю {described}")
            # For a round video note this reads the audio stream out of the
            # .mp4 directly — the video itself is never needed.
            recognised = await transcriber.transcribe(Path(media_path))
            if recognised:
                text = f"{text}\n{recognised}".strip() if text else recognised
                transcribed = True
                stats.transcribed_count += 1
                stats.transcribed_seconds += duration

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
    since: datetime | None = None,
) -> ParseStats:
    logger.info("=== Starting DM parse for input: %r ===", dialog_input)
    if since is not None and since.tzinfo is None:
        since = since.astimezone()
    client = _make_client()
    await start_client(client)
    logger.info("Telegram client connected")

    tmp_dir = Path(tempfile.mkdtemp(prefix="tg_dm_media_"))
    logger.info("Temporary media directory: %s", tmp_dir)

    out_dir: Path | None = None

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

        # Pay the model load now, while the UI already shows progress, instead
        # of stalling on the first voice message.
        await progress_cb(0, total, stats_out, stage="loading_model",
                          detail="загружаю модель распознавания")
        await transcriber.warm_up()

        current = 0
        chunk: list = []
        # ChunkWriter wipes chunks/ on creation, so a fresh run never inherits
        # parts of the previous one.
        writer = chunk_store.ChunkWriter(out_dir)

        async def flush(chunk_messages: list) -> None:
            """Process one chunk end to end, then let it go out of scope."""
            nonlocal current

            # Start every download in this chunk up front. Downloading is
            # network-bound and recognition is compute-bound, so they run at
            # the same time: while the model works on one voice message, the
            # next ones are already arriving.
            downloads = {
                msg.id: asyncio.create_task(
                    _download_media(client, msg, tmp_dir, sem, stats_out)
                )
                for msg in chunk_messages
                if _voice_kind(msg)
            }

            try:
                for msg in chunk_messages:
                    current += 1

                    async def report(stage: str, detail: str) -> None:
                        await progress_cb(current, total, stats_out,
                                          stage=stage, detail=detail)

                    entry = await _process_message(
                        client, msg, tmp_dir, me.id, stats_out,
                        report=report, download=downloads.get(msg.id),
                    )
                    stats_out.total_posts += 1
                    if entry is None:
                        stats_out.skipped_posts += 1
                    else:
                        writer.write(entry)
                        stats_out.saved_posts += 1

                    # Media messages already reported themselves; text is
                    # cheap enough that a periodic tick is plenty.
                    if entry is not None and entry["kind"] != "text":
                        writer.flush()
                        await progress_cb(current, total, stats_out, stage="scanning")
                    elif current % TEXT_PROGRESS_EVERY == 0:
                        await progress_cb(current, total, stats_out, stage="scanning")
            finally:
                # On cancellation the prefetch must not outlive the run.
                for task in downloads.values():
                    if not task.done():
                        task.cancel()

            writer.flush()
            await progress_cb(current, total, stats_out, stage="scanning")
            logger.info(
                "Chunk done: %s/%s processed, saved=%s transcribed=%s (%ss of %ss speech)",
                current, total, stats_out.saved_posts, stats_out.transcribed_count,
                stats_out.transcribed_seconds, stats_out.voice_seconds,
            )

        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

        # With reverse=True, offset_date makes Telegram start at that moment
        # and walk forward, so older history is never fetched.
        first_of_chunk = None
        try:
            async for message in client.iter_messages(entity, reverse=True, offset_date=since):
                if not chunk:
                    first_of_chunk = message.id
                chunk.append(message)
                if len(chunk) >= DM_CHUNK_SIZE:
                    writer.start_chunk(first_of_chunk)
                    await flush(chunk)
                    chunk = []

            if chunk:
                writer.start_chunk(first_of_chunk)
                await flush(chunk)
        finally:
            writer.close()

        # All parts are on disk — fold them into the single merged transcript.
        chunk_store.merge_chunks(out_dir)

        await progress_cb(current, total, stats_out)
        logger.info(
            "=== DM parse finished: saved=%s skipped=%s voice=%s video_notes=%s transcribed=%s ===",
            stats_out.saved_posts, stats_out.skipped_posts,
            stats_out.voice_count, stats_out.videos_count, stats_out.transcribed_count,
        )

    except asyncio.CancelledError:
        # Stopped from the UI: a half-parsed chat is not useful, and the run
        # rewrites all chunk parts on start anyway, so drop the whole directory.
        if out_dir is not None:
            shutil.rmtree(out_dir, ignore_errors=True)
            logger.info("DM parse cancelled — removed partial data: %s", out_dir)
        raise

    finally:
        # The whole chat is done (or has failed) — only now drop the media.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Temporary media directory removed: %s", tmp_dir)
        # Cleanup runs while the task may already be cancelled; a second
        # cancellation must not mask the reason we are unwinding.
        try:
            await client.disconnect()
        except (Exception, asyncio.CancelledError):
            pass
        logger.info("Telegram client disconnected")

    return stats_out


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def iter_messages(messages_path: Path):
    """
    Yield transcript entries one at a time, in chronological order.

    Reading stays streaming so exporting a long chat costs no more memory than
    parsing it did. Accepts either the merged file or a list of chunk parts.
    """
    paths = [messages_path] if isinstance(messages_path, Path) else messages_path
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed line in %s", path)


def dialog_message_paths(out_dir: Path) -> list[Path]:
    """
    Files to read a dialog's transcript from.

    Prefers the merged messages.jsonl; falls back to the chunk parts when the
    merge was lost or interrupted (the chunks are the backup copy).
    """
    merged = out_dir / MESSAGES_FILE
    if merged.exists():
        return [merged]
    return chunk_store.list_chunk_paths(out_dir)


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
        paths = dialog_message_paths(d)
        if paths:
            # Counted line by line: never loads the chat into memory.
            for entry in iter_messages(paths):
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
