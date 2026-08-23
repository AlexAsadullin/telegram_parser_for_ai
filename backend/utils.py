import logging
import re

from telethon import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInviteAlready

logger = logging.getLogger(__name__)


def parse_channel_input(raw: str):
    """
    Normalise any channel reference to something get_entity() accepts.

    Supported formats:
      - @username  /  username
      - https://t.me/username  /  t.me/username
      - https://t.me/username/123          (link to a specific post)
      - https://t.me/+InviteHash           (private channel invite)
      - https://t.me/joinchat/InviteHash   (legacy invite link)
      - numeric id  (e.g. -1001234567890 or 1234567890)
    """
    raw = raw.strip()

    # numeric id
    try:
        return int(raw)
    except ValueError:
        pass

    # private invite link  https://t.me/+Abc123  or  https://t.me/joinchat/Abc123
    invite_match = re.match(r"https?://t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", raw)
    if invite_match:
        return f"https://t.me/+{invite_match.group(1)}"

    # post link  https://t.me/username/123  (also t.me/username/123)
    post_match = re.match(r"(?:https?://)?t\.me/([^/+][^/]*)/\d+", raw)
    if post_match:
        return f"@{post_match.group(1)}"

    # channel link  https://t.me/username  or  t.me/username
    link_match = re.match(r"(?:https?://)?t\.me/([^/+][^/]*)/?$", raw)
    if link_match:
        return f"@{link_match.group(1)}"

    # bare username with or without @
    if not raw.startswith("@"):
        return f"@{raw}"

    return raw


async def resolve_channel(client: TelegramClient, raw: str):
    """
    Return a Telethon entity for the channel.
    Raises ValueError with a clear message on failure.
    """
    identifier = parse_channel_input(raw)
    logger.info("Resolving channel: input=%r → identifier=%r", raw, identifier)

    try:
        entity = await client.get_entity(identifier)
    except Exception as exc:
        # try invite link fallback
        if isinstance(identifier, str) and "/+" in identifier:
            invite_hash = identifier.rsplit("+", 1)[-1].rstrip("/")
            logger.warning(
                "get_entity failed for invite link, trying CheckChatInviteRequest(hash=%r): %s",
                invite_hash, exc,
            )
            try:
                result = await client(CheckChatInviteRequest(invite_hash))
                if isinstance(result, ChatInviteAlready):
                    entity = result.chat
                else:
                    raise ValueError(
                        f"Канал по invite-ссылке найден, но вы в нём не состоите. "
                        f"Сначала вступите в канал в Telegram, потом запустите скачивание."
                    )
            except ValueError:
                raise
            except Exception as inner:
                logger.error("Invite link resolution failed: %s", inner)
                raise ValueError(f"Не удалось открыть invite-ссылку '{raw}': {inner}")
        else:
            logger.error("get_entity failed for %r: %s", identifier, exc)
            raise ValueError(f"Не удалось найти канал '{raw}': {exc}")

    if not isinstance(entity, (Channel, Chat)):
        type_name = type(entity).__name__
        logger.error("Resolved entity is %s, not a channel/chat", type_name)
        raise ValueError(
            f"'{raw}' — это {type_name}, не канал или группа. "
            f"Скачивание возможно только для каналов и групп."
        )

    logger.info(
        "Channel resolved: id=%s title=%r username=%r",
        entity.id, getattr(entity, "title", "?"), getattr(entity, "username", None),
    )
    return entity
