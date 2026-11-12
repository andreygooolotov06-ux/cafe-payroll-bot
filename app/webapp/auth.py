from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass


class InitDataError(ValueError):
    """Помилка криптографічної перевірки Telegram WebApp initData."""


@dataclass(frozen=True)
class VerifiedTelegramUser:
    telegram_id: int
    username: str | None
    first_name: str
    last_name: str
    auth_date: int
    start_param: str | None = None


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 86400,
    now_ts: int | None = None,
) -> VerifiedTelegramUser:
    raw = (init_data or "").strip()
    if not raw:
        raise InitDataError("Відсутні дані авторизації Telegram (initData).")

    pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True)
    if not pairs:
        raise InitDataError("Некоректний формат initData.")

    params = dict(pairs)
    received_hash = params.get("hash", "").strip()
    if not received_hash:
        raise InitDataError("Відсутній криптографічний підпис initData.")

    # Валідація часу життя підпису auth_date
    raw_auth_date = params.get("auth_date", "").strip()
    try:
        auth_date = int(raw_auth_date)
    except ValueError as exc:
        raise InitDataError("Некоректна позначка часу auth_date.") from exc

    now = int(time.time()) if now_ts is None else int(now_ts)
    if auth_date > now + 300:
        raise InitDataError("Позначка часу initData з майбутнього.")
    if max_age_seconds > 0 and (now - auth_date) > max_age_seconds:
        raise InitDataError("Сесія Mini App застаріла — відкрий кабінет знову з чату бота.")

    # Офіційний алгоритм перевірки підпису Telegram WebApp
    check_lines = [f"{k}={v}" for k, v in sorted(pairs, key=lambda kv: kv[0]) if k != "hash"]
    data_check_string = "\n".join(check_lines)

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash.lower(), received_hash.lower()):
        raise InitDataError("Невірний підпис Telegram WebApp.")

    user_raw = params.get("user")
    if not user_raw:
        raise InitDataError("Відсутній профіль користувача у initData.")

    try:
        user_obj = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise InitDataError("Некоректні дані профілю користувача.") from exc

    uid = user_obj.get("id")
    if not isinstance(uid, int):
        raise InitDataError("Некоректний Telegram ID користувача.")

    username_raw = user_obj.get("username")
    username = str(username_raw).strip() if username_raw else None
    first_name = str(user_obj.get("first_name") or "").strip()
    last_name = str(user_obj.get("last_name") or "").strip()
    start_param_raw = params.get("start_param")
    start_param = str(start_param_raw).strip() if start_param_raw else None

    return VerifiedTelegramUser(
        telegram_id=uid,
        username=username,
        first_name=first_name,
        last_name=last_name,
        auth_date=auth_date,
        start_param=start_param,
    )


def build_telegram_init_data(
    user_id: int,
    bot_token: str,
    username: str | None = None,
    first_name: str = "User",
    last_name: str = "",
    auth_date: int | None = None,
    start_param: str | None = None,
) -> str:
    """Генерує підписаний initData для автотестів."""
    ts = int(time.time()) if auth_date is None else int(auth_date)
    user_payload: dict[str, object] = {"id": int(user_id), "first_name": first_name}
    if last_name:
        user_payload["last_name"] = last_name
    if username:
        user_payload["username"] = username

    pairs: list[tuple[str, str]] = [
        ("auth_date", str(ts)),
        ("query_id", "TESTQUERY"),
        ("user", json.dumps(user_payload, separators=(",", ":"), ensure_ascii=False)),
    ]
    if start_param:
        pairs.append(("start_param", start_param))

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs, key=lambda kv: kv[0]))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    pairs.append(("hash", sig))
    return urllib.parse.urlencode(pairs)
