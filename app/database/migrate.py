from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import BASE_DIR
from app.database.engine import async_session_factory, create_db_engine, init_db
from app.database.models import (
    Adjustment,
    AdminPermissionSettings,
    DailyRevenue,
    Employee,
    IdempotencyRecord,
    OwnerBinding,
    Payout,
    PayoutRequest,
    Role,
    ScheduledRunLog,
    Shift,
)

# Порядок вставки (з урахуванням зовнішніх ключів)
COPY_MODELS = (
    Role,
    OwnerBinding,
    AdminPermissionSettings,
    ScheduledRunLog,
    DailyRevenue,
    Employee,
    Shift,
    Adjustment,
    Payout,
    PayoutRequest,
    IdempotencyRecord,
)

# Таблиці, що свідчать про наявність робочих даних у цільовій БД
_USER_DATA_MODELS = (
    OwnerBinding,
    DailyRevenue,
    Employee,
    Shift,
    Adjustment,
    Payout,
    PayoutRequest,
    IdempotencyRecord,
)


def mask_db_target(raw: str | Path) -> str:
    s = str(raw)
    if "://" not in s:
        return s
    parts = urlsplit(s)
    if parts.password:
        user = parts.username or ""
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{user}:***@{host}{port}" if user else f"{host}{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    return s


def _make_engine(source_or_target: str | Path) -> AsyncEngine:
    s = str(source_or_target).strip()
    if "://" in s:
        return create_db_engine(BASE_DIR / "data" / "unused.db", database_url=s)
    path = Path(s)
    if not path.is_absolute():
        path = BASE_DIR / path
    return create_db_engine(path)


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime) and v.tzinfo is None:
            cleaned[k] = v.replace(tzinfo=timezone.utc)
        else:
            cleaned[k] = v
    return cleaned


async def migrate_database(
    source: str | Path,
    target: str | Path,
    *,
    overwrite: bool = False,
    batch_size: int = 500,
) -> dict[str, int]:
    """Копіює всі таблиці з source (SQLite/DB URL) у target (PostgreSQL/Neon або іншу БД)."""
    src_str = str(source).strip()
    dst_str = str(target).strip()
    if not src_str or not dst_str:
        raise ValueError("Потрібно вказати і source, і target")

    if "://" not in src_str:
        src_path = Path(src_str)
        if not src_path.is_absolute():
            src_path = BASE_DIR / src_path
        if not src_path.is_file():
            raise FileNotFoundError(f"Файл вихідної БД не знайдено: {src_path}")
        src_cmp = str(src_path.resolve())
    else:
        src_cmp = src_str

    if "://" not in dst_str and dst_str != ":memory:":
        dst_path = Path(dst_str)
        if not dst_path.is_absolute():
            dst_path = BASE_DIR / dst_path
        dst_cmp = str(dst_path.resolve())
    else:
        dst_cmp = dst_str

    if src_cmp == dst_cmp:
        raise ValueError("Джерело та цільова БД не можуть збігатися")

    src_engine = _make_engine(source)
    dst_engine = _make_engine(target)

    try:
        # Ініціалізуємо схему в цільовій БД (і оновлюємо індекси/відсутні колонки у джерелі якщо потрібно)
        await init_db(src_engine, async_session_factory(src_engine))
        await init_db(dst_engine, async_session_factory(dst_engine))

        if not overwrite:
            async with dst_engine.connect() as dst_conn:
                for model in _USER_DATA_MODELS:
                    tbl = model.__table__
                    cnt = int(await dst_conn.scalar(select(func.count()).select_from(tbl)) or 0)
                    if cnt > 0:
                        raise RuntimeError(
                            f"Цільова БД вже містить дані у таблиці '{tbl.name}' ({cnt} рядків). "
                            "Використовуйте --overwrite для повної заміни."
                        )

        counts: dict[str, int] = {}
        async with src_engine.connect() as src_conn:
            async with dst_engine.begin() as dst_conn:
                # Спочатку очищаємо цільові таблиці у зворотному порядку FK (включно з початковими ролями)
                for model in reversed(COPY_MODELS):
                    await dst_conn.execute(delete(model.__table__))

                for model in COPY_MODELS:
                    tbl = model.__table__
                    res = await src_conn.execute(select(tbl))
                    rows = [_normalize_row(dict(r)) for r in res.mappings().all()]
                    counts[tbl.name] = len(rows)
                    if not rows:
                        continue
                    for start in range(0, len(rows), batch_size):
                        chunk = rows[start : start + batch_size]
                        await dst_conn.execute(tbl.insert(), chunk)

                # Оновлюємо sequence у PostgreSQL, щоб нові INSERT отримували коректні ID
                if dst_engine.dialect.name == "postgresql":
                    for model in COPY_MODELS:
                        tbl = model.__table__
                        if "id" not in tbl.c:
                            continue
                        seq_name = await dst_conn.scalar(
                            text("SELECT pg_get_serial_sequence(:tbl, 'id')"),
                            {"tbl": tbl.name},
                        )
                        if seq_name:
                            await dst_conn.execute(
                                text(
                                    f"SELECT setval(:seq, COALESCE((SELECT MAX(id) FROM {tbl.name}), 1), "
                                    f"(SELECT MAX(id) FROM {tbl.name}) IS NOT NULL)"
                                ),
                                {"seq": seq_name},
                            )

        return counts
    finally:
        await src_engine.dispose()
        await dst_engine.dispose()


async def _cli_main(argv: list[str] | None = None) -> int:
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(
        description="Перенесення даних Cafe Payroll Bot із локального SQLite у PostgreSQL (Neon/Render)."
    )
    parser.add_argument(
        "--source",
        default=os.getenv("DB_PATH", "data/cafe_payroll.db"),
        help="Шлях до SQLite-файлу або URL джерела (за замовчуванням DB_PATH з .env)",
    )
    parser.add_argument(
        "--target",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL DATABASE_URL призначення (за замовчуванням DATABASE_URL з .env)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Очистити цільову БД перед імпортом, якщо в ній уже є записи",
    )
    args = parser.parse_args(argv)

    if not args.target or not str(args.target).strip():
        print(
            "Помилка: вкажіть --target postgresql://... або задайте DATABASE_URL у .env",
            file=sys.stderr,
        )
        return 2

    print(f"Перенесення: {mask_db_target(args.source)} -> {mask_db_target(args.target)}")
    counts = await migrate_database(args.source, args.target, overwrite=args.overwrite)
    total = sum(counts.values())
    for tbl_name, cnt in counts.items():
        print(f"  - {tbl_name}: {cnt}")
    print(f"Готово! Усього перенесено рядків: {total}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_cli_main()))


if __name__ == "__main__":
    main()
