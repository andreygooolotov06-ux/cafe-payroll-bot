from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import AdminPermissionSettings, Base, Role

DEFAULT_ROLES = (
    "Адмін",
    "Бариста",
    "Кухар",
    "Прибиральник",
    "Охоронець",
    "Водій",
    "Бармен",
)

_LIBPQ_ONLY_PARAMS = frozenset({
    "sslmode",
    "channel_binding",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "gssencmode",
    "target_session_attrs",
})


def normalize_database_url(raw_url: str) -> tuple[str, dict[str, Any]]:
    """Нормалізує DATABASE_URL (зокрема Neon/Render PostgreSQL) для SQLAlchemy + asyncpg."""
    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    if not url.startswith("postgresql+asyncpg://"):
        return url, {}

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    raw_pairs = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = ""
    kept_pairs: list[tuple[str, str]] = []
    for k, v in raw_pairs:
        kl = k.lower()
        if kl == "sslmode":
            sslmode = v.strip().lower()
        elif kl in _LIBPQ_ONLY_PARAMS:
            continue
        else:
            kept_pairs.append((k, v))

    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept_pairs), parts.fragment))

    connect_args: dict[str, Any] = {}
    if sslmode == "disable":
        pass
    elif sslmode in {"verify-full", "verify-ca"} or (
        sslmode == "require" and (host.endswith(".neon.tech") or host.endswith(".supabase.co"))
    ):
        ctx = ssl.create_default_context()
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = sslmode != "verify-ca"
        connect_args["ssl"] = ctx
    elif sslmode in {"require", "prefer", "allow"} or (
        not sslmode and (host.endswith(".neon.tech") or host.endswith(".render.com") or host.endswith(".supabase.co"))
    ):
        connect_args["ssl"] = "require"

    return clean_url, connect_args


def create_db_engine(
    db_path: Path | str,
    *,
    database_url: str | None = None,
) -> AsyncEngine:
    target_raw = database_url.strip() if database_url and database_url.strip() else None
    if target_raw is None and isinstance(db_path, str) and "://" in db_path:
        target_raw = db_path.strip()

    if target_raw is not None:
        url, connect_args = normalize_database_url(target_raw)
        is_sqlite = url.startswith("sqlite")
        if not is_sqlite:
            return create_async_engine(
                url,
                echo=False,
                future=True,
                pool_pre_ping=True,
                pool_recycle=240,
                pool_size=5,
                max_overflow=5,
                connect_args=connect_args,
            )
    else:
        if str(db_path) != ":memory:":
            path_obj = Path(db_path)
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{path_obj.as_posix()}"
        else:
            url = "sqlite+aiosqlite:///:memory:"

    engine = create_async_engine(url, echo=False, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA cache_size=-16000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


def async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine: AsyncEngine, session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "sqlite":
            res = await conn.execute(text("PRAGMA table_info(employees)"))
            existing_cols = {row[1] for row in res.fetchall()}
            if "last_start_time" not in existing_cols:
                await conn.execute(text("ALTER TABLE employees ADD COLUMN last_start_time VARCHAR(5)"))
            if "last_end_time" not in existing_cols:
                await conn.execute(text("ALTER TABLE employees ADD COLUMN last_end_time VARCHAR(5)"))
            if "card_number" not in existing_cols:
                await conn.execute(text("ALTER TABLE employees ADD COLUMN card_number VARCHAR(32)"))

        # Композитні індекси для швидкої вибірки змін, нарахувань, виплат та заявок
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shifts_emp_date ON shifts (employee_id, work_date)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_adjustments_emp_date ON adjustments (employee_id, adj_date)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payouts_emp_date ON payouts (employee_id, paid_date)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payout_requests_status_created ON payout_requests (status, created_at DESC)"))

    async with session_maker() as session:
        existing = set((await session.scalars(select(Role.name))).all())
        for role_name in DEFAULT_ROLES:
            if role_name not in existing:
                session.add(Role(name=role_name))

        adm_perms = await session.get(AdminPermissionSettings, 1)
        if adm_perms is None:
            session.add(AdminPermissionSettings(id=1))

        await session.commit()


async def close_db(engine: AsyncEngine) -> None:
    await engine.dispose()
