from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Chat, Message, User
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.database import (
    Adjustment,
    DailyRevenue,
    Employee,
    IdempotencyRecord,
    Payout,
    PayoutRequest,
    Role,
    Shift,
    async_session_factory,
    close_db,
    create_db_engine,
    init_db,
)
from app.services.analytics import (
    build_theil_sen_revenue_forecast,
    compute_owner_analytics,
    modified_z_scores,
)
from app.services.payroll import compute_shift_breakdown, get_all_employees_summary, get_employee_summary
from app.utils.datetime_utils import parse_time_range
from app.webapp import build_telegram_init_data, create_webapp, verify_telegram_init_data
from app.webapp.auth import InitDataError

TEST_TOKEN = "123456:TEST_MINIAPP_TOKEN"


@pytest_asyncio.fixture
async def webapp_harness(tmp_path):
    db_file = tmp_path / "miniapp_test.db"
    engine = create_db_engine(db_file)
    session_maker = async_session_factory(engine)
    await init_db(engine, session_maker)

    settings = Settings(
        bot_token=TEST_TOKEN,
        owner_ids=frozenset({1000}),
        owner_usernames=frozenset(),
        timezone_name="Europe/Kyiv",
        tz=ZoneInfo("Europe/Kyiv"),
        db_path=db_file,
    )

    sent_messages: list[dict] = []
    bot = Bot(token=TEST_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def fake_make_request(_bot, method, timeout=None):
        name = method.__class__.__name__
        if name == "SendMessage":
            sent_messages.append({
                "chat_id": method.chat_id,
                "text": method.text,
                "reply_markup": method.reply_markup,
            })
            return Message(
                message_id=len(sent_messages),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if name == "SendDocument":
            sent_messages.append({
                "chat_id": method.chat_id,
                "document": method.document,
                "caption": method.caption,
            })
            return Message(
                message_id=len(sent_messages),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.caption or "",
            )
        if name == "GetMe":
            return User(id=999999, is_bot=True, first_name="CafeBot", username="my_cafe_bot")
        return True

    bot.session.make_request = AsyncMock(side_effect=fake_make_request)  # type: ignore[method-assign]

    app = create_webapp(session_maker=session_maker, bot=bot, settings=settings)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, session_maker, sent_messages
    finally:
        await client.close()
        await bot.session.close()
        await close_db(engine)


def test_init_data_hmac_and_decimal_math() -> None:
    good = build_telegram_init_data(1000, TEST_TOKEN, username="anya", first_name="Аня")
    parsed = verify_telegram_init_data(good, TEST_TOKEN)
    assert parsed.telegram_id == 1000
    assert parsed.username == "anya"

    # Підробка user_id без перепідписання -> помилка
    tampered = good.replace("1000", "9999")
    with pytest.raises(InitDataError):
        verify_telegram_init_data(tampered, TEST_TOKEN)

    # Прострочений auth_date -> помилка
    old = build_telegram_init_data(1000, TEST_TOKEN, auth_date=1_600_000_000)
    with pytest.raises(InitDataError):
        verify_telegram_init_data(old, TEST_TOKEN, max_age_seconds=3600)

    # Перевірка точного Decimal округлення годин та ставок (ROUND_HALF_UP)
    _, _, h = parse_time_range("09:00-17:20")  # 500 хв = 8.3333... -> 8.33
    assert h == 8.33

    sh = Shift(
        employee_id=1,
        work_date=date(2026, 11, 10),
        start_time="09:00",
        end_time="17:20",
        hours=h,
        hourly_rate_snapshot=120.55,
        percent_snapshot=3.33,
        is_banquet=False,
    )
    bd = compute_shift_breakdown(sh, {date(2026, 11, 10): 15432.10})
    # Decimal("8.33") * Decimal("120.55") = 1004.1815 -> 1004.18
    assert Decimal(str(bd.base_pay)) == Decimal("1004.18")
    # Decimal("15432.10") * Decimal("3.33") / 100 = 513.88893 -> 513.89
    assert Decimal(str(bd.percent_pay)) == Decimal("513.89")
    assert Decimal(str(bd.total_pay)) == Decimal("1518.07")


def test_theil_sen_forecast_and_modified_z_scores() -> None:
    ref = date(2026, 11, 20)
    # Менше 3 днів -> insufficient_data
    too_few = [
        DailyRevenue(work_date=ref - timedelta(days=1), amount=12000.0),
        DailyRevenue(work_date=ref, amount=14000.0),
    ]
    res_low = build_theil_sen_revenue_forecast(too_few, ref, recent_labor_ratio=0.25)
    assert res_low["status"] == "insufficient_data"

    # 14 днів зростаючої каси з тижневою сезонністю
    rows = []
    for i in range(14, 0, -1):
        d = ref - timedelta(days=i - 1)
        weekend_boost = 1.25 if d.weekday() in (4, 5, 6) else 1.0
        rows.append(DailyRevenue(work_date=d, amount=round((15000 + (14 - i) * 200) * weekend_boost, 2)))

    fc = build_theil_sen_revenue_forecast(rows, ref, recent_labor_ratio=0.28)
    assert fc["status"] == "ready"
    assert len(fc["days"]) == 7
    assert fc["trend_per_day"] > 0
    assert fc["projected_7d_low"] <= fc["projected_7d_revenue"] <= fc["projected_7d_high"]
    for day_fc in fc["days"]:
        assert day_fc["low_revenue"] <= day_fc["predicted_revenue"] <= day_fc["high_revenue"]
        assert day_fc["estimated_payroll"] > 0

    # Modified Z-score виявляє різкий викид (18 год серед 8 год)
    z = modified_z_scores([8.0, 8.2, 7.8, 8.1, 8.0, 7.9, 18.0])
    assert abs(z[-1]) > 2.8

    from app.services.tunnel import _LOCALHOSTRUN_RE, _TRYCLOUDFLARE_RE

    assert _TRYCLOUDFLARE_RE.search("failed connecting to https://api.trycloudflare.com/tunnel") is None
    assert _TRYCLOUDFLARE_RE.search("| https://sweet-latte-cafe.trycloudflare.com |").group(0) == "https://sweet-latte-cafe.trycloudflare.com"
    assert _LOCALHOSTRUN_RE.search("tunneled, https://b2796e642dacb0.lhr.life").group(0) == "https://b2796e642dacb0.lhr.life"


@pytest.mark.asyncio
async def test_miniapp_end_to_end_rbac_idempotency_and_analytics(webapp_harness) -> None:
    client, session_maker, sent = webapp_harness

    # Підготовка працівників у БД
    async with session_maker() as s:
        barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        adm_id = await s.scalar(select(Role.id).where(Role.name == "Адмін"))
        assert barista_id and adm_id

        emp = Employee(
            full_name="Іван Бариста",
            role_id=barista_id,
            hourly_rate=100.0,
            revenue_percent=5.0,
            banquet_rate_per_person=None,
            invite_code="MINIINV1",
        )
        adm = Employee(
            full_name="Аліна Адмін",
            role_id=adm_id,
            telegram_id=5001,
            hourly_rate=140.0,
            revenue_percent=0.0,
        )
        s.add_all([emp, adm])
        await s.commit()
        await s.refresh(emp)
        emp_id = emp.id

    owner_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(1000, TEST_TOKEN, "anya")}
    adm_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(5001, TEST_TOKEN, "alina")}

    # 1. Без підпису / чужий юзер -> 401 / 403
    r_no_auth = await client.get("/api/me")
    assert r_no_auth.status == 401

    stranger_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(7777, TEST_TOKEN, "stranger")}
    r_stranger = await client.get("/api/me", headers=stranger_hdr)
    assert r_stranger.status == 403

    # 2. Працівник відкриває Mini App через start_param="MINIINV1" -> автоматична прив'язка + пуш Ані!
    emp_invite_hdr = {
        "X-Telegram-Init-Data": build_telegram_init_data(
            2001, TEST_TOKEN, username="ivan_mini", start_param="MINIINV1"
        )
    }
    sent.clear()
    r_me_emp = await client.get("/api/me", headers=emp_invite_hdr)
    assert r_me_emp.status == 200
    me_emp_data = await r_me_emp.json()
    assert me_emp_data["role"] == "employee"
    assert me_emp_data["employee"]["full_name"] == "Іван Бариста"
    assert any("Іван Бариста" in m["text"] and "Підключено" in m["text"] for m in sent if m["chat_id"] == 1000)

    emp_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(2001, TEST_TOKEN, username="ivan_mini")}

    # 3. Працівник вносить зміну з X-Idempotency-Key -> повторний запит з тим же ключем НЕ створює дубль
    sent.clear()
    shift_payload = {
        "work_date": "2026-11-12",
        "start_time": "10:00",
        "end_time": "22:00",
        "is_banquet": True,
        "banquet_guests": 10,
    }
    r_sh1 = await client.post(
        "/api/my/shifts",
        headers={**emp_hdr, "X-Idempotency-Key": "shift-req-1"},
        json=shift_payload,
    )
    assert r_sh1.status == 201
    d_sh1 = await r_sh1.json()
    assert d_sh1["needs_owner_banquet_rate"] is True
    assert d_sh1["shift"]["base_pay"] == 1200.0
    # Попередження Ані про відсутній банкетний тариф надійшло 1 раз
    assert sum(1 for m in sent if m["chat_id"] == 1000) == 1

    # Повтор з тим же ключем ідемпотентності повертає кешований 201 без дубля чи другого пуша!
    r_sh_replay = await client.post(
        "/api/my/shifts",
        headers={**emp_hdr, "X-Idempotency-Key": "shift-req-1"},
        json=shift_payload,
    )
    assert r_sh_replay.status == 201
    assert sum(1 for m in sent if m["chat_id"] == 1000) == 1

    # Зміна без ключа ідемпотентності на ту ж дату -> 409 conflict
    r_sh_dup = await client.post("/api/my/shifts", headers=emp_hdr, json=shift_payload)
    assert r_sh_dup.status == 409

    # 4. Аня вказує банкетний тариф 60 грн/люд -> автоперерахунок + пуш працівнику
    sent.clear()
    r_patch = await client.patch(
        f"/api/owner/staff/{emp_id}",
        headers=owner_hdr,
        json={"banquet_rate_per_person": 60},
    )
    assert r_patch.status == 200
    patch_data = await r_patch.json()
    assert patch_data["recalculated_banquet_dates"] == ["2026-11-12"]
    assert any("банкетний тариф" in m["text"].lower() for m in sent if m["chat_id"] == 2001)

    # 5. Права дефолтного Адміна: бачить персонал без зарплат, вносить касу без чужих %, заблокована в аналітиці/виплатах
    r_adm_analytics = await client.get("/api/owner/analytics?preset=month", headers=adm_hdr)
    assert r_adm_analytics.status == 403

    r_adm_payouts = await client.get("/api/owner/payouts", headers=adm_hdr)
    assert r_adm_payouts.status == 403

    r_adm_perms = await client.get("/api/owner/admin-perms", headers=adm_hdr)
    assert r_adm_perms.status == 403

    r_adm_staff = await client.get("/api/owner/staff?preset=month", headers=adm_hdr)
    assert r_adm_staff.status == 200
    adm_staff_data = await r_adm_staff.json()
    assert adm_staff_data["can_view_money"] is False
    ivan_row = next(x for x in adm_staff_data["staff"] if x["id"] == emp_id)
    assert ivan_row["hourly_rate"] is None
    assert ivan_row["balance_due"] is None

    # Аліна вносить касу 20000 грн за 12.11.2026 -> 200, не показує 5% Івана в JSON, але Іван і Аня отримують пуші!
    sent.clear()
    r_adm_rev = await client.put(
        "/api/owner/revenues/2026-11-12",
        headers=adm_hdr,
        json={"amount": 20000},
    )
    assert r_adm_rev.status == 200
    rev_json = await r_adm_rev.json()
    assert "percent_bonuses" not in rev_json
    assert any("+1 000 грн" in m["text"] for m in sent if m["chat_id"] == 2001)
    assert any("Адмін Аліна Адмін" in m["text"] and "20 000 грн" in m["text"] for m in sent if m["chat_id"] == 1000)

    # 6. Аня відкриває повну аналітику за листопад 2026:
    # 1200 (база) + 1000 (5% від 20000) + 600 (10*60 банкет) = 2800 грн
    # Симулятор маржі, каса та AI-прогнози каси прибрані; загальний залишок до видачі команді — на місці!
    r_own_analytics = await client.get(
        "/api/owner/analytics?from=2026-11-01&to=2026-11-30",
        headers=owner_hdr,
    )
    assert r_own_analytics.status == 200
    an_data = await r_own_analytics.json()
    assert an_data["kpis"]["total_payroll"] == 2800.0
    assert an_data["kpis"]["total_balance_due"] == 2800.0
    assert an_data["kpis"]["paid_period"] == 0.0
    assert an_data["kpis"]["total_hours"] == 12.0
    assert "total_revenue" not in an_data["kpis"]
    assert "labor_ratio_pct" not in an_data["kpis"]
    assert "forecast" not in an_data
    assert "what_if_defaults" not in an_data

    # 7. Excel download + надсилання у чат
    r_xlsx = await client.get(
        "/api/owner/reports/excel?from=2026-11-01&to=2026-11-30",
        headers=owner_hdr,
    )
    assert r_xlsx.status == 200
    xlsx_bytes = await r_xlsx.read()
    assert xlsx_bytes[:2] == b"PK"

    sent.clear()
    r_send_xlsx = await client.post(
        "/api/owner/reports/send_excel",
        headers=owner_hdr,
        json={"from": "2026-11-01", "to": "2026-11-30"},
    )
    assert r_send_xlsx.status == 200
    assert any("document" in m and m["chat_id"] == 1000 for m in sent)

    # 8. Ідемпотентна виплата: подвійний запит з однаковим ключем списує залишок ТІЛЬКИ 1 раз!
    sent.clear()
    pay_hdr = {**owner_hdr, "X-Idempotency-Key": "pay-ivan-full-1"}
    r_p1 = await client.post(
        "/api/owner/payouts",
        headers=pay_hdr,
        json={"employee_id": emp_id, "amount": 2800, "comment": "за листопад"},
    )
    r_p2 = await client.post(
        "/api/owner/payouts",
        headers=pay_hdr,
        json={"employee_id": emp_id, "amount": 2800, "comment": "за листопад"},
    )
    assert r_p1.status == 201 and r_p2.status == 201
    async with session_maker() as s:
        payouts = (await s.scalars(select(Payout).where(Payout.employee_id == emp_id))).all()
        assert len(payouts) == 1
        emp_ref = await s.get(Employee, emp_id)
        summary = await get_employee_summary(s, emp_ref, date(2026, 11, 1), date(2026, 11, 30))
        assert summary.balance_due == 0.0


