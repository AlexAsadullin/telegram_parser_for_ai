"""
Markdown export built for a language model to read on its own.

The plain-text export assumes a human who already knows what the file is. This
one does not: each document carries its own header explaining what it contains,
who the participants are, and how to read the notation — so an assistant handed
nothing but this file can reconstruct the conversation or the channel.

Three properties matter for that:

  * Self-describing. A legend states what `[голосовое]` means and, importantly,
    that such text came from automatic recognition and may contain errors — an
    assistant should not quote it as verbatim wording.
  * Unambiguous speakers. Every turn is attributed, so no reply can be misread
    as coming from the wrong participant.
  * Streaming. Documents are yielded piece by piece, like the .txt export, so a
    chat of any length costs the same memory.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import dialogs
from .config import DATA_DIR

KIND_LABEL = {
    "voice": "голосовое",
    "video_note": "кружок",
}


def _clock(seconds) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _split_dt(value: str | None) -> tuple[str, str]:
    """('2024-01-15T10:25:00') -> ('2024-01-15', '10:25')"""
    if not value:
        return "", ""
    date, _, time = value.partition("T")
    return date, time[:5]


# ---------------------------------------------------------------------------
# Private messages
# ---------------------------------------------------------------------------

def iter_dialog_markdown(dialog_id: str):
    """Yield a dialogue transcript as Markdown, oldest message first."""
    out_dir = dialogs.dialog_dir(dialog_id)
    info_path = out_dir / dialogs.INFO_FILE
    messages_path = out_dir / dialogs.MESSAGES_FILE

    if not info_path.exists() or not messages_path.exists():
        raise FileNotFoundError(f"Dialog data not found for id={dialog_id}")

    with info_path.open(encoding="utf-8") as f:
        info = json.load(f)

    peer = info.get("title") or str(dialog_id)
    username = info.get("username", "")
    me = "Я"

    # First pass: counts and date range for the header. Streaming, so the file
    # is walked twice rather than held in memory.
    total = voice = notes = transcribed = 0
    first_date = last_date = ""
    for entry in dialogs.iter_messages(messages_path):
        total += 1
        kind = entry.get("kind")
        if kind == "voice":
            voice += 1
        elif kind == "video_note":
            notes += 1
        if entry.get("transcribed"):
            transcribed += 1
        date, _ = _split_dt(entry.get("date"))
        if date:
            first_date = first_date or date
            last_date = date

    speech = voice + notes
    peer_label = f"{peer}" + (f" (@{username})" if username else "")

    yield f"# Переписка: {peer_label}\n\n"
    yield (
        "Это полный текст личной переписки в Telegram, выгруженный для анализа. "
        "Документ самодостаточен: вся переписка целиком находится ниже.\n\n"
    )

    yield "## О документе\n\n"
    yield "| Параметр | Значение |\n|---|---|\n"
    yield "| Тип | Личная переписка, диалог двух участников |\n"
    yield f"| Участник 1 | **{me}** — владелец аккаунта, от чьего лица сделан экспорт |\n"
    yield f"| Участник 2 | **{peer}**{f' (@{username})' if username else ''} |\n"
    yield f"| Сообщений | {total} |\n"
    if first_date:
        yield f"| Период | {first_date} — {last_date} |\n"
    if speech:
        yield f"| Голосовых и кружков | {speech} (распознано {transcribed}) |\n"
    yield f"| Дата экспорта | {_now()} |\n"
    yield "\n"

    yield "## Как читать\n\n"
    yield (
        "- Сообщения идут в хронологическом порядке, от старых к новым, "
        "и сгруппированы по датам (заголовки `###`).\n"
        "- Каждая реплика начинается с имени автора и времени: "
        f"`**{me}** · 10:02`.\n"
        f"- **{me}** — это владелец аккаунта. **{peer}** — собеседник.\n"
    )
    if speech:
        yield (
            "- Пометка `(голосовое, 0:12)` или `(кружок, 0:08)` означает, что "
            "текст реплики получен **автоматическим распознаванием речи**, а не "
            "напечатан. Такой текст может содержать ошибки распознавания, "
            "поэтому не цитируйте его как дословную формулировку — используйте "
            "как смысл сказанного.\n"
            "- Пометка `(голосовое, не распознано)` означает, что сообщение было, "
            "но его содержимое неизвестно.\n"
        )
    yield (
        "- Вложения, кроме голосовых и кружков, в переписку не выгружались — "
        "фото, видео и файлы здесь не представлены.\n\n"
    )

    yield "---\n\n## Переписка\n\n"

    current_date = None
    for entry in dialogs.iter_messages(messages_path):
        date, time = _split_dt(entry.get("date"))
        if date and date != current_date:
            current_date = date
            yield f"### {date}\n\n"

        who = me if entry.get("outgoing") else peer
        kind = entry.get("kind")

        note = ""
        if kind in KIND_LABEL:
            stamp = _clock(entry.get("duration"))
            label = KIND_LABEL[kind] + (f", {stamp}" if stamp else "")
            if not entry.get("transcribed"):
                label += ", не распознано"
            note = f" · _({label})_"

        text = (entry.get("text") or "").strip() or "—"
        # Blockquote keeps multi-line messages visually attached to their author.
        body = "\n".join(f"> {line}" if line.strip() else ">" for line in text.split("\n"))

        yield f"**{who}** · {time}{note}\n\n{body}\n\n"

    yield f"---\n\n_Конец переписки. Всего сообщений: {total}._\n"


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def iter_channel_markdown(channel_id: str, transcriptions: dict[str, str] | None = None):
    """Yield a channel's posts as Markdown, oldest post first."""
    channel_dir = DATA_DIR / str(channel_id)
    info_path = channel_dir / "info.json"
    posts_path = channel_dir / "posts.json"

    if not info_path.exists() or not posts_path.exists():
        raise FileNotFoundError(f"Channel data not found for id={channel_id}")

    with info_path.open(encoding="utf-8") as f:
        info = json.load(f)
    with posts_path.open(encoding="utf-8") as f:
        posts: dict[str, dict] = json.load(f)

    transcriptions = transcriptions or {}

    title = info.get("title", "")
    username = info.get("username", "")
    ref = f"@{username}" if username else str(channel_id)
    subscribers = info.get("subscribers")
    description = (info.get("description") or "").strip()

    # Channel posts live in a dict keyed by timestamp; oldest first reads as a
    # history, which is what an assistant summarising the channel wants.
    items = sorted(posts.items())
    first = items[0][0].split("T")[0] if items else ""
    last = items[-1][0].split("T")[0] if items else ""
    with_voice = sum(1 for _, e in items if e.get("voice"))

    heading = f"{ref}" + (f" — {title}" if title else "")
    yield f"# Telegram-канал: {heading}\n\n"
    yield (
        "Это выгрузка постов Telegram-канала для анализа. Документ "
        "самодостаточен: все посты целиком находятся ниже.\n\n"
    )

    yield "## О документе\n\n"
    yield "| Параметр | Значение |\n|---|---|\n"
    yield "| Тип | Лента Telegram-канала, последовательность постов |\n"
    yield f"| Канал | {heading} |\n"
    if info.get("type"):
        yield f"| Доступ | {'публичный' if info['type'] == 'public' else 'приватный'} |\n"
    if subscribers:
        yield f"| Подписчиков | {subscribers:,} |\n".replace(",", " ")
    yield f"| Постов | {len(items)} |\n"
    if first:
        yield f"| Период | {first} — {last} |\n"
    yield f"| Дата экспорта | {_now()} |\n"
    yield "\n"

    if description:
        yield f"**Описание канала.** {description}\n\n"

    yield "## Как читать\n\n"
    yield (
        "- Посты идут в хронологическом порядке, от старых к новым, "
        "и сгруппированы по датам (заголовки `###`).\n"
        "- Каждый пост начинается со времени публикации.\n"
        "- Строка `_Медиа: ..._` перечисляет вложения поста. Сами файлы в "
        "документ не входят — только упоминание, что они были.\n"
        "- Строка `_Ссылки: ..._` содержит ссылки из поста.\n"
    )
    if with_voice:
        yield (
            "- `_(голосовое сообщение)_` — пост был голосовым. Если далее идёт "
            "текст, он получен распознаванием речи и может содержать ошибки.\n"
        )
    yield "\n---\n\n## Посты\n\n"

    current_date = None
    for timestamp, entry in items:
        date, time = _split_dt(timestamp)
        if date and date != current_date:
            current_date = date
            yield f"### {date}\n\n"

        yield f"**{time}**\n\n"

        text = (entry.get("text") or "").strip()
        if text:
            yield f"{text}\n\n"

        if entry.get("voice"):
            yield "_(голосовое сообщение)_\n\n"
            recognised = transcriptions.get(timestamp)
            if recognised:
                yield f"{recognised}\n\n"

        media = []
        for field, word in (("photos", "фото"), ("videos", "видео"), ("files", "файлов")):
            n = len(entry.get(field, []))
            if n:
                media.append(f"{n} {word}")
        if media:
            yield f"_Медиа: {', '.join(media)}_\n\n"

        links = entry.get("links") or []
        if links:
            yield "_Ссылки: " + ", ".join(links) + "_\n\n"

        if not text and not entry.get("voice") and not media and not links:
            yield "_(пустой пост)_\n\n"

    yield f"---\n\n_Конец выгрузки. Всего постов: {len(items)}._\n"
