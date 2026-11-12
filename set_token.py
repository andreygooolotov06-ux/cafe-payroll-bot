from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from aiogram import Bot

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
TOKEN_RE = re.compile(r"\b(\d{6,15}:[A-Za-z0-9_-]{30,45})\b")


def extract_bot_token(raw_text: str) -> str | None:
    match = TOKEN_RE.search(raw_text or "")
    return match.group(1) if match else None


def update_env_token(env_path: Path, token: str) -> None:
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith("BOT_TOKEN="):
            new_lines.append(f"BOT_TOKEN={token}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.insert(0, f"BOT_TOKEN={token}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _read_windows_clipboard() -> str:
    return subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )


async def _verify_token_live(token: str) -> str:
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        return str(me.username or me.id)
    finally:
        await bot.session.close()


def main() -> None:
    print("\n=== Налаштування токена бота ===")
    print("1) Відкрий Telegram (@BotFather) та скопіюй токен (або все повідомлення з токеном).")
    print("2) Потім повернися сюди і просто натисни Enter (вставляти нічого не треба!).\n")

    while True:
        input("Натисни Enter, коли токен буде скопійовано...")
        clip = _read_windows_clipboard()
        token = extract_bot_token(clip)
        if not token:
            print(
                f"У буфері обміну поки не знайдено токен Telegram (довжина тексту: {len(clip.strip())}).\n"
                "Скопіюй у @BotFather рядок виду 1234567890:AAF... і знову натисни Enter.\n"
            )
            continue

        print(f"Токен знайдено (довжина: {len(token)})! Перевіряю з'єднання з Telegram...")
        try:
            bot_username = asyncio.run(_verify_token_live(token))
        except Exception as exc:
            print(f"Telegram не прийняв цей токен ({type(exc).__name__}). Перевір у @BotFather і спробуй ще раз.\n")
            continue

        update_env_token(ENV_FILE, token)
        print(f"Успішно! Токен збережено в .env, бот @{bot_username} готовий до запуску!")
        break


if __name__ == "__main__":
    main()