@pytest.mark.asyncio
async def test_payout_requests_and_card_workflow_miniapp(webapp_harness) -> None:
    client, session_maker, sent = webapp_harness

    async with session_maker() as s:
        barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        adm_id = await s.scalar(select(Role.id).where(Role.name == "Адмін"))
        assert barista_id and adm_id
        emp = Employee(
            full_name="Марія Бариста",
            role_id=barista_id,
            telegram_id=3001,
            hourly_rate=120.0,
            revenue_percent=0.0,
        )
        adm = Employee(
            full_name="Олег Адмін",
            role_id=adm_id,
            telegram_id=5002,
            hourly_rate=150.0,
            revenue_percent=0.0,
        )
        s.add_all([emp, adm])
        await s.commit()
        await s.refresh(emp)
        emp_id = emp.id
        # 2 зміни по 10 год -> баланс 2400 грн
        s.add_all([
            Shift(
                employee_id=emp_id,
                work_date=date(2026, 11, 10),
                start_time="09:00",
                end_time="19:00",
                hours=10.0,
                hourly_rate_snapshot=120.0,
                percent_snapshot=0.0,
            ),
            Shift(
                employee_id=emp_id,
                work_date=date(2026, 11, 11),
                start_time="09:00",
                end_time="19:00",
                hours=10.0,
                hourly_rate_snapshot=120.0,
                percent_snapshot=0.0,
            ),
        ])
        await s.commit()

    owner_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(1000, TEST_TOKEN, "anya")}
    emp_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(3001, TEST_TOKEN, "maria")}
    adm_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(5002, TEST_TOKEN, "oleg")}

    # 1. Без картки створити запит не можна; невалідна картка -> 400
    r_no_card = await client.post(
        "/api/my/payout-requests",
        headers=emp_hdr,
        json={"amount": 1000},
    )
    assert r_no_card.status == 400

    r_bad_card = await client.put(
        "/api/my/card",
        headers=emp_hdr,
        json={"card_number": "12345"},
    )
    assert r_bad_card.status == 400

    # 2. Працівник зберігає номер картки -> коректно форматується і маскується
    r_good_card = await client.put(
        "/api/my/card",
        headers=emp_hdr,
        json={"card_number": "4149-4991 1234-5678"},
    )
    assert r_good_card.status == 200
    card_data = await r_good_card.json()
    assert card_data["card_digits"] == "4149499112345678"
    assert card_data["card_display"] == "4149 4991 1234 5678"
    assert card_data["card_masked"] == "4149 •••• •••• 5678"

    r_me = await client.get("/api/me", headers=emp_hdr)
    me_data = await r_me.json()
    assert me_data["employee"]["card_digits"] == "4149499112345678"

    # 3. Створення запиту на виплату (ідемпотентне) + пуш Ані
    sent.clear()
    req_hdr = {**emp_hdr, "X-Idempotency-Key": "req-maria-1"}
    r_req1 = await client.post(
        "/api/my/payout-requests",
        headers=req_hdr,
        json={"amount": 1500, "comment": "на картку за тиждень"},
    )
    assert r_req1.status == 201
    d_req1 = await r_req1.json()
    req_id = d_req1["request"]["id"]
    assert d_req1["request"]["status"] == "pending"
    assert any("Марія Бариста" in m["text"] and "1 500" in m["text"] for m in sent if m["chat_id"] == 1000)

    # Повтор з тим же ключем не дублює запит чи сповіщення
    r_req1_replay = await client.post(
        "/api/my/payout-requests",
        headers=req_hdr,
        json={"amount": 1500, "comment": "на картку за тиждень"},
    )
    assert r_req1_replay.status == 201
    assert sum(1 for m in sent if m["chat_id"] == 1000) == 1

    # Другий одночасний pending-запит з іншим ключем заборонено -> 400
    r_req_dup = await client.post(
        "/api/my/payout-requests",
        headers=emp_hdr,
        json={"amount": 500},
    )
    assert r_req_dup.status == 400

    # 4. До прийняття Анею запит у /api/owner/payouts має лише замасковану картку, а не повні цифри
    r_adm_reqs = await client.get("/api/owner/payout-requests", headers=adm_hdr)
    assert r_adm_reqs.status == 403

    r_pay_overview = await client.get("/api/owner/payouts", headers=owner_hdr)
    assert r_pay_overview.status == 200
    ov_data = await r_pay_overview.json()
    assert ov_data["total_balance_due"] == 2400.0
    pending_item = next(x for x in ov_data["payout_requests"] if x["id"] == req_id)
    assert pending_item["status"] == "pending"
    assert pending_item["card_masked"] == "4149 •••• •••• 5678"
    assert "card_digits" not in pending_item

    # 5. Аня приймає запит -> статус approved, одразу повертається повний номер картки і працівнику йде пуш!
    sent.clear()
    r_acc = await client.post(f"/api/owner/payout-requests/{req_id}/accept", headers=owner_hdr)
    assert r_acc.status == 200
    acc_data = await r_acc.json()
    assert acc_data["request"]["status"] == "approved"
    assert acc_data["request"]["card_digits"] == "4149499112345678"
    assert acc_data["request"]["card_display"] == "4149 4991 1234 5678"
    assert any("прийняла твій запит" in m["text"].lower() for m in sent if m["chat_id"] == 3001)

    # І в списку /api/owner/payouts тепер картка теж відкрита
    r_pay_after_acc = await client.get("/api/owner/payouts", headers=owner_hdr)
    acc_item = next(x for x in (await r_pay_after_acc.json())["payout_requests"] if x["id"] == req_id)
    assert acc_item["card_digits"] == "4149499112345678"

    # 6. Аня позначає запит виплаченим -> списує залишок 2400 -> 900, створює рівно 1 Payout (ідемпотентно)
    sent.clear()
    paid_hdr = {**owner_hdr, "X-Idempotency-Key": "pay-req-maria-1"}
    r_paid1 = await client.post(f"/api/owner/payout-requests/{req_id}/pay", headers=paid_hdr)
    r_paid2 = await client.post(f"/api/owner/payout-requests/{req_id}/pay", headers=paid_hdr)
    assert r_paid1.status == 201 and r_paid2.status == 201
    paid_data = await r_paid1.json()
    assert paid_data["amount"] == 1500.0
    assert paid_data["balance_due"] == 900.0
    assert any("1 500" in m["text"] and "900" in m["text"] for m in sent if m["chat_id"] == 3001)

    r_pay_after_pay = await client.get("/api/owner/payouts", headers=owner_hdr)
    assert (await r_pay_after_pay.json())["total_balance_due"] == 900.0

    # 7. Скасування працівником та відхилення Анею
    r_req2 = await client.post("/api/my/payout-requests", headers=emp_hdr, json={"amount": 400})
    req2_id = (await r_req2.json())["request"]["id"]
    r_cancel = await client.post(f"/api/my/payout-requests/{req2_id}/cancel", headers=emp_hdr)
    assert r_cancel.status == 200
    assert (await r_cancel.json())["request"]["status"] == "cancelled"

    r_req3 = await client.post("/api/my/payout-requests", headers=emp_hdr, json={"amount": 900})
    req3_id = (await r_req3.json())["request"]["id"]
    sent.clear()
    r_rej = await client.post(
        f"/api/owner/payout-requests/{req3_id}/reject",
        headers=owner_hdr,
        json={"reason": "Скину завтра вранці"},
    )
    assert r_rej.status == 200
    assert (await r_rej.json())["request"]["status"] == "rejected"
    assert any("Скину завтра вранці" in m["text"] for m in sent if m["chat_id"] == 3001)

    async with session_maker() as s:
        all_reqs = (await s.scalars(select(PayoutRequest).where(PayoutRequest.employee_id == emp_id))).all()
        assert len(all_reqs) == 3


