from __future__ import annotations

import ssl
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from app.config import Settings, load_settings
from app.database import (
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
    async_session_factory,
    close_db,
    create_db_engine,
    init_db,
    normalize_database_url,
)
from app.database.migrate import mask_db_target, migrate_database
from app.handlers import common
from app.main import configure_webhook_route
from app.middlewares import AppContextMiddleware
from app.webapp import create_webapp


def test_normalize_database_url() -> None:
    neon_url = (
        "postgresql://neondb_owner:secret123@ep-cool-rain.us-east-2.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    clean, kwargs = normalize_database_url(neon_url)
    assert clean == "postgresql+asyncpg://neondb_owner:secret123@ep-cool-rain.us-east-2.aws.neon.tech/neondb"
    assert isinstance(kwargs.get("ssl"), ssl.SSLContext)
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED

    render_url = "postgres://user:pw@dpg-12345.oregon-postgres.render.com/cafedb"
    clean_r, kwargs_r = normalize_database_url(render_url)
    assert clean_r == "postgresql+asyncpg://user:pw@dpg-12345.oregon-postgres.render.com/cafedb"
    assert kwargs_r.get("ssl") == "require"

    local_pg = "postgresql://postgres:pw@localhost:5432/cafe?sslmode=disable&application_name=cafe"
    clean_l, kwargs_l = normalize_database_url(local_pg)
    assert clean_l == "postgresql+asyncpg://postgres:pw@localhost:5432/cafe?application_name=cafe"
    assert "ssl" not in kwargs_l

    sq_url, sq_kwargs = normalize_database_url("sqlite+aiosqlite:///data/cafe_payroll.db")
    assert sq_url == "sqlite+aiosqlite:///data/cafe_payroll.db"
    assert sq_kwargs == {}

    assert "***" in mask_db_target(neon_url)
    assert "secret123" not in mask_db_target(neon_url)


def test_load_settings_local_vs_cloud_render(monkeypatch, tmp_path) -> None:
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    for key in (
        "BOT_TOKEN",
        "OWNER_IDS",
        "OWNER_USERNAMES",
        "TIMEZONE",
        "DB_PATH",
        "DATABASE_URL",
        "MINIAPP_HOST",
        "MINIAPP_PORT",
        "PORT",
        "MINIAPP_URL",
        "RENDER",
        "RENDER_EXTERNAL_URL",
        "WEBHOOK_URL",
        "MINIAPP_AUTO_TUNNEL",
        "BOT_MODE",
        "WEBHOOK_PATH",
        "WEBHOOK_SECRET",
        "RAILWAY_ENVIRONMENT",
        "FLY_APP_NAME",
        "KOYEB_APP_NAME",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("BOT_TOKEN", "123456:LOCAL_TEST")
    monkeypatch.setenv("OWNER_IDS", "1000")
    local_cfg = load_settings(empty_env)
    assert local_cfg.bot_mode == "polling"
    assert local_cfg.miniapp_host == "127.0.0.1"
    assert local_cfg.miniapp_port == 8080
    assert local_cfg.miniapp_auto_tunnel is True
    assert local_cfg.database_url is None
    assert local_cfg.webhook_secret is None

    # Імітуємо Render Web Service оточення
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://cafe-payroll.onrender.com/")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@ep-test.neon.tech/neondb?sslmode=require")
    cloud_cfg = load_settings(empty_env)
    assert cloud_cfg.bot_mode == "webhook"
    assert cloud_cfg.miniapp_host == "0.0.0.0"
    assert cloud_cfg.miniapp_port == 10000
    assert cloud_cfg.miniapp_url == "https://cafe-payroll.onrender.com"
    assert cloud_cfg.miniapp_auto_tunnel is False
    assert cloud_cfg.webhook_path == "/telegram/webhook"
    assert cloud_cfg.webhook_secret is not None and len(cloud_cfg.webhook_secret) == 48
    assert cloud_cfg.database_url == "postgresql://u:p@ep-test.neon.tech/neondb?sslmode=require"

    # Повторний виклик дає той самий детермінований секрет
    assert load_settings(empty_env).webhook_secret == cloud_cfg.webhook_secret

    # Explicit webhook без публічного URL має падати з зрозумілою помилкою
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setenv("BOT_MODE", "webhook")
    with pytest.raises(ValueError, match="MINIAPP_URL"):
        load_settings(empty_env)


@pytest.mark.asyncio
async def test_migrate_database_full_parity_and_overwrite_guard(tmp_path) -> None:
    src_db = tmp_path / "src.db"
    dst_db = tmp_path / "dst.db"

    src_engine = create_db_engine(src_db)
    src_sm = async_session_factory(src_engine)
    await init_db(src_engine, src_sm)

    async with src_sm() as s:
        perms = await s.get(AdminPermissionSettings, 1)
        assert perms is not None
        perms.can_edit_hours = True

        custom_role = Role(name="Сушист")
        s.add(custom_role)
        await s.flush()

        barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        assert barista_id is not None

        emp = Employee(
            full_name="Анна Бариста",
            role_id=barista_id,
            telegram_id=5010,
            telegram_username="anna_b",
            hourly_rate=125.0,
            revenue_percent=2.5,
            banquet_rate_per_person=50.0,
            invite_code="INV999",
            card_number="4149499112345678",
            last_start_time="09:00",
            last_end_time="21:00",
        )
        s.add(emp)
        s.add(OwnerBinding(username="anya_owner", telegram_id=1000))
        s.add(DailyRevenue(work_date=date(2026, 11, 10), amount=32000.0))
        s.add(ScheduledRunLog(job_key="staff_morning_0900", last_run_date=date(2026, 11, 12)))
        await s.flush()

        s.add(
            Shift(
                employee_id=emp.id,
                work_date=date(2026, 11, 10),
                start_time="09:00",
                end_time="21:00",
                hours=12.0,
                hourly_rate_snapshot=125.0,
                percent_snapshot=2.5,
                is_banquet=True,
                banquet_guests=10,
                banquet_rate_snapshot=50.0,
            )
        )
        s.add(Adjustment(employee_id=emp.id, adj_date=date(2026, 11, 10), kind="bonus", amount=200.0, comment="Ок"))
        payout = Payout(employee_id=emp.id, paid_date=date(2026, 11, 11), amount=1000.0, comment="Аванс")
        s.add(payout)
        await s.flush()

        s.add(
            PayoutRequest(
                employee_id=emp.id,
                amount=1000.0,
                comment="На карту",
                card_snapshot="4149499112345678",
                status="paid",
                payout_id=payout.id,
                reviewed_by_tg_id=1000,
                reviewed_at=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc),
                paid_at=datetime(2026, 11, 11, 12, 5, tzinfo=timezone.utc),
            )
        )
        s.add(
            IdempotencyRecord(
                key="req-1",
                actor_tg_id=1000,
                endpoint="POST:/api/owner/payouts",
                status_code=201,
                response_json='{"ok": true}',
            )
        )
        await s.commit()

    await close_db(src_engine)

    with pytest.raises(FileNotFoundError):
        await migrate_database(tmp_path / "missing.db", dst_db)
    with pytest.raises(ValueError):
        await migrate_database(src_db, src_db)

    counts = await migrate_database(src_db, dst_db)
    assert counts["roles"] >= 8
    assert counts["employees"] == 1
    assert counts["shifts"] == 1
    assert counts["payout_requests"] == 1
    assert counts["idempotency_records"] == 1

    # Повторний запуск без --overwrite блокується
    with pytest.raises(RuntimeError, match="overwrite"):
        await migrate_database(src_db, dst_db, overwrite=False)

    # Перевіряємо перенесені дані у цільовій БД
    dst_engine = create_db_engine(dst_db)
    dst_sm = async_session_factory(dst_engine)
    async with dst_sm() as s2:
        emps = (await s2.scalars(select(Employee))).all()
        assert len(emps) == 1
        assert emps[0].full_name == "Анна Бариста"
        assert emps[0].card_number == "4149499112345678"
        assert emps[0].role.name == "Бариста"
        assert await s2.scalar(select(func.count(Role.id)).where(Role.name == "Сушист")) == 1
        perms2 = await s2.get(AdminPermissionSettings, 1)
        assert perms2 is not None and perms2.can_edit_hours is True
        reqs2 = (await s2.scalars(select(PayoutRequest))).all()
        assert len(reqs2) == 1 and reqs2[0].payout_id is not None
    await close_db(dst_engine)

    # Із overwrite=True міграція проходить чисто
    counts2 = await migrate_database(src_db, dst_db, overwrite=True)
    assert counts2["employees"] == 1


@pytest.mark.asyncio
async def test_webhook_route_secret_and_dispatch(tmp_path) -> None:
    db_file = tmp_path / "wh.db"
    engine = create_db_engine(db_file)
    session_maker = async_session_factory(engine)
    await init_db(engine, session_maker)

    token = "123456:TEST_WEBHOOK_TOKEN"
    settings = Settings(
        bot_token=token,
        owner_ids=frozenset({1000}),
        owner_usernames=frozenset(),
        timezone_name="Europe/Kyiv",
        tz=ZoneInfo("Europe/Kyiv"),
        db_path=db_file,
        miniapp_url="https://cafe.example.com",
        miniapp_auto_tunnel=False,
        bot_mode="webhook",
        webhook_path="/telegram/webhook",
        webhook_secret="super_secret_token_42",
    )

    sent: list[dict] = []
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def fake_make_request(_bot, method, timeout=None):
        name = method.__class__.__name__
        if name == "SendMessage":
            sent.append({"chat_id": method.chat_id, "text": method.text})
            return Message(
                message_id=len(sent),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if name == "GetMe":
            return User(id=999999, is_bot=True, first_name="CafeBot", username="my_cafe_bot")
        return True

    bot.session.make_request = AsyncMock(side_effect=fake_make_request)

    dp = Dispatcher(storage=MemoryStorage())
    mw = AppContextMiddleware(session_maker=session_maker, settings=settings)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_router(common.start_router)

    webapp = create_webapp(session_maker=session_maker, bot=bot, settings=settings)
    configure_webhook_route(webapp, dp, bot, settings, handle_in_background=False)

    server = TestServer(webapp)
    client = TestClient(server)
    await client.start_server()
    try:
        r_health = await client.get("/health")
        assert r_health.status == 200
        assert (await r_health.json())["ok"] is True

        update_payload = {
            "update_id": 10001,
            "message": {
                "message_id": 1,
                "date": 1762948800,
                "chat": {"id": 1000, "type": "private", "first_name": "Anya"},
                "from": {"id": 1000, "is_bot": False, "first_name": "Anya", "username": "anya"},
                "text": "/start",
                "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
            },
        }

        # Без секрету або з хибним секретом -> 401
        r_no_hdr = await client.post("/telegram/webhook", json=update_payload)
        assert r_no_hdr.status == 401
        r_bad_hdr = await client.post(
            "/telegram/webhook",
            json=update_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert r_bad_hdr.status == 401
        assert sent == []

        # З правильним секретом -> 200 і відправка відповіді на /start
        r_ok = await client.post(
            "/telegram/webhook",
            json=update_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "super_secret_token_42"},
        )
        assert r_ok.status == 200
        assert len(sent) == 1
        assert sent[0]["chat_id"] == 1000
        assert "Привіт, Аню!" in sent[0]["text"]
    finally:
        await client.close()
        await bot.session.close()
        await close_db(engine)
