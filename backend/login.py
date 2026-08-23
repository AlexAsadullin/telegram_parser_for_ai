"""
One-off interactive Telegram login.

The web backend never prompts for credentials — it only uses an already
authorised session file. Run this once from the project root:

    python -m backend.login

It asks for your phone number and the code Telegram sends you (plus the
2FA password if you have one) and writes the session to TG_SESSION.
"""
import asyncio

from telethon import TelegramClient

from .config import API_HASH, API_ID, SESSION_PATH


async def main() -> None:
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start()  # prompts on stdin, which is fine here

    me = await client.get_me()
    username = f" @{me.username}" if me.username else ""
    print(f"\nГотово. Авторизован как: {me.first_name or ''}{username} (id={me.id})")
    print(f"Сессия сохранена: {SESSION_PATH}.session")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
