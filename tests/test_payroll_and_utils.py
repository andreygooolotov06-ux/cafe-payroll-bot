from __future__ import annotations

from datetime import date
from io import BytesIO

import pytest
import pytest_asyncio
from openpyxl import load_workbook
from sqlalchemy.exc import IntegrityError

from app.database import (
    Adjustment,
    AdminPermissionSettings,
    DailyRevenue,
    DEFAULT_ROLES,
    Employee,
    Payout,
    Role,
    Shift,
    async_session_factory,
    close_db,
    create_db_engine,
    init_db,
)
from app.services.access import (
    ActorRole,
    bind_invite_code,
    get_admin_perms,
    get_connected_admin_employees,
    is_admin_role_name,
    resolve_actor,
    toggle_admin_perm,
)
from app.services.excel_export import build_payroll_excel
from app.services.payroll import (
    compute_shift_breakdown,
    delete_employee_safely,
    get_all_employees_summary,
    get_employee_summary,
    set_employee_banquet_rate,
)
from app.services.scheduler import run_owner_2300_reminder, run_staff_0900_reminders
from app.utils.datetime_utils import (
    format_date_with_day,
    get_period_bounds,
    parse_date_input,
    parse_date_range_input,
    parse_time_range,
)


@pytest_asyncio.fixture
async def db_session():
    engine = create_db_engine(":memory:")
    session_maker = async_session_factory(engine)
    await init_db(engine, session_maker)
    async with session_maker() as session:
        yield session
    await close_db(engine)


def test_datetime_and_time_range_parsing() -> None:
    d = parse_date_input("12.11.2026")
    assert d == date(2026, 11, 12)
    assert format_date_with_day(d) == "12.11.2026 (Чт)"

    with pytest.raises(ValueError):
        parse_date_input("31.02.2026")
    with pytest.raises(ValueError):
        parse_date_input("2026-11-12")

    assert parse_date_range_input("11.11.2026-12.11.2026") == (date(2026, 11, 11), date(2026, 11, 12))
    assert parse_date_range_input("12.11.2026") == (date(2026, 11, 12), date(2026, 11, 12))

    assert parse_time_range("10:00-22:00") == ("10:00", "22:00", 12.0)
    assert parse_time_range("18:00-02:00") == ("18:00", "02:00", 8.0)
    assert parse_time_range("09:30-18:00") == ("09:30", "18:00", 8.5)

    with pytest.raises(ValueError):
        parse_time_range("10:00-10:00")
    with pytest.raises(ValueError):
        parse_time_range("25:00-10:00")

    d1, d2, _ = get_period_bounds("week", date(2026, 11, 12))
    assert d1 == date(2026, 11, 9)  # Понеділок
    assert d2 == date(2026, 11, 12)


@pytest.mark.asyncio
async def test_access_invite_persistence_and_delete(db_session, tmp_path) -> None:
    db_file = tmp_path / "restart_test.db"
    engine1 = create_db_engine(db_file)
    sm1 = async_session_factory(engine1)
    await init_db(engine1, sm1)

    owner_ids = frozenset({999001})

    async with sm1() as s1:
        from sqlalchemy import select

        role_id = await s1.scalar(select(Role.id).where(Role.name == "Бариста"))
        assert role_id is not None
        emp = Employee(
            full_name="Олег Сушист",
            role_id=role_id,
            hourly_rate=120.0,
            revenue_percent=3.0,
            banquet_rate_per_person=None,
            invite_code="INV123",
        )
        s1.add(emp)
        await s1.commit()

        # Власниця
        owner_ctx = await resolve_actor(s1, 999001, owner_ids)
        assert owner_ctx.role == ActorRole.OWNER

        # Невідомий користувач
        stranger_ctx = await resolve_actor(s1, 555555, owner_ids)
        assert stranger_ctx.role == ActorRole.UNAUTHORIZED

        # Прив'язка по посиланню
        ok, linked, reason = await bind_invite_code(s1, "INV123", 111222, "oleg_sushi")
        assert ok is True
        assert reason == "ok"
        assert linked is not None
        assert linked.telegram_id == 111222

        # Чужий користувач пробує використати те саме посилання -> заборонено
        ok2, _, reason2 = await bind_invite_code(s1, "INV123", 777888, "hacker")
        assert ok2 is False
        assert reason2 == "forbidden"

    await close_db(engine1)

    # Повний рестарт бота з новим engine і сесією — має одразу впізнати по Telegram ID
    engine2 = create_db_engine(db_file)
    sm2 = async_session_factory(engine2)
    async with sm2() as s2:
        actor = await resolve_actor(s2, 111222, owner_ids)
        assert actor.role == ActorRole.EMPLOYEE
        assert actor.employee is not None
        assert actor.employee.full_name == "Олег Сушист"

        # Видалення працівника миттєво забирає доступ
        mode = await delete_employee_safely(s2, actor.employee)
        assert mode == "hard"
        after_del = await resolve_actor(s2, 111222, owner_ids)
        assert after_del.role == ActorRole.UNAUTHORIZED

    await close_db(engine2)


