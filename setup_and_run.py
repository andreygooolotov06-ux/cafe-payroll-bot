from __future__ import annotations

import asyncio
import getpass
from pathlib import Path

from dotenv import dotenv_values

from app.main import main

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def _needs_setup() -> bool:
    if not ENV_FILE.exists():
        return True
    vals = dotenv_values(ENV_FILE)
    token = (vals.get("BOT_TOKEN") or "").strip()
    owner_ids = (vals.get("OWNER_IDS") or "").strip()
    owner_names = (vals.get("OWNER_USERNAMES") or "").strip()
    if not token or token == "your_telegram_bot_token_here":
        return True
    if not owner_ids and not owner_names:
        return True
    return False


def _interactive_setup() -> None:
    print("\n=== Перше налаштування Telegram-бота кафе ===")
    print("Підказка: у цьому вікні вставляйте текст ПРАВОЮ КНОПКОЮ МИШІ (не Ctrl+V), інакше Windows може додати зайвий символ.\n")

    while True:
        raw_token = getpass.getpass("1) Встав токен бота від @BotFather (введення приховане) і натисни Enter: ")
        token = "".join(ch for ch in raw_token if ord(ch) >= 32).strip()
        if ":" in token and len(token) >= 35:
            break
        print("   Схоже, токен неповний або містить зайвий символ. Спробуй вставити правою кнопкою миші.\n")

    while True:
        owner_raw = input("2) Введи свій @username у Telegram (наприклад, @anya_cafe) або числовий ID: ").strip()
        cleaned = owner_raw.lstrip("@").strip()
        if cleaned:
            break
        print("   Поле не може бути порожнім. Спробуй ще раз.\n")

    if cleaned.isdigit():
        owner_ids_val = cleaned
        owner_names_val = ""
    else:
        owner_ids_val = ""
        owner_names_val = cleaned.lower()

    ENV_FILE.write_text(
        f"BOT_TOKEN={token}\n"
        f"OWNER_IDS={owner_ids_val}\n"
        f"OWNER_USERNAMES={owner_names_val}\n"
        "TIMEZONE=Europe/Kyiv\n"
        "DB_PATH=data/cafe_payroll.db\n",
        encoding="utf-8",
    )
    print("\nГотово! Налаштування збережено у файл .env. Запускаю бота...\n")


if __name__ == "__main__":
    if _needs_setup():
        _interactive_setup()
    asyncio.run(main())
