import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Awaitable

from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    MessageService,
    Poll,
)

from .config import API_HASH, API_ID, DATA_DIR, SESSION_PATH
from .models import ParseStats
from .utils import resolve_channel, start_client

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, ParseStats], Awaitable[None]]

PROGRESS_EVERY = 50


def _make_client() -> TelegramClient:
    return TelegramClient(SESSION_PATH, API_ID, API_HASH)


async def get_channel_info(client: TelegramClient, entity) -> dict:
    full = await client.get_entity(entity)
    username = getattr(full, "username", None) or ""
    link = f"https://t.me/{username}" if username else ""
    subscribers = getattr(full, "participants_count", None)
    linked_chat_id = getattr(full, "linked_chat_id", None) or None

    return {
        "id": entity.id,
        "title": getattr(entity, "title", ""),
        "username": username,
        "link": link,
        "discussion_group_id": linked_chat_id,
        "type": "private" if not username else "public",
        "description": getattr(entity, "about", "") or "",
        "subscribers": subscribers,
    }


def _extract_links(message) -> list[str]:
    links = []
    if not message.entities:
        return links
    for entity in message.entities:
        if isinstance(entity, MessageEntityUrl):
            start = entity.offset
            end = entity.offset + entity.length
            links.append(message.raw_text[start:end])
        elif isinstance(entity, MessageEntityTextUrl):
            links.append(entity.url)
    return list(dict.fromkeys(links))  # deduplicate, preserve order


def _classify_media(message) -> str | None:
    """Return media category name or None if no downloadable media."""
    media = message.media
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photos"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("video/") or mime == "video/mp4":
            return "videos"
        if mime in ("audio/ogg", "audio/mpeg", "audio/mp4"):
            return "voice"
        # VideoNote (round video) is a Document with a VideoSize attribute
        attrs = getattr(doc, "attributes", [])
        for attr in attrs:
            if attr.__class__.__name__ == "DocumentAttributeVideo":
                if getattr(attr, "round_message", False):
                    return "videos"
            if attr.__class__.__name__ == "DocumentAttributeAnimated":
                return "videos"
        return "files"
    if isinstance(media, MessageMediaWebPage):
        return None  # links already captured from entities
    return None


def _should_skip(message) -> bool:
    if isinstance(message, MessageService):
        return True
    if message.poll is not None:
        return True
    media_cat = _classify_media(message)
    has_text = bool(message.raw_text and message.raw_text.strip())
    if not has_text and media_cat is None:
        return True
    return False


async def _download_media_file(client: TelegramClient, message, out_dir: Path) -> tuple[str | None, str | None]:
    """Download media and return (category, filename) or (None, None)."""
    media_cat = _classify_media(message)
    if media_cat is None:
        return None, None

    target_dir = out_dir / media_cat
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        path = await client.download_media(message.media, file=str(target_dir) + "/")
    except Exception as exc:
        logger.warning("Media download failed for msg %s: %s", message.id, exc)
        return None, None

    if path is None:
        return None, None

    filename = os.path.basename(path)
    logger.debug("Downloaded media: msg=%s -> %s/%s", message.id, media_cat, filename)
    return media_cat, filename


async def parse_channel(
    channel_input: str,
    progress_cb: ProgressCallback,
    stats_out: ParseStats,
) -> ParseStats:
    logger.info("=== Starting parse for input: %r ===", channel_input)
    client = _make_client()
    await start_client(client)
    logger.info("Telegram client connected")

    try:
        entity = await resolve_channel(client, channel_input)
        channel_dir = DATA_DIR / str(entity.id)
        channel_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Channel directory: %s", channel_dir)

        info = await get_channel_info(client, entity)
        _write_json_atomic(channel_dir / "info.json", info)
        logger.info(
            "Channel info: id=%s title=%r username=%r type=%s subscribers=%s",
            info["id"], info["title"], info["username"], info["type"], info["subscribers"],
        )

        stats_out.channel_id = str(entity.id)
        stats_out.channel_username = info["username"]
        stats_out.channel_title = info["title"]

        total = (await client.get_messages(entity, limit=0)).total
        logger.info("Total messages reported by Telegram: %s", total)

        posts: dict[str, dict] = {}
        current = 0

        async for message in client.iter_messages(entity, reverse=True):
            current += 1
            date_str = message.date.strftime("%Y-%m-%d %H:%M") if message.date else "?"

            if _should_skip(message):
                reason = _skip_reason(message)
                stats_out.skipped_posts += 1
                stats_out.total_posts += 1
                logger.info(
                    "Post %s/%s [%s] msg_id=%s SKIP (%s)",
                    current, total, date_str, message.id, reason,
                )
                if current % PROGRESS_EVERY == 0:
                    await progress_cb(current, total, stats_out)
                continue

            timestamp = message.date.strftime("%Y-%m-%dT%H:%M:%S")
            text = message.raw_text or ""
            links = _extract_links(message)

            entry: dict = {
                "message_id": message.id,
                "text": text,
                "links": links,
                "photos": [],
                "videos": [],
                "voice": [],
                "files": [],
            }

            media_cat, filename = await _download_media_file(client, message, channel_dir)
            if media_cat and filename:
                entry[media_cat].append(f"{media_cat}/{filename}")
                _increment_media_stat(stats_out, media_cat)

            stats_out.links_count += len(links)
            stats_out.saved_posts += 1
            stats_out.total_posts += 1

            posts[timestamp] = entry

            tags = []
            if text:
                tags.append(f"text({len(text)})")
            if media_cat:
                tags.append(media_cat)
            if links:
                tags.append(f"links({len(links)})")
            logger.info(
                "Post %s/%s [%s] msg_id=%s SAVED %s",
                current, total, date_str, message.id, "+".join(tags) or "empty",
            )

            if current % PROGRESS_EVERY == 0:
                await progress_cb(current, total, stats_out)
                _write_json_atomic(channel_dir / "posts.json", posts)

        _write_json_atomic(channel_dir / "posts.json", posts)
        await progress_cb(current, total, stats_out)
        logger.info(
            "=== Parse finished: saved=%s skipped=%s photos=%s videos=%s voice=%s files=%s links=%s ===",
            stats_out.saved_posts, stats_out.skipped_posts,
            stats_out.photos_count, stats_out.videos_count,
            stats_out.voice_count, stats_out.files_count, stats_out.links_count,
        )

    finally:
        await client.disconnect()
        logger.info("Telegram client disconnected")

    return stats_out


def _skip_reason(message) -> str:
    if isinstance(message, MessageService):
        return "service message"
    if message.poll is not None:
        return "poll"
    has_text = bool(message.raw_text and message.raw_text.strip())
    if not has_text and _classify_media(message) is None:
        return "empty / unsupported media"
    return "unknown"


def _increment_media_stat(stats: ParseStats, category: str) -> None:
    if category == "photos":
        stats.photos_count += 1
    elif category == "videos":
        stats.videos_count += 1
    elif category == "voice":
        stats.voice_count += 1
    elif category == "files":
        stats.files_count += 1


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