@pytest.mark.asyncio
async def test_owner_username_bootstrap_and_hijack_protection(db_session) -> None:
    owner_ids = frozenset()
    owner_usernames = frozenset({"anya_cafe"})

    # Перший вхід власниці по @username прив'язує її числовий telegram_id
    ctx1 = await resolve_actor(
        db_session,
        telegram_id=888001,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
        telegram_username="Anya_Cafe",
    )
    assert ctx1.role == ActorRole.OWNER

    # Навіть якщо вона змінить username (telegram_username=None), доступ зберігається по telegram_id
    ctx2 = await resolve_actor(
        db_session,
        telegram_id=888001,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
        telegram_username=None,
    )
    assert ctx2.role == ActorRole.OWNER

    # Чужий користувач не може перехопити той самий @username
    ctx_hijack = await resolve_actor(
        db_session,
        telegram_id=999999,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
        telegram_username="anya_cafe",
    )
    assert ctx_hijack.role == ActorRole.UNAUTHORIZED


@pytest.mark.asyncio
async def test_payroll_banquets_missing_data_and_ledger(db_session) -> None:
    from sqlalchemy import select

    role_id = await db_session.scalar(select(Role.id).where(Role.name == "Бариста"))
    assert role_id is not None
    emp = Employee(
        full_name="Марія Водій",
        role_id=role_id,
        telegram_id=222333,
        hourly_rate=100.0,
        revenue_percent=2.0,
        banquet_rate_per_person=None,  # Аня ще не вписала ставку банкету!
    )
    db_session.add(emp)
    await db_session.commit()
    await db_session.refresh(emp)

    day1 = date(2026, 11, 10)
    day2 = date(2026, 11, 11)

    # День 1: звичайна зміна + каса дня 25000
    s1 = Shift(
        employee_id=emp.id,
        work_date=day1,
        start_time="10:00",
        end_time="20:00",
        hours=10.0,
        hourly_rate_snapshot=emp.hourly_rate,
        percent_snapshot=emp.revenue_percent,
        is_banquet=False,
    )
    # День 2: банкет з 15 гостями, але Аня забула вказати тариф банкету
    s2 = Shift(
        employee_id=emp.id,
        work_date=day2,
        start_time="12:00",
        end_time="22:00",
        hours=10.0,
        hourly_rate_snapshot=emp.hourly_rate,
        percent_snapshot=emp.revenue_percent,
        is_banquet=True,
        banquet_guests=15,
        banquet_rate_snapshot=None,
    )
    db_session.add_all([s1, s2, DailyRevenue(work_date=day1, amount=25000.0)])
    await db_session.commit()

    # Захист від дублів на одну дату
    dup = Shift(
        employee_id=emp.id,
        work_date=day1,
        start_time="10:00",
        end_time="18:00",
        hours=8.0,
        hourly_rate_snapshot=100.0,
        percent_snapshot=2.0,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    await db_session.refresh(emp)

    # Перевіряємо визначення нестачі тарифу банкету ("Звернись до Ані")
    summary_before = await get_employee_summary(db_session, emp, day1, day2)
    assert summary_before.missing_banquet_rate_dates == [day2]
    assert summary_before.missing_revenue_dates == [day2]
    assert summary_before.base_pay == 2000.0
    assert summary_before.percent_pay == 500.0  # 2% від 25000 за day1
    assert summary_before.banquet_pay == 0.0

    # Аня заповнює ставку за 1 людину на банкеті = 50 грн -> старий банкет перераховується!
    updated_dates = await set_employee_banquet_rate(db_session, emp, 50.0)
    assert updated_dates == [day2]

    # Підвищуємо звичайну ставку працівнику — старі зміни НЕ мають змінитися
    emp.hourly_rate = 150.0
    await db_session.commit()

    # Додаємо премію +200, штраф -100 і виплату за 1 день (1500)
    db_session.add_all([
        Adjustment(employee_id=emp.id, adj_date=day2, kind="bonus", amount=200.0, comment="Молодець"),
        Adjustment(employee_id=emp.id, adj_date=day2, kind="fine", amount=100.0, comment="Запізнення"),
        Payout(employee_id=emp.id, paid_date=day2, amount=1500.0, comment="за 10.11.2026"),
    ])
    await db_session.commit()

    summary_after = await get_employee_summary(db_session, emp, day1, day2)
    assert summary_after.missing_banquet_rate_dates == []
    assert summary_after.base_pay == 2000.0  # збережений snapshot 100 грн/год!
    assert summary_after.banquet_pay == 750.0  # 15 * 50
    assert summary_after.earned_period == 3350.0  # 2000 + 500 + 750 + 200 - 100
    assert summary_after.paid_period == 1500.0
    assert summary_after.balance_due == 1850.0  # 3350 - 1500

    # Перевірка незаповненої кількості людей на банкеті ("Внесіть кількість людей")
    day3 = date(2026, 11, 12)
    s3 = Shift(
        employee_id=emp.id,
        work_date=day3,
        start_time="11:00",
        end_time="19:00",
        hours=8.0,
        hourly_rate_snapshot=150.0,
        percent_snapshot=2.0,
        is_banquet=True,
        banquet_guests=None,
        banquet_rate_snapshot=50.0,
    )
    bd3 = compute_shift_breakdown(s3, {})
    assert bd3.missing_banquet_guests is True
    assert bd3.missing_banquet_rate is False
    assert bd3.banquet_pay == 0.0

    # Загальна таблиця + Excel
    all_summaries = await get_all_employees_summary(db_session, day1, day2)
    assert len(all_summaries) == 1

    xlsx = build_payroll_excel(all_summaries, day1, day2)
    wb = load_workbook(BytesIO(xlsx))
    assert wb.sheetnames == ["Загальна звітність", "Деталізація змін"]
    ws = wb["Загальна звітність"]
    assert ws.cell(row=4, column=1).value == "Марія Водій"
    assert ws.cell(row=4, column=9).value == 3350.0
    assert ws.cell(row=4, column=11).value == 1850.0


@pytest.mark.asyncio
async def test_default_roles_and_existing_sqlite_migration(tmp_path) -> None:
    import sqlite3
    from sqlalchemy import select

    db_file = tmp_path / "legacy.db"
    # Імітуємо стару схему БД до додавання last_start_time / last_end_time та таблиць адміна/планувальника
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE roles (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(64) NOT NULL UNIQUE);
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name VARCHAR(120) NOT NULL,
            role_id INTEGER NOT NULL REFERENCES roles(id),
            telegram_id BIGINT UNIQUE,
            telegram_username VARCHAR(64),
            hourly_rate FLOAT NOT NULL DEFAULT 0.0,
            revenue_percent FLOAT NOT NULL DEFAULT 0.0,
            banquet_rate_per_person FLOAT,
            invite_code VARCHAR(32) UNIQUE,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO roles (id, name) VALUES (1, 'Сушист'), (2, 'Адміністратор');
        INSERT INTO employees (id, full_name, role_id, telegram_id) VALUES (1, 'Старий Юзер', 1, 555111);
        """
    )
    con.commit()
    con.close()

    engine = create_db_engine(db_file)
    sm = async_session_factory(engine)
    await init_db(engine, sm)

    async with sm() as s:
        role_names = set((await s.scalars(select(Role.name))).all())
        assert set(DEFAULT_ROLES).issubset(role_names)
        assert "Сушист" in role_names
        assert "Адміністратор" in role_names

        old_emp = await s.get(Employee, 1)
        assert old_emp is not None
        assert old_emp.full_name == "Старий Юзер"
        assert old_emp.last_start_time is None
        assert old_emp.last_end_time is None

        perms = await s.get(AdminPermissionSettings, 1)
        assert perms is not None
        assert perms.can_enter_revenue is True
        assert perms.can_edit_hours is False

    await close_db(engine)


@pytest.mark.asyncio
async def test_admin_perms_and_scheduler_dedup(db_session) -> None:
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    from sqlalchemy import select
    from app.config import Settings

    assert is_admin_role_name("Адмін") is True
    assert is_admin_role_name("адміністратор") is True
    assert is_admin_role_name("Бариста") is False

    perms0 = await get_admin_perms(db_session)
    assert perms0.can_enter_revenue is True
    assert perms0.can_view_staff_shifts is True
    assert perms0.can_edit_hours is False

    perms1 = await toggle_admin_perm(db_session, "can_edit_hours")
    assert perms1.can_edit_hours is True

    adm_role_id = await db_session.scalar(select(Role.id).where(Role.name == "Адмін"))
    barista_role_id = await db_session.scalar(select(Role.id).where(Role.name == "Бариста"))
    assert adm_role_id is not None and barista_role_id is not None

    adm = Employee(
        full_name="Аліна",
        role_id=adm_role_id,
        telegram_id=7001,
        hourly_rate=130.0,
        revenue_percent=2.0,
    )
    barista = Employee(
        full_name="Олег",
        role_id=barista_role_id,
        telegram_id=7002,
        hourly_rate=100.0,
        revenue_percent=2.0,
    )
    db_session.add_all([adm, barista])
    await db_session.commit()

    ctx_adm = await resolve_actor(db_session, 7001, frozenset({9000}))
    assert ctx_adm.role == ActorRole.ADMIN
    assert ctx_adm.admin_perms.can_edit_hours is True
    assert len(await get_connected_admin_employees(db_session)) == 1

    # Планувальник 23:00 і 09:00
    sent: list[dict] = []

    class DummyBot:
        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
            return SimpleNamespace(chat_id=chat_id)

    bot = DummyBot()
    settings = Settings(
        bot_token="123:ABC",
        owner_ids=frozenset({9000}),
        owner_usernames=frozenset(),
        timezone_name="Europe/Kyiv",
        tz=ZoneInfo("Europe/Kyiv"),
        db_path="ignored.db",
    )
    day1 = date(2026, 11, 12)
    day_prev = date(2026, 11, 11)

    # Немає змін за day1 -> 23:00 не спамить
    assert await run_owner_2300_reminder(db_session, bot, settings, day1, force=True) is False
    assert sent == []

    # Додаємо зміну за day1 без каси -> 23:00 має надіслати 1 раз (і не дублюватись!)
    db_session.add(
        Shift(
            employee_id=barista.id,
            work_date=day1,
            start_time="10:00",
            end_time="20:00",
            hours=10.0,
            hourly_rate_snapshot=100.0,
            percent_snapshot=2.0,
        )
    )
    # А у adm є банкет за day_prev без вказаних людей, зате є зміна за day_prev
    db_session.add(
        Shift(
            employee_id=adm.id,
            work_date=day_prev,
            start_time="10:00",
            end_time="20:00",
            hours=10.0,
            hourly_rate_snapshot=130.0,
            percent_snapshot=2.0,
            is_banquet=True,
            banquet_guests=None,
        )
    )
    await db_session.commit()

    assert await run_owner_2300_reminder(db_session, bot, settings, day1) is True
    assert len(sent) == 1 and sent[0]["chat_id"] == 9000
    # Повторний запуск того ж дня не дублює
    assert await run_owner_2300_reminder(db_session, bot, settings, day1) is False
    assert len(sent) == 1

    # Ранкові 09:00 нагадування для day1: barista забув вчора (day_prev), adm забула людей на банкеті
    sent.clear()
    cnt = await run_staff_0900_reminders(db_session, bot, day1)
    assert cnt == 2
    by_chat = {m["chat_id"]: m for m in sent}
    assert "Внести зміну" in by_chat[7002]["reply_markup"].inline_keyboard[0][0].text
    assert "Внести людей" in by_chat[7001]["reply_markup"].inline_keyboard[0][0].text

    # Повторний виклик 09:00 того ж дня не дублює
    assert await run_staff_0900_reminders(db_session, bot, day1) == 0