@pytest.mark.asyncio
async def test_get_all_employees_summary_parity(tmp_path) -> None:
    db_file = tmp_path / "bulk_parity.db"
    engine = create_db_engine(db_file)
    session_maker = async_session_factory(engine)
    await init_db(engine, session_maker)

    try:
        d1 = date(2026, 11, 1)
        d2 = date(2026, 11, 30)

        async with session_maker() as s:
            barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
            cook_id = await s.scalar(select(Role.id).where(Role.name == "Кухар"))
            assert barista_id and cook_id

            emp1 = Employee(
                full_name="Антон Бариста",
                role_id=barista_id,
                hourly_rate=115.5,
                revenue_percent=3.5,
                banquet_rate_per_person=60.0,
                is_active=True,
            )
            emp2 = Employee(
                full_name="Богдан Кухар",
                role_id=cook_id,
                hourly_rate=100.0,
                revenue_percent=0.0,
                banquet_rate_per_person=None,
                is_active=True,
            )
            emp3 = Employee(
                full_name="Старий Архівний",
                role_id=cook_id,
                hourly_rate=95.0,
                is_active=False,
            )
            emp4 = Employee(
                full_name="Звільнений У Періоді",
                role_id=barista_id,
                hourly_rate=90.0,
                is_active=False,
            )
            s.add_all([emp1, emp2, emp3, emp4])
            await s.commit()
            for emp in (emp1, emp2, emp3, emp4):
                await s.refresh(emp)

            s.add_all([
                DailyRevenue(work_date=date(2026, 10, 28), amount=12000.0),
                DailyRevenue(work_date=date(2026, 11, 5), amount=15432.10),
                # emp1: до періоду і в періоді (одна каса є, другої немає)
                Shift(
                    employee_id=emp1.id,
                    work_date=date(2026, 10, 28),
                    start_time="09:00",
                    end_time="17:00",
                    hours=8.0,
                    hourly_rate_snapshot=115.5,
                    percent_snapshot=3.5,
                ),
                Shift(
                    employee_id=emp1.id,
                    work_date=date(2026, 11, 5),
                    start_time="09:00",
                    end_time="17:20",
                    hours=8.33,
                    hourly_rate_snapshot=115.5,
                    percent_snapshot=3.5,
                    is_banquet=True,
                    banquet_guests=11,
                    banquet_rate_snapshot=60.0,
                ),
                Shift(
                    employee_id=emp1.id,
                    work_date=date(2026, 11, 6),
                    start_time="10:00",
                    end_time="18:00",
                    hours=8.0,
                    hourly_rate_snapshot=115.5,
                    percent_snapshot=3.5,
                ),
                # emp2: відсутній банкетний тариф та гості
                Shift(
                    employee_id=emp2.id,
                    work_date=date(2026, 11, 8),
                    start_time="10:00",
                    end_time="20:00",
                    hours=10.0,
                    hourly_rate_snapshot=100.0,
                    percent_snapshot=0.0,
                    is_banquet=True,
                    banquet_guests=None,
                    banquet_rate_snapshot=None,
                ),
                # emp3: неактивний без руху у періоді
                Shift(
                    employee_id=emp3.id,
                    work_date=date(2026, 10, 15),
                    start_time="09:00",
                    end_time="15:00",
                    hours=6.0,
                    hourly_rate_snapshot=95.0,
                    percent_snapshot=0.0,
                ),
                # emp4: неактивний з активністю у періоді
                Shift(
                    employee_id=emp4.id,
                    work_date=date(2026, 11, 2),
                    start_time="09:00",
                    end_time="14:00",
                    hours=5.0,
                    hourly_rate_snapshot=90.0,
                    percent_snapshot=0.0,
                ),
                Adjustment(employee_id=emp1.id, adj_date=date(2026, 10, 29), kind="bonus", amount=300.0),
                Adjustment(employee_id=emp1.id, adj_date=date(2026, 11, 10), kind="bonus", amount=250.0),
                Adjustment(employee_id=emp1.id, adj_date=date(2026, 11, 11), kind="fine", amount=75.5),
                Payout(employee_id=emp1.id, paid_date=date(2026, 10, 30), amount=500.0),
                Payout(employee_id=emp1.id, paid_date=date(2026, 11, 12), amount=1000.0),
                Adjustment(employee_id=emp2.id, adj_date=date(2026, 11, 9), kind="bonus", amount=150.0),
            ])
            await s.commit()

            all_bulk = await get_all_employees_summary(s, d1, d2, include_inactive_with_activity=True)
            assert [x.employee.id for x in all_bulk] == [emp1.id, emp2.id, emp4.id]

            active_bulk = await get_all_employees_summary(s, d1, d2, include_inactive_with_activity=False)
            assert [x.employee.id for x in active_bulk] == [emp1.id, emp2.id]

            for item in all_bulk:
                single = await get_employee_summary(s, item.employee, d1, d2)
                assert item.shifts_count == single.shifts_count
                assert item.total_hours == single.total_hours
                assert item.base_pay == single.base_pay
                assert item.percent_pay == single.percent_pay
                assert item.banquet_pay == single.banquet_pay
                assert item.bonuses == single.bonuses
                assert item.fines == single.fines
                assert item.earned_period == single.earned_period
                assert item.paid_period == single.paid_period
                assert item.all_time_earned == single.all_time_earned
                assert item.all_time_paid == single.all_time_paid
                assert item.balance_due == single.balance_due
                assert item.missing_banquet_rate_dates == single.missing_banquet_rate_dates
                assert item.missing_banquet_guests_dates == single.missing_banquet_guests_dates
                assert item.missing_revenue_dates == single.missing_revenue_dates
    finally:
        await close_db(engine)


