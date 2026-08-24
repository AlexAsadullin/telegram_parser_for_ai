"""
Export downloaded data to a single LLM-friendly text file.

Channels: posts only, optionally transcribing voice via the Telegram API
(requires Premium). Private messages: text plus locally recognised voice and
round video notes, already merged into one chronological transcript.

Run from the project root:
    uvicorn backend.main:app --reload
"""
import asyncio
import json
from datetime import datetime, timezone

from telethon import TelegramClient

from . import dialogs
from .config import API_HASH, API_ID, DATA_DIR, SESSION_PATH
from .utils import resolve_channel, start_client


# ---------------------------------------------------------------------------
# Channel listing
# ---------------------------------------------------------------------------

def list_channels() -> list[dict]:
    """Return metadata for every channel that has been downloaded."""
    channels = []
    if not DATA_DIR.exists():
        return channels

    for channel_dir in sorted(DATA_DIR.iterdir()):
        if not channel_dir.is_dir() or channel_dir.name.startswith("dm_"):
            continue
        info_path = channel_dir / "info.json"
        posts_path = channel_dir / "posts.json"
        if not info_path.exists():
            continue

        with info_path.open(encoding="utf-8") as f:
            info = json.load(f)

        posts_count = 0
        voice_count = 0
        if posts_path.exists():
            with posts_path.open(encoding="utf-8") as f:
                posts = json.load(f)
            posts_count = len(posts)
            voice_count = sum(1 for p in posts.values() if p.get("voice"))

        channels.append({
            "channel_id": str(info.get("id", channel_dir.name)),
            "title": info.get("title", ""),
            "username": info.get("username", ""),
            "link": info.get("link", ""),
            "type": info.get("type", ""),
            "subscribers": info.get("subscribers"),
            "posts_count": posts_count,
            "voice_count": voice_count,
        })

    return channels


# ---------------------------------------------------------------------------
# Voice transcription helpers
# ---------------------------------------------------------------------------

async def _transcribe_voice(client, peer, msg_id: int, poll_timeout: float = 20.0) -> str | None:
    """
    Call Telegram's TranscribeAudio API and wait for the result.
    Returns the transcription string, or None if unavailable.
    Requires Telegram Premium on the authenticated account.
    """
    try:
        from telethon.tl.functions.messages import TranscribeAudioRequest
        result = await client(TranscribeAudioRequest(peer=peer, msg_id=msg_id))
        if not result.pending:
            return result.text or None

        deadline = asyncio.get_event_loop().time() + poll_timeout
        while result.pending:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(2.0, remaining))
            result = await client(TranscribeAudioRequest(peer=peer, msg_id=msg_id))

        return result.text or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------

def _media_annotation(entry: dict) -> str | None:
    """Return a bracketed media annotation line, or None if no media."""
    parts = []
    n = len(entry.get("photos", []))
    if n:
        parts.append(f"{n} фото")
    n = len(entry.get("videos", []))
    if n:
        parts.append(f"{n} видео")
    n = len(entry.get("files", []))
    if n:
        parts.append(f"{n} {'файл' if n == 1 else 'файлов'}")
    return f"[медиа: {', '.join(parts)}]" if parts else None


def _format_post(timestamp: str, entry: dict, transcription: str | None = None) -> str:
    """Format a single post as plain text for LLM consumption."""
    lines = [f"[{timestamp}]"]

    has_voice = bool(entry.get("voice"))
    text = (entry.get("text") or "").strip()
    media_note = _media_annotation(entry)

    if text:
        lines.append(text)

    if has_voice:
        if transcription:
            lines.append(f"[голосовое сообщение]\n[транскрипция: \"{transcription}\"]")
        else:
            lines.append("[голосовое сообщение]")

    if media_note:
        lines.append(media_note)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main export function
# ---------------------------------------------------------------------------

async def channel_transcriptions_for(channel_id: str, transcribe: bool) -> dict[str, str]:
    """Transcriptions for a channel by id, or {} when not requested."""
    if not transcribe:
        return {}

    channel_dir = DATA_DIR / str(channel_id)
    info_path = channel_dir / "info.json"
    posts_path = channel_dir / "posts.json"
    if not info_path.exists() or not posts_path.exists():
        raise FileNotFoundError(f"Channel data not found for id={channel_id}")

    with info_path.open(encoding="utf-8") as f:
        info = json.load(f)
    with posts_path.open(encoding="utf-8") as f:
        posts = json.load(f)

    username = info.get("username", "")
    channel_ref = f"@{username}" if username else str(channel_id)
    return await collect_channel_transcriptions(sorted(posts.items()), channel_ref, info)


