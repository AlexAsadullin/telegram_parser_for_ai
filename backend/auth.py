"""
Telegram login driven from the web UI.

Logging in is a multi-step conversation: request a code, send the code back,
and sometimes answer a 2FA password. Telethon keeps that state on a *connected*
client, so one client is held open here between HTTP requests for the duration
of the flow, then closed as soon as the session file is authorised — parsing
runs open their own clients.

The state is module level because there is exactly one session file, so there
is exactly one login in flight at a time; a lock serialises concurrent calls.
"""
import asyncio
import logging

from telethon import TelegramClient, errors

from .config import API_HASH, API_ID, SESSION_PATH
from .utils import display_name

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_client: TelegramClient | None = None
_phone: str | None = None
_phone_code_hash: str | None = None


class AuthError(Exception):
    """A login problem worth showing to the user as-is."""


def _new_client() -> TelegramClient:
    return TelegramClient(SESSION_PATH, API_ID, API_HASH)


async def _drop_client() -> None:
    """Close the in-flight login client and forget the pending code."""
    global _client, _phone, _phone_code_hash
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
    _client = None
    _phone = None
    _phone_code_hash = None


def _describe(user) -> dict:
    return {
        "id": user.id,
        "name": display_name(user),
        "username": getattr(user, "username", None) or "",
        "phone": getattr(user, "phone", None) or "",
    }


async def status() -> dict:
    """
    Report whether the stored session can be used for parsing.

    Reuses the in-flight login client when there is one, so the session file is
    never opened by two clients at once.
    """
    async with _lock:
        if _client is not None and _client.is_connected():
            if await _client.is_user_authorized():
                user = await _client.get_me()
                await _drop_client()
                return {"authorized": True, "user": _describe(user), "code_pending": False}
            return {"authorized": False, "user": None, "code_pending": _phone_code_hash is not None}

        client = _new_client()
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"authorized": False, "user": None, "code_pending": False}
            user = await client.get_me()
            return {"authorized": True, "user": _describe(user), "code_pending": False}
        finally:
            await client.disconnect()


async def send_code(phone: str) -> dict:
    """Ask Telegram to send a login code to `phone`."""
    global _client, _phone, _phone_code_hash

    phone = phone.strip().replace(" ", "")
    if not phone:
        raise AuthError("Введите номер телефона.")
    if not phone.startswith("+"):
        phone = "+" + phone

    async with _lock:
        await _drop_client()

        client = _new_client()
        await client.connect()

        if await client.is_user_authorized():
            user = await client.get_me()
            await client.disconnect()
            return {"already_authorized": True, "user": _describe(user)}

        try:
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            await client.disconnect()
            raise AuthError("Неверный номер телефона.")
        except errors.FloodWaitError as exc:
            await client.disconnect()
            raise AuthError(f"Слишком много попыток. Повторите через {exc.seconds} с.")
        except Exception as exc:
            await client.disconnect()
            raise AuthError(f"Не удалось отправить код: {exc}")

        _client = client
        _phone = phone
        _phone_code_hash = sent.phone_code_hash
        logger.info("Login code sent to %s", phone)
        return {"already_authorized": False, "phone": phone}


async def sign_in(code: str) -> dict:
    """Complete the login with the code from Telegram."""
    global _client

    code = code.strip()
    if not code:
        raise AuthError("Введите код из Telegram.")

    async with _lock:
        if _client is None or _phone_code_hash is None:
            raise AuthError("Сессия входа истекла. Запросите код заново.")

        try:
            user = await _client.sign_in(
                phone=_phone, code=code, phone_code_hash=_phone_code_hash
            )
        except errors.SessionPasswordNeededError:
            # Account has 2FA; keep the client open for the password step.
            logger.info("2FA password required for %s", _phone)
            return {"authorized": False, "password_required": True}
        except errors.PhoneCodeInvalidError:
            raise AuthError("Неверный код. Проверьте и попробуйте ещё раз.")
        except errors.PhoneCodeExpiredError:
            await _drop_client()
            raise AuthError("Код истёк. Запросите новый.")
        except errors.FloodWaitError as exc:
            raise AuthError(f"Слишком много попыток. Повторите через {exc.seconds} с.")
        except Exception as exc:
            raise AuthError(f"Не удалось войти: {exc}")

        described = _describe(user)
        # Session file is written; release it so parsers can open it.
        await _drop_client()
        logger.info("Logged in as %s (id=%s)", described["name"], described["id"])
        return {"authorized": True, "password_required": False, "user": described}


async def sign_in_password(password: str) -> dict:
    """Second factor for accounts protected by a cloud password."""
    if not password:
        raise AuthError("Введите облачный пароль.")

    async with _lock:
        if _client is None:
            raise AuthError("Сессия входа истекла. Запросите код заново.")

        try:
            user = await _client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise AuthError("Неверный облачный пароль.")
        except errors.FloodWaitError as exc:
            raise AuthError(f"Слишком много попыток. Повторите через {exc.seconds} с.")
        except Exception as exc:
            raise AuthError(f"Не удалось войти: {exc}")

        described = _describe(user)
        await _drop_client()
        logger.info("Logged in with 2FA as %s (id=%s)", described["name"], described["id"])
        return {"authorized": True, "user": described}


async def log_out() -> dict:
    """Log out and invalidate the stored session."""
    async with _lock:
        await _drop_client()
        client = _new_client()
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
                logger.info("Logged out; session invalidated")
        finally:
            await client.disconnect()
        return {"authorized": False}


async def cancel() -> dict:
    """Abandon a login in progress."""
    async with _lock:
        await _drop_client()
        return {"cancelled": True}