@pytest.mark.asyncio
async def test_bootstrap_endpoint_and_bundled_assets(webapp_harness, tmp_path) -> None:
    client, session_maker, _ = webapp_harness

    async with session_maker() as s:
        barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        adm_id = await s.scalar(select(Role.id).where(Role.name == "Адмін"))
        assert barista_id and adm_id

        emp = Employee(
            full_name="Тарас Бариста",
            role_id=barista_id,
            telegram_id=2001,
            hourly_rate=110.0,
            revenue_percent=0.0,
            card_number="4149499112345678",
        )
        adm = Employee(
            full_name="Аліна Адмін",
            role_id=adm_id,
            telegram_id=5001,
            hourly_rate=140.0,
            revenue_percent=0.0,
        )
        s.add_all([emp, adm])
        await s.commit()
        await s.refresh(emp)
        s.add(
            Shift(
                employee_id=emp.id,
                work_date=date(2026, 11, 12),
                start_time="09:00",
                end_time="19:00",
                hours=10.0,
                hourly_rate_snapshot=110.0,
                percent_snapshot=0.0,
            )
        )
        s.add(
            PayoutRequest(
                employee_id=emp.id,
                amount=500.0,
                card_snapshot="4149499112345678",
                status="pending",
            )
        )
        await s.commit()

    owner_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(1000, TEST_TOKEN, "anya")}
    adm_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(5001, TEST_TOKEN, "alina")}
    emp_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(2001, TEST_TOKEN, "taras")}
    stranger_hdr = {"X-Telegram-Init-Data": build_telegram_init_data(7777, TEST_TOKEN, "stranger")}

    # 1. /api/bootstrap RBAC і цільові payload-и
    assert (await client.get("/api/bootstrap", headers=stranger_hdr)).status == 403

    r_own = await client.get("/api/bootstrap", headers=owner_hdr)
    assert r_own.status == 200
    d_own = await r_own.json()
    assert d_own["workspace"] == "admin"
    assert d_own["initial_tab"] == "pulse"
    assert "analytics" in d_own
    assert d_own["me"]["pending_payout_requests"] == 1

    r_own_staff = await client.get("/api/bootstrap?tab=staff", headers=owner_hdr)
    assert r_own_staff.status == 200
    d_own_staff = await r_own_staff.json()
    assert d_own_staff["initial_tab"] == "staff"
    assert d_own_staff["staff_list"]["can_view_money"] is True

    r_own_pay = await client.get("/api/bootstrap?tab=payouts", headers=owner_hdr)
    assert r_own_pay.status == 200
    assert (await r_own_pay.json())["initial_tab"] == "payouts"

    # Адмін за замовчуванням потрапляє на staff без чужих зарплат, або в personal shift
    r_adm = await client.get("/api/bootstrap", headers=adm_hdr)
    assert r_adm.status == 200
    d_adm = await r_adm.json()
    assert d_adm["workspace"] == "admin"
    assert d_adm["initial_tab"] == "staff"
    assert d_adm["staff_list"]["can_view_money"] is False
    taras_row = next(x for x in d_adm["staff_list"]["staff"] if x["full_name"] == "Тарас Бариста")
    assert taras_row["hourly_rate"] is None
    assert taras_row["balance_due"] is None

    r_adm_shift = await client.get("/api/bootstrap?tab=shift", headers=adm_hdr)
    assert r_adm_shift.status == 200
    d_adm_shift = await r_adm_shift.json()
    assert d_adm_shift["workspace"] == "personal"
    assert d_adm_shift["initial_tab"] == "shift"
    assert "my_dash" in d_adm_shift

    # Працівник отримує my_dash + запити на виплату зі своїм реальним ПІБ (без lazy loading)
    r_emp = await client.get("/api/bootstrap", headers=emp_hdr)
    assert r_emp.status == 200
    d_emp = await r_emp.json()
    assert d_emp["workspace"] == "personal"
    assert d_emp["initial_tab"] == "shift"
    assert d_emp["my_dash"]["payout_requests"][0]["employee_name"] == "Тарас Бариста"

    # 2. Бандл HTML, Gzip, ETag / 304, валідний мініфікований JS
    r_idx = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r_idx.status == 200
    assert r_idx.headers.get("Content-Encoding") == "gzip"
    assert r_idx.headers.get("Cache-Control") == "no-cache"
    etag = r_idx.headers.get("ETag")
    assert etag

    html = await r_idx.text()
    assert "<style>" in html and "<script>" in html
    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert 'src="/static/' not in html
    assert 'href="/static/' not in html
    assert '<img src="/icon.svg"' not in html
    assert html.find('id="boot-screen"') < html.find("https://telegram.org/js/telegram-web-app.js")

    r_304 = await client.get("/", headers={"If-None-Match": etag, "Accept-Encoding": "gzip"})
    assert r_304.status == 304

    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert scripts
    js_bundle = scripts[-1]
    if shutil.which("node"):
        tmp_js = tmp_path / "bundled_app.js"
        tmp_js.write_text(js_bundle, encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(tmp_js)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