async def collect_channel_transcriptions(items, channel_ref: str, info: dict) -> dict[str, str]:
    """
    Transcribe a channel's voice posts via Telegram (Premium only).

    Shared by the .txt and .md exports, keyed by post timestamp.
    """
    voice_items = [
        (ts, entry) for ts, entry in items
        if entry.get("voice") and entry.get("message_id")
    ]
    if not voice_items:
        return {}

    transcriptions: dict[str, str] = {}
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await start_client(client)
    try:
        peer = await resolve_channel(client, channel_ref or str(info.get("id", "")))
        for ts, entry in voice_items:
            result = await _transcribe_voice(client, peer, entry["message_id"])
            if result:
                transcriptions[ts] = result
    finally:
        await client.disconnect()
    return transcriptions


async def export_channel_text(channel_id: str, transcribe: bool = False) -> str:
    """
    Generate a single LLM-friendly text export of all posts in a channel.
    Posts are ordered newest → oldest.
    If transcribe=True, connects to Telegram and transcribes voice messages.
    """
    channel_dir = DATA_DIR / channel_id
    info_path = channel_dir / "info.json"
    posts_path = channel_dir / "posts.json"

    if not info_path.exists() or not posts_path.exists():
        raise FileNotFoundError(f"Channel data not found for id={channel_id}")

    with info_path.open(encoding="utf-8") as f:
        info = json.load(f)
    with posts_path.open(encoding="utf-8") as f:
        posts: dict[str, dict] = json.load(f)

    title = info.get("title", "")
    username = info.get("username", "")
    ch_type = info.get("type", "")
    subscribers = info.get("subscribers")
    channel_ref = f"@{username}" if username else channel_id
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    sep = "─" * 60
    header_lines = [
        sep,
        f"КАНАЛ: {channel_ref}" + (f" — {title}" if title else ""),
    ]
    if ch_type:
        header_lines.append(
            f"ТИП: {'публичный' if ch_type == 'public' else 'приватный'}"
            + (f" | ПОДПИСЧИКОВ: {subscribers:,}" if subscribers else "")
        )
    header_lines += [
        f"ЭКСПОРТ: {exported_at} | ПОСТОВ: {len(posts)}",
        sep,
    ]
    header = "\n".join(header_lines)

    sorted_items = sorted(posts.items(), reverse=True)

    transcriptions = await collect_channel_transcriptions(
        sorted_items, channel_ref, info
    ) if transcribe else {}

    post_blocks = [
        _format_post(ts, entry, transcriptions.get(ts))
        for ts, entry in sorted_items
    ]

    body = "\n\n".join(post_blocks)
    return f"{header}\n\n{body}\n\n{sep}\n"


# ---------------------------------------------------------------------------
# Private-message export
# ---------------------------------------------------------------------------

_KIND_LABEL = {
    "voice": "голосовое",
    "video_note": "кружок",
}


def _format_dm_entry(entry: dict, me_label: str, peer_label: str) -> str:
    """
    One line of the transcript.

    Typed text and recognised speech are rendered the same way — only a small
    marker says where the text came from, so the chat reads as one sequence.
    """
    who = me_label if entry.get("outgoing") else peer_label
    stamp = (entry.get("date") or "").replace("T", " ")

    marker = ""
    kind = entry.get("kind")
    if kind in _KIND_LABEL:
        label = _KIND_LABEL[kind]
        duration = entry.get("duration")
        if duration:
            label = f"{label}, {duration}с"
        marker = f" [{label}{'' if entry.get('transcribed') else ', не распознано'}]"

    text = (entry.get("text") or "").strip() or "—"
    return f"[{stamp}] {who}{marker}: {text}"


def iter_dialog_export(dialog_id: str):
    """
    Yield an LLM-friendly transcript of a parsed private chat, piece by piece.

    Messages come out oldest → newest, the order a conversation is read in.
    The file is walked twice — once to count for the header, once to format —
    so no more than one message is ever held in memory, however long the chat.
    """
    out_dir = dialogs.dialog_dir(dialog_id)
    info_path = out_dir / dialogs.INFO_FILE
    messages_path = out_dir / dialogs.MESSAGES_FILE

    if not info_path.exists() or not messages_path.exists():
        raise FileNotFoundError(f"Dialog data not found for id={dialog_id}")

    with info_path.open(encoding="utf-8") as f:
        info = json.load(f)

    peer_label = info.get("title") or dialog_id
    me_label = "Я"
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    total = 0
    transcribed = 0
    for entry in dialogs.iter_messages(messages_path):
        total += 1
        if entry.get("transcribed"):
            transcribed += 1

    sep = "─" * 60
    username = info.get("username", "")
    yield "\n".join([
        sep,
        f"ЛИЧНЫЙ ЧАТ: {peer_label}" + (f" (@{username})" if username else ""),
        f"ЭКСПОРТ: {exported_at} | СООБЩЕНИЙ: {total} | РАСПОЗНАНО ГОЛОСОМ: {transcribed}",
        sep,
        "",
        "",
    ])

    for entry in dialogs.iter_messages(messages_path):
        yield _format_dm_entry(entry, me_label, peer_label) + "\n"

    yield f"\n{sep}\n"
