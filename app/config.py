from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    owner_usernames: frozenset[str]
    timezone_name: str
    tz: ZoneInfo
    db_path: Path
    database_url: str | None = None
    miniapp_host: str = "127.0.0.1"
    miniapp_port: int = 8080
    miniapp_url: str | None = None
    miniapp_auto_tunnel: bool = True
    bot_mode: str = "polling"  # "polling" | "webhook"
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str | None = None


def _parse_owner_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            ids.add(int(cleaned))
        except ValueError as exc:
            raise ValueError(f"Некоректний OWNER_IDS: '{cleaned}' має бути числом.") from exc
    return frozenset(ids)


def _parse_owner_usernames(raw: str) -> frozenset[str]:
    names: set[str] = set()
    for part in raw.split(","):
        cleaned = part.strip().lstrip("@").lower()
        if cleaned:
            names.add(cleaned)
    return frozenset(names)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or (BASE_DIR / ".env"))

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("Не задано BOT_TOKEN у файлі .env")

    owner_ids = _parse_owner_ids(os.getenv("OWNER_IDS", ""))
    owner_usernames = _parse_owner_usernames(os.getenv("OWNER_USERNAMES", ""))
    if not owner_ids and not owner_usernames:
        raise ValueError(
            "Не задано OWNER_IDS або OWNER_USERNAMES у файлі .env (потрібен хоча б один Telegram ID або @username власниці)"
        )

    timezone_name = os.getenv("TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"
    tz = ZoneInfo(timezone_name)

    raw_db = os.getenv("DB_PATH", "data/cafe_payroll.db").strip() or "data/cafe_payroll.db"
    db_path = Path(raw_db)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    database_url = os.getenv("DATABASE_URL", "").strip() or None

    is_cloud = bool(
        os.getenv("RENDER")
        or os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("FLY_APP_NAME")
        or os.getenv("KOYEB_APP_NAME")
    )

    raw_host = os.getenv("MINIAPP_HOST", "").strip()
    if raw_host:
        miniapp_host = raw_host
    else:
        miniapp_host = "0.0.0.0" if (is_cloud or os.getenv("PORT")) else "127.0.0.1"

    raw_port = (
        os.getenv("MINIAPP_PORT", "").strip()
        or os.getenv("PORT", "").strip()
        or "8080"
    )
    try:
        miniapp_port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"Некоректний MINIAPP_PORT/PORT: '{raw_port}'") from exc

    raw_url = (
        os.getenv("MINIAPP_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        or os.getenv("WEBHOOK_URL", "").strip()
    )
    miniapp_url = raw_url.rstrip("/") if raw_url else None

    raw_auto_env = os.getenv("MINIAPP_AUTO_TUNNEL")
    if raw_auto_env is not None and raw_auto_env.strip():
        miniapp_auto_tunnel = raw_auto_env.strip().lower() not in {"0", "false", "no", "off"}
    else:
        miniapp_auto_tunnel = not bool(miniapp_url or is_cloud)

    raw_mode = (os.getenv("BOT_MODE", "auto") or "auto").strip().lower()
    if raw_mode not in {"auto", "polling", "webhook"}:
        raise ValueError(f"Некоректний BOT_MODE: '{raw_mode}'. Очікується auto, polling або webhook.")
    if raw_mode == "auto":
        bot_mode = "webhook" if (miniapp_url and (is_cloud or os.getenv("WEBHOOK_URL"))) else "polling"
    else:
        bot_mode = raw_mode

    if bot_mode == "webhook" and not miniapp_url:
        raise ValueError("Для режиму BOT_MODE=webhook потрібен публічний HTTPS MINIAPP_URL або RENDER_EXTERNAL_URL")

    raw_wh_path = (os.getenv("WEBHOOK_PATH", "/telegram/webhook") or "/telegram/webhook").strip()
    webhook_path = raw_wh_path if raw_wh_path.startswith("/") else f"/{raw_wh_path}"

    raw_wh_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if raw_wh_secret:
        webhook_secret: str | None = raw_wh_secret
    elif bot_mode == "webhook":
        webhook_secret = hashlib.sha256(f"wh:{bot_token}".encode("utf-8")).hexdigest()[:48]
    else:
        webhook_secret = None

    return Settings(
        bot_token=bot_token,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
        timezone_name=timezone_name,
        tz=tz,
        db_path=db_path,
        database_url=database_url,
        miniapp_host=miniapp_host,
        miniapp_port=miniapp_port,
        miniapp_url=miniapp_url,
        miniapp_auto_tunnel=miniapp_auto_tunnel,
        bot_mode=bot_mode,
        webhook_path=webhook_path,
        webhook_secret=webhook_secret,
    )
