import asyncio
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel

import os


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

SESSION_PATH = Path(
    os.getenv(
        "TELEGRAM_SESSION",
        str(BASE_DIR / "telegram_session")
    )
)


async def main():

    client = TelegramClient(
        str(SESSION_PATH),
        API_ID,
        API_HASH,
        device_model="NOVA AI DEBUG",
        system_version="macOS",
        app_version="1.0",
    )

    await client.connect()

    if not await client.is_user_authorized():
        print("Telegram session НЕ авторизована")
        await client.disconnect()
        return

    dialogs = await client.get_dialogs(limit=30)

    print()
    print("=" * 70)
    print(" TELEGRAM DEBUG")
    print("=" * 70)

    for dialog in dialogs:

        unread_count = getattr(
            dialog,
            "unread_count",
            0
        )

        raw_dialog = getattr(
            dialog,
            "dialog",
            None
        )

        read_id = getattr(
            raw_dialog,
            "read_inbox_max_id",
            None
        )

        top_id = getattr(
            raw_dialog,
            "top_message",
            None
        )

        if unread_count <= 0:
            continue

        print()
        print("-" * 70)

        print(
            f"CHAT: {dialog.title}"
        )

        print(
            f"TYPE: {type(dialog.entity).__name__}"
        )

        print(
            f"UNREAD_COUNT: {unread_count}"
        )

        print(
            f"READ_INBOX_MAX_ID: {read_id}"
        )

        print(
            f"TOP_MESSAGE: {top_id}"
        )

        try:

            messages = await client.get_messages(
                dialog.entity,
                limit=10
            )

            print(
                f"LAST_MESSAGES: {len(messages)}"
            )

            for message in messages:

                if not message:
                    continue

                text = (
                    message.raw_text
                    or ""
                ).replace(
                    "\n",
                    " "
                )

                print(
                    f"  ID={message.id} "
                    f"OUT={message.out} "
                    f"DATE={message.date} "
                    f"TEXT={text[:120]}"
                )

        except Exception as e:

            print(
                f"ERROR: {e}"
            )

    print()
    print("=" * 70)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
