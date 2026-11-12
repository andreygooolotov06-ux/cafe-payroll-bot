from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, MenuButtonCommands, Message, Update, User

from sqlalchemy import select

from app.config import Settings
from app.database import Employee, PayoutRequest, Role, async_session_factory, close_db, create_db_engine, init_db
from app.handlers import common, employee, owner_finance, owner_staff
from app.keyboards.reply import (
    BTN_EMP_MY_SALARY,
    BTN_EMP_PAYOUT_REQ,
    BTN_EMP_SHIFT,
    BTN_OWN_ADMIN_PERMS,
    BTN_OWN_PAYOUTS,
    BTN_OWN_REPORTS,
    BTN_OWN_REVENUE,
    BTN_OWN_STAFF,
)
from app.middlewares import AppContextMiddleware
from app.services.tunnel import miniapp_runtime, restore_telegram_menu_button


@pytest_asyncio.fixture
async def bot_harness(tmp_path):
    db_file = tmp_path / "integration.db"
    engine = create_db_engine(db_file)
    session_maker = async_session_factory(engine)
    await init_db(engine, session_maker)

    from zoneinfo import ZoneInfo

    settings = Settings(
        bot_token="123456:TESTTOKEN",
        owner_ids=frozenset({1000}),
        owner_usernames=frozenset(),
        timezone_name="Europe/Kyiv",
        tz=ZoneInfo("Europe/Kyiv"),
        db_path=db_file,
    )

    sent_messages: list[dict] = []

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    async def fake_call(method, timeout=None):
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
        if name == "EditMessageText":
            sent_messages.append({
                "chat_id": method.chat_id,
                "text": method.text,
                "reply_markup": method.reply_markup,
                "edited": True,
            })
            return True
        if name == "AnswerCallbackQuery":
            sent_messages.append({
                "callback_answer": method.text,
                "show_alert": method.show_alert,
            })
            return True
        if name == "GetMe":
            return User(id=999999, is_bot=True, first_name="CafeBot", username="my_cafe_bot")
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
        if name == "SetChatMenuButton":
            sent_messages.append({"set_chat_menu_button": method.menu_button})
            return True
        return True

    async def fake_make_request(_bot, method, timeout=None):
        return await fake_call(method, timeout)

    bot.session.make_request = AsyncMock(side_effect=fake_make_request)  # type: ignore[method-assign]

    dp = Dispatcher(storage=MemoryStorage())
    mw = AppContextMiddleware(session_maker=session_maker, settings=settings)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    routers = (
        common.start_router,
        employee.router,
        owner_staff.router,
        owner_finance.router,
        common.fallback_router,
    )
    for r in routers:
        r._parent_router = None  # noqa: SLF001
        dp.include_router(r)

    try:
        yield dp, bot, session_maker, sent_messages
    finally:
        dp.sub_routers.clear()
        for r in routers:
            r._parent_router = None  # noqa: SLF001
        await bot.session.close()
        await close_db(engine)


def _msg_update(update_id: int, user_id: int, text: str, bot: Bot, username: str = "user") -> Update:
    msg = Message(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test", username=username),
        text=text,
    ).as_(bot)
    return Update(update_id=update_id, message=msg)


def _cb_update(update_id: int, user_id: int, data: str, bot: Bot) -> Update:
    prev_msg = Message(
        message_id=500,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        text="prev",
    ).as_(bot)
    cb = CallbackQuery(
        id=str(update_id),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        chat_instance="ci",
        data=data,
        message=prev_msg,
    ).as_(bot)
    return Update(update_id=update_id, callback_query=cb)


def _last_text_msg(sent: list[dict], chat_id: int | None = None) -> dict:
    for item in reversed(sent):
        if "text" in item and (chat_id is None or item.get("chat_id") == chat_id):
            return item
    raise AssertionError(f"No text message found in sent={sent!r}")


@pytest.mark.asyncio
async def test_end_to_end_access_banquet_alert_and_duplicate_guard(bot_harness) -> None:
    dp, bot, session_maker, sent = bot_harness

    # Створюємо працівника без прив'язаного Telegram і без банкетного тарифу
    async with session_maker() as s:
        role_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        assert role_id is not None
        s.add(
            Employee(
                full_name="Іван Сушист",
                role_id=role_id,
                hourly_rate=110.0,
                revenue_percent=3.0,
                banquet_rate_per_person=None,
                invite_code="JOIN123",
            )
        )
        await s.commit()

    # 1. Чужий користувач отримує "Недостатньо прав"
    await dp.feed_update(bot, _msg_update(1, 7777, "/start", bot))
    assert "Недостатньо прав" in sent[-1]["text"]

    # 2. Аня (власниця) заходить через /start
    await dp.feed_update(bot, _msg_update(2, 1000, "/start", bot, username="anya"))
    assert "Привіт, Аню!" in sent[-1]["text"]

    # 3. Працівник заходить по інвайт-посиланню -> прив'язується, Аня отримує сповіщення
    sent.clear()
    await dp.feed_update(bot, _msg_update(3, 2001, "/start JOIN123", bot, username="ivan"))
    assert any("успішно підключено" in m["text"] for m in sent if m.get("chat_id") == 2001)
    assert any(
        "Іван Сушист" in m["text"] and "Підключено" in m["text"]
        for m in sent
        if m.get("chat_id") == 1000
    )

    # 4. Хтось інший пробує той же інвайт або адмін-кнопку -> "Недостатньо прав"
    await dp.feed_update(bot, _msg_update(4, 7777, "/start JOIN123", bot))
    assert "Недостатньо прав" in sent[-1]["text"]

    await dp.feed_update(bot, _msg_update(5, 2001, BTN_OWN_STAFF, bot))
    assert "Недостатньо прав" in sent[-1]["text"]

    await dp.feed_update(bot, _cb_update(6, 2001, "own:staff_list", bot))
    assert "Недостатньо прав" in sent[-1]["callback_answer"]

    # 5. Працівник вносить зміну з банкетом за 12.11.2026
    await dp.feed_update(bot, _msg_update(7, 2001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(8, 2001, "emp:date:2026-11-12", bot))
    await dp.feed_update(bot, _msg_update(9, 2001, "10:00-22:00", bot))
    await dp.feed_update(bot, _cb_update(10, 2001, "emp:banquet:yes", bot))
    sent.clear()
    await dp.feed_update(bot, _msg_update(11, 2001, "14", bot))

    # Перевіряємо, що Аня отримала сповіщення з кнопкою вказати ставку, а працівник — "звернись до Ані"
    owner_alerts = [m for m in sent if m.get("chat_id") == 1000]
    emp_replies = [m for m in sent if m.get("chat_id") == 2001]
    assert len(owner_alerts) == 1
    assert "Іван Сушист" in owner_alerts[0]["text"]
    assert owner_alerts[0]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("own:emp_banquet:")

    assert any("звернись до ані" in m["text"].lower() for m in emp_replies)

    # 6. Повторний вибір тієї ж дати 12.11.2026 -> показується ТІЛЬКИ кнопка "✏️ Змінити години"
    sent.clear()
    await dp.feed_update(bot, _msg_update(12, 2001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(13, 2001, "emp:date:2026-11-12", bot))
    last = [m for m in sent if "reply_markup" in m][-1]
    buttons = [btn.text for row in last["reply_markup"].inline_keyboard for btn in row]
    assert buttons == ["✏️ Змінити години"]

    # 7. Аня натискає кнопку під сповіщенням "Вказати ставку за банкет" і вносить 50 грн/люд
    banquet_btn_cb = owner_alerts[0]["reply_markup"].inline_keyboard[0][0].callback_data
    sent.clear()
    await dp.feed_update(bot, _cb_update(14, 1000, banquet_btn_cb, bot))
    await dp.feed_update(bot, _msg_update(15, 1000, "50", bot))
    # Працівник отримав сповіщення про перерахунок банкету
    assert any("банкетний тариф" in m["text"].lower() for m in sent if m.get("chat_id") == 2001)

    # 8. Аня вносить касу дня за 12.11.2026 = 20000 грн (3% = 600 грн)
    sent.clear()
    await dp.feed_update(bot, _cb_update(16, 1000, "own:rev_date:2026-11-12", bot))
    await dp.feed_update(bot, _msg_update(17, 1000, "20000", bot))
    assert any("600 грн" in m["text"] for m in sent if m.get("chat_id") == 1000)

    # 9. Аня створює нового працівника (Прибиральник) і одразу отримує картку з посиланням знизу і кнопкою поширення
    sent.clear()
    await dp.feed_update(bot, _cb_update(18, 1000, "own:emp_add", bot))
    await dp.feed_update(bot, _msg_update(19, 1000, "Світлана Прибиральниця", bot))
    # Обираємо роль "Прибиральник" з 2-колонкової сітки швидких ролей
    role_kb = sent[-1]["reply_markup"].inline_keyboard
    role_cb = next(
        btn.callback_data
        for row in role_kb
        for btn in row
        if btn.text == "Прибиральник"
    )
    assert role_cb is not None
    await dp.feed_update(bot, _cb_update(20, 1000, role_cb, bot))
    await dp.feed_update(bot, _msg_update(21, 1000, "90", bot))
    await dp.feed_update(bot, _msg_update(22, 1000, "0", bot))
    sent.clear()
    await dp.feed_update(bot, _cb_update(23, 1000, "own:emp_add_banquet_skip", bot))

    card_msg = [m for m in sent if m.get("chat_id") == 1000 and "Світлана Прибиральниця" in m["text"]][-1]
    assert "⏳ Очікує переходу за посиланням" in card_msg["text"]
    assert "<code>https://t.me/" not in card_msg["text"]
    match = re.search(r"https://t\.me/my_cafe_bot\?start=([A-Za-z0-9]+)", card_msg["text"])
    assert match is not None
    sveta_code = match.group(1)
    first_btn = card_msg["reply_markup"].inline_keyboard[0][0]
    assert first_btn.text == "📤 Надіслати запрошення працівнику"
    assert first_btn.url and first_btn.url.startswith("https://t.me/share/url?")

    # Повторне відкриття картки Світлани до підключення також показує активне посилання знизу
    async with session_maker() as s:
        sveta_id = await s.scalar(
            Employee.__table__.select().where(Employee.full_name == "Світлана Прибиральниця")
        )
    sent.clear()
    await dp.feed_update(bot, _cb_update(24, 1000, f"own:emp:{sveta_id}", bot))
    reopened = sent[-1] if "text" in sent[-1] else sent[-2]
    assert f"https://t.me/my_cafe_bot?start={sveta_code}" in reopened["text"]
    assert f"<code>https://t.me/my_cafe_bot?start={sveta_code}</code>" not in reopened["text"]

    # Окреме повідомлення для пересилки теж без <code>, щоб посилання було клікабельним
    sent.clear()
    await dp.feed_update(bot, _cb_update(240, 1000, f"own:emp_link:{sveta_id}", bot))
    fwd_msg = [m for m in sent if m.get("chat_id") == 1000 and "https://t.me/my_cafe_bot?start=" in m.get("text", "")][0]
    assert f"<code>https://t.me/my_cafe_bot?start={sveta_code}</code>" not in fwd_msg["text"]
    assert "Ставка за годину" not in fwd_msg["text"]

    # 9a. Світлана переходить за посиланням -> бот прив'язує її, сповіщає Аню, статус в картці стає 🟢 Підключено
    sent.clear()
    await dp.feed_update(bot, _msg_update(25, 2002, f"/start {sveta_code}", bot, username="sveta"))
    assert any("успішно підключено" in m["text"] for m in sent if m.get("chat_id") == 2002)
    owner_join_alert = [
        m for m in sent if m.get("chat_id") == 1000 and "Світлана Прибиральниця" in m["text"]
    ]
    assert len(owner_join_alert) == 1
    assert "(@sveta)" in owner_join_alert[0]["text"]
    assert "Підключено" in owner_join_alert[0]["text"]
    open_card_cb = owner_join_alert[0]["reply_markup"].inline_keyboard[0][0].callback_data
    assert open_card_cb == f"own:emp:{sveta_id}"

    sent.clear()
    await dp.feed_update(bot, _cb_update(26, 1000, open_card_cb, bot))
    connected_card = [m for m in sent if m.get("chat_id") == 1000 and "Світлана Прибиральниця" in m["text"]][-1]
    assert "🟢 Підключено (@sveta)" in connected_card["text"]
    assert "https://t.me/my_cafe_bot?start=" not in connected_card["text"]
    btn_texts = [b.text for row in connected_card["reply_markup"].inline_keyboard for b in row]
    assert "📤 Надіслати запрошення працівнику" not in btn_texts
    assert "📨 Окреме повідомлення для пересилки" not in btn_texts

    # 10. Аня вивантажує Excel за листопад
    sent.clear()
    await dp.feed_update(bot, _cb_update(27, 1000, "own:rep_xlsx:2026-11-01:2026-11-30", bot))
    docs = [m for m in sent if "document" in m]
    assert len(docs) == 1

    # 11. Аня виплачує Івану за конкретну дату 12.11.2026:
    # 12 год * 110 (1320) + 3% від 20000 (600) + 14*50 (700) = 2620 грн
    async with session_maker() as s:
        ivan_id = await s.scalar(Employee.__table__.select().where(Employee.telegram_id == 2001))
    sent.clear()
    await dp.feed_update(bot, _cb_update(28, 1000, f"own:pay_period:{ivan_id}", bot))
    await dp.feed_update(bot, _msg_update(29, 1000, "12.11.2026", bot))
    assert any("2 620 грн" in m["text"] for m in sent if m.get("chat_id") == 1000)
    await dp.feed_update(bot, _cb_update(30, 1000, "own:pay_confirm_ok", bot))
    assert any("Новий залишок до видачі: <b>0 грн</b>" in m["text"] for m in sent if m.get("chat_id") == 1000)
    assert any("виплату: <b>2 620 грн</b>" in m["text"] for m in sent if m.get("chat_id") == 2001)


@pytest.mark.asyncio
async def test_last_time_quick_button_and_all_owner_pushes(bot_harness) -> None:
    dp, bot, session_maker, sent = bot_harness

    async with session_maker() as s:
        bar_role = await s.scalar(select(Role.id).where(Role.name == "Бармен"))
        assert bar_role is not None
        emp = Employee(
            full_name="Петро Бармен",
            role_id=bar_role,
            telegram_id=3001,
            hourly_rate=100.0,
            revenue_percent=2.0,
            banquet_rate_per_person=50.0,
        )
        s.add(emp)
        await s.commit()
        await s.refresh(emp)
        emp_id = emp.id

    # 1. Перша зміна — без кнопки "Як минулого разу"
    sent.clear()
    await dp.feed_update(bot, _msg_update(101, 3001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(102, 3001, "emp:date:2026-11-10", bot))
    time_prompt = _last_text_msg(sent, 3001)
    btn_cbs = [b.callback_data for row in time_prompt["reply_markup"].inline_keyboard for b in row]
    assert "emp:time_last" not in btn_cbs

    await dp.feed_update(bot, _msg_update(103, 3001, "11:00-23:00", bot))
    await dp.feed_update(bot, _cb_update(104, 3001, "emp:banquet:no", bot))

    # 2. Наступна зміна за ручною датою — кнопка "⚡ Як минулого разу (11:00-23:00)" з'являється!
    sent.clear()
    await dp.feed_update(bot, _msg_update(105, 3001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(106, 3001, "emp:date_manual", bot))
    await dp.feed_update(bot, _msg_update(107, 3001, "11.11.2026", bot))
    time_prompt2 = _last_text_msg(sent, 3001)
    btn_texts = [b.text for row in time_prompt2["reply_markup"].inline_keyboard for b in row]
    assert "⚡ Як минулого разу (11:00-23:00)" in btn_texts

    await dp.feed_update(bot, _cb_update(108, 3001, "emp:time_last", bot))
    await dp.feed_update(bot, _cb_update(109, 3001, "emp:banquet:no", bot))
    assert any("11:00-23:00" in m["text"] for m in sent if m.get("chat_id") == 3001)

    # 3. Пуші працівнику на ВСІ фінансові/часові зміни Ані:
    # 3a. Ставка за годину
    sent.clear()
    await dp.feed_update(bot, _cb_update(110, 1000, f"own:emp_rate:{emp_id}", bot))
    await dp.feed_update(bot, _msg_update(111, 1000, "135", bot))
    assert any("135 грн/год" in m["text"] for m in sent if m.get("chat_id") == 3001)

    # 3b. Відсоток від каси
    sent.clear()
    await dp.feed_update(bot, _cb_update(112, 1000, f"own:emp_pct:{emp_id}", bot))
    await dp.feed_update(bot, _msg_update(113, 1000, "4", bot))
    assert any("4%" in m["text"] for m in sent if m.get("chat_id") == 3001)

    # 3c. Банкетний тариф (навіть якщо ще не було банкетів)
    sent.clear()
    await dp.feed_update(bot, _cb_update(114, 1000, f"own:emp_banquet:{emp_id}", bot))
    await dp.feed_update(bot, _msg_update(115, 1000, "75", bot))
    assert any("75 грн/люд." in m["text"] for m in sent if m.get("chat_id") == 3001)

    # 3d. Зміна годин Анею
    sent.clear()
    await dp.feed_update(bot, _cb_update(116, 1000, f"own:emp_hours:{emp_id}", bot))
    await dp.feed_update(bot, _msg_update(117, 1000, "10.11.2026", bot))
    await dp.feed_update(bot, _msg_update(118, 1000, "10:00-20:00", bot))
    assert any(
        "змінив(ла) твої години" in m["text"] and "10:00-20:00" in m["text"]
        for m in sent
        if m.get("chat_id") == 3001
    )

    # 3e. Премія та штраф
    sent.clear()
    await dp.feed_update(bot, _cb_update(119, 1000, f"own:emp_bonus:{emp_id}", bot))
    await dp.feed_update(bot, _msg_update(120, 1000, "250", bot))
    await dp.feed_update(bot, _msg_update(121, 1000, "Крутий коктейль", bot))
    assert any(
        "премію: <b>+250 грн</b>" in m["text"] and "Крутий коктейль" in m["text"]
        for m in sent
        if m.get("chat_id") == 3001
    )

    sent.clear()
    await dp.feed_update(bot, _cb_update(122, 1000, f"own:emp_fine:{emp_id}", bot))
    await dp.feed_update(bot, _msg_update(123, 1000, "50", bot))
    await dp.feed_update(bot, _msg_update(124, 1000, "Спізнення", bot))
    assert any(
        "штраф: <b>−50 грн</b>" in m["text"] and "Спізнення" in m["text"]
        for m in sent
        if m.get("chat_id") == 3001
    )

    # 3f. Каса дня -> працівник на цій зміні отримує пуш про свій %
    sent.clear()
    await dp.feed_update(bot, _cb_update(125, 1000, "own:rev_date:2026-11-10", bot))
    await dp.feed_update(bot, _msg_update(126, 1000, "10000", bot))
    assert any(
        "вніс(ла) касу" in m["text"] and "+200 грн" in m["text"]
        for m in sent
        if m.get("chat_id") == 3001
    )


@pytest.mark.asyncio
async def test_relink_employee_telegram_flow(bot_harness) -> None:
    dp, bot, session_maker, sent = bot_harness

    async with session_maker() as s:
        role_id = await s.scalar(select(Role.id).where(Role.name == "Кухар"))
        assert role_id is not None
        emp = Employee(
            full_name="Оля Кухар",
            role_id=role_id,
            telegram_id=4001,
            telegram_username="olya_old",
            hourly_rate=110.0,
        )
        s.add(emp)
        await s.commit()
        await s.refresh(emp)
        emp_id = emp.id

    # 1. Аня скидає прив'язку Telegram у картці працівника
    sent.clear()
    await dp.feed_update(bot, _cb_update(201, 1000, f"own:emp:{emp_id}", bot))
    card_msg = _last_text_msg(sent, 1000)
    card_btns = [b.callback_data for row in card_msg["reply_markup"].inline_keyboard for b in row if b.callback_data]
    assert f"own:emp_relink_ask:{emp_id}" in card_btns

    await dp.feed_update(bot, _cb_update(202, 1000, f"own:emp_relink_ask:{emp_id}", bot))
    sent.clear()
    await dp.feed_update(bot, _cb_update(203, 1000, f"own:emp_relink_yes:{emp_id}", bot))

    card_text = _last_text_msg(sent, 1000)["text"]
    match = re.search(r"https://t\.me/my_cafe_bot\?start=([A-Za-z0-9]+)", card_text)
    assert match is not None
    new_code = match.group(1)

    # 2. Старий Telegram втратив доступ одразу
    sent.clear()
    await dp.feed_update(bot, _msg_update(204, 4001, "/start", bot))
    assert "Недостатньо прав" in sent[-1]["text"]

    # 3. Новий Telegram підключається по новому посиланню
    sent.clear()
    await dp.feed_update(bot, _msg_update(205, 4002, f"/start {new_code}", bot, username="olya_new"))
    assert any("успішно підключено" in m["text"] for m in sent if m.get("chat_id") == 4002)
    assert any("(@olya_new)" in m["text"] for m in sent if m.get("chat_id") == 1000)


@pytest.mark.asyncio
async def test_admin_role_permissions_and_notifications(bot_harness) -> None:
    dp, bot, session_maker, sent = bot_harness

    # Підготуємо звичайного працівника зі зміною
    async with session_maker() as s:
        barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        assert barista_id is not None
        worker = Employee(
            full_name="Саша Бариста",
            role_id=barista_id,
            telegram_id=6001,
            hourly_rate=100.0,
            revenue_percent=5.0,
        )
        s.add(worker)
        await s.commit()
        await s.refresh(worker)
        worker_id = worker.id

    # Саша вносить зміну за 12.11.2026
    await dp.feed_update(bot, _msg_update(301, 6001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(302, 6001, "emp:date:2026-11-12", bot))
    await dp.feed_update(bot, _msg_update(303, 6001, "10:00-18:00", bot))
    await dp.feed_update(bot, _cb_update(304, 6001, "emp:banquet:no", bot))

    # 1. Аня створює співробітника з роллю "Адмін"
    sent.clear()
    await dp.feed_update(bot, _cb_update(305, 1000, "own:emp_add", bot))
    await dp.feed_update(bot, _msg_update(306, 1000, "Аліна Адмін", bot))
    role_kb = sent[-1]["reply_markup"].inline_keyboard
    admin_role_cb = next(b.callback_data for row in role_kb for b in row if b.text == "Адмін")
    assert admin_role_cb is not None
    await dp.feed_update(bot, _cb_update(307, 1000, admin_role_cb, bot))
    await dp.feed_update(bot, _msg_update(308, 1000, "140", bot))
    await dp.feed_update(bot, _msg_update(309, 1000, "0", bot))
    sent.clear()
    await dp.feed_update(bot, _cb_update(310, 1000, "own:emp_add_banquet_skip", bot))

    card_msg = [m for m in sent if m.get("chat_id") == 1000 and "Аліна Адмін" in m["text"]][-1]
    match = re.search(r"https://t\.me/my_cafe_bot\?start=([A-Za-z0-9]+)", card_msg["text"])
    assert match is not None
    adm_code = match.group(1)

    # Аліна підключається як Адмін (id=5001)
    sent.clear()
    await dp.feed_update(bot, _msg_update(311, 5001, f"/start {adm_code}", bot, username="alina_adm"))
    adm_join = [m for m in sent if m.get("chat_id") == 5001][-1]
    menu_texts = [b.text for row in adm_join["reply_markup"].keyboard for b in row]
    assert BTN_EMP_SHIFT in menu_texts
    assert BTN_EMP_MY_SALARY in menu_texts
    assert BTN_OWN_STAFF in menu_texts
    assert BTN_OWN_REVENUE in menu_texts
    # За замовчуванням звіти, виплати і налаштування прав адміна приховані
    assert BTN_OWN_REPORTS not in menu_texts
    assert BTN_OWN_PAYOUTS not in menu_texts
    assert BTN_OWN_ADMIN_PERMS not in menu_texts

    # 2. За замовчуванням Аліна може дивитись зміни працівника БЕЗ грошей, і НЕ може відкривати заборонені дії
    sent.clear()
    await dp.feed_update(bot, _cb_update(312, 5001, f"own:emp:{worker_id}", bot))
    worker_card_for_adm = _last_text_msg(sent, 5001)
    assert "Ставка за годину" not in worker_card_for_adm["text"]
    assert "До видачі" not in worker_card_for_adm["text"]

    await dp.feed_update(bot, _cb_update(313, 5001, f"own:emp_shifts:{worker_id}:0", bot))
    shifts_text = _last_text_msg(sent, 5001)["text"]
    assert "10:00-18:00" in shifts_text
    assert "800 грн" not in shifts_text

    # Заборонені дії для дефолтного Адміна -> "Недостатньо прав"
    for idx, blocked_cb in enumerate(
        [
            f"own:emp_summary:{worker_id}:month",
            f"own:emp_bonus:{worker_id}",
            f"own:emp_rate:{worker_id}",
            f"own:emp_hours:{worker_id}",
        ],
        start=314,
    ):
        await dp.feed_update(bot, _cb_update(idx, 5001, blocked_cb, bot))
        assert "Недостатньо прав" in sent[-1]["callback_answer"]

    await dp.feed_update(bot, _msg_update(320, 5001, BTN_OWN_ADMIN_PERMS, bot))
    assert "Недостатньо прав" in sent[-1]["text"]

    # 3. Аліна вносить касу дня = 10000 -> дозволено, Аня отримує сповіщення, Саша отримує свій % (+500), а Аліна НЕ бачить суми зарплат інших
    sent.clear()
    await dp.feed_update(bot, _cb_update(321, 5001, "own:rev_date:2026-11-12", bot))
    await dp.feed_update(bot, _msg_update(322, 5001, "10000", bot))
    adm_rev_replies = [m["text"] for m in sent if m.get("chat_id") == 5001]
    assert any("10 000 грн" in t for t in adm_rev_replies)
    assert all("Саша Бариста" not in t for t in adm_rev_replies)
    assert any("+500 грн" in m["text"] and "Адмін Аліна Адмін" in m["text"] for m in sent if m.get("chat_id") == 6001)
    assert any("Адмін Аліна Адмін" in m["text"] and "10 000 грн" in m["text"] for m in sent if m.get("chat_id") == 1000)

    # 4. Аня вмикає для Адміна право редагування годин (⚙️ Права адміна) -> Аліні прилітає оновлене меню і доступ відкривається!
    sent.clear()
    await dp.feed_update(bot, _msg_update(323, 1000, BTN_OWN_ADMIN_PERMS, bot))
    await dp.feed_update(bot, _cb_update(324, 1000, "own:adm_toggle:can_edit_hours", bot))
    assert any("оновила права доступу" in m["text"] for m in sent if m.get("chat_id") == 5001)

    sent.clear()
    await dp.feed_update(bot, _cb_update(325, 5001, f"own:emp_hours:{worker_id}", bot))
    await dp.feed_update(bot, _msg_update(326, 5001, "12.11.2026", bot))
    await dp.feed_update(bot, _msg_update(327, 5001, "09:00-19:00", bot))
    assert any("Адмін Аліна Адмін" in m["text"] and "09:00-19:00" in m["text"] for m in sent if m.get("chat_id") == 6001)
    assert any("Адмін Аліна Адмін" in m["text"] and "09:00-19:00" in m["text"] for m in sent if m.get("chat_id") == 1000)

    # 5. Навіть якщо Аня увімкне can_manage_staff, Адмін не може призначати іншого Адміна або видаляти Адміна
    await dp.feed_update(bot, _cb_update(328, 1000, "own:adm_toggle:can_manage_staff", bot))
    async with session_maker() as s:
        adm_role_id = await s.scalar(select(Role.id).where(Role.name == "Адмін"))
        alina_id = await s.scalar(select(Employee.id).where(Employee.telegram_id == 5001))
    sent.clear()
    await dp.feed_update(bot, _cb_update(329, 5001, f"own:emp_set_role:{worker_id}:{adm_role_id}", bot))
    assert "Тільки Аня" in sent[-1]["callback_answer"]
    await dp.feed_update(bot, _cb_update(330, 5001, f"own:emp_del_yes:{alina_id}", bot))
    assert "Недостатньо прав" in sent[-1]["callback_answer"]


@pytest.mark.asyncio
async def test_employee_card_and_payout_request_chat_flow(bot_harness) -> None:
    dp, bot, session_maker, sent = bot_harness

    # 1. Створюємо працівника Катю (id=7001) і фіксуємо їй 1 зміну на 1200 грн
    await dp.feed_update(bot, _cb_update(401, 1000, "own:emp_add", bot))
    await dp.feed_update(bot, _msg_update(402, 1000, "Катя Бармен", bot))
    role_kb = sent[-1]["reply_markup"].inline_keyboard
    barmen_cb = next(b.callback_data for row in role_kb for b in row if b.text == "Бармен")
    await dp.feed_update(bot, _cb_update(403, 1000, barmen_cb, bot))
    await dp.feed_update(bot, _msg_update(404, 1000, "100", bot))
    await dp.feed_update(bot, _msg_update(405, 1000, "0", bot))
    sent.clear()
    await dp.feed_update(bot, _cb_update(406, 1000, "own:emp_add_banquet_skip", bot))

    card_msg = [m for m in sent if m.get("chat_id") == 1000 and "Катя Бармен" in m["text"]][-1]
    code = re.search(r"https://t\.me/my_cafe_bot\?start=([A-Za-z0-9]+)", card_msg["text"]).group(1)

    await dp.feed_update(bot, _msg_update(407, 7001, f"/start {code}", bot, username="katya_b"))
    await dp.feed_update(bot, _msg_update(408, 7001, BTN_EMP_SHIFT, bot))
    today_cb = sent[-1]["reply_markup"].inline_keyboard[0][0].callback_data
    await dp.feed_update(bot, _cb_update(409, 7001, today_cb, bot))
    await dp.feed_update(bot, _msg_update(410, 7001, "10:00-22:00", bot))
    await dp.feed_update(bot, _cb_update(411, 7001, "emp:banquet:no", bot))

    # 2. Катя відкриває "💳 Картка / Запит на виплату", зберігає картку та надсилає запит на весь залишок (1200 грн)
    sent.clear()
    await dp.feed_update(bot, _msg_update(412, 7001, BTN_EMP_PAYOUT_REQ, bot))
    await dp.feed_update(bot, _cb_update(413, 7001, "emp:card_edit", bot))
    await dp.feed_update(bot, _msg_update(414, 7001, "5168 7420 1111 2222", bot))
    assert "5168 7420 1111 2222" in _last_text_msg(sent, 7001)["text"]

    sent.clear()
    await dp.feed_update(bot, _cb_update(415, 7001, "emp:req_new", bot))
    await dp.feed_update(bot, _cb_update(416, 7001, "emp:req_amt_full", bot))
    await dp.feed_update(bot, _msg_update(417, 7001, "за сьогодні", bot))

    # Аня отримує пуш про новий запит БЕЗ повного номера картки, але з кнопкою Прийняти
    owner_req_msg = [m for m in sent if m.get("chat_id") == 1000 and "Новий запит на виплату" in m["text"]][-1]
    assert "5168742011112222" not in owner_req_msg["text"]
    accept_cb = next(
        b.callback_data
        for row in owner_req_msg["reply_markup"].inline_keyboard
        for b in row
        if b.callback_data and b.callback_data.startswith("own:req_accept:")
    )
    req_id = int(accept_cb.split(":")[2])

    # 3. Аня натискає "✅ Прийняти" -> їй одразу показується картка + CopyTextButton, а Катя отримує сповіщення
    sent.clear()
    await dp.feed_update(bot, _cb_update(418, 1000, accept_cb, bot))
    owner_approved = _last_text_msg(sent, 1000)
    assert "5168742011112222" in owner_approved["text"]
    copy_btns = [
        b
        for row in owner_approved["reply_markup"].inline_keyboard
        for b in row
        if getattr(b, "copy_text", None) is not None
    ]
    assert len(copy_btns) == 1
    assert copy_btns[0].copy_text.text == "5168742011112222"
    assert any("прийняла твій запит" in m["text"].lower() for m in sent if m.get("chat_id") == 7001)

    # 4. Аня натискає "💸 Виплачено" -> запит закривається, баланс Каті стає 0 грн
    pay_cb = next(
        b.callback_data
        for row in owner_approved["reply_markup"].inline_keyboard
        for b in row
        if b.callback_data and b.callback_data.startswith("own:req_pay:")
    )
    sent.clear()
    await dp.feed_update(bot, _cb_update(419, 1000, pay_cb, bot))
    assert "0 грн" in _last_text_msg(sent, 1000)["text"]
    assert any("0 грн" in m["text"] and "1 200" in m["text"] for m in sent if m.get("chat_id") == 7001)

    async with session_maker() as s:
        req_row = await s.get(PayoutRequest, req_id)
        assert req_row is not None and req_row.status == "paid" and req_row.payout_id is not None


@pytest.mark.asyncio
async def test_navigation_commands_menu_button_and_back_buttons(bot_harness) -> None:
    dp, bot, session_maker, sent = bot_harness

    async with session_maker() as s:
        barista_id = await s.scalar(select(Role.id).where(Role.name == "Бариста"))
        assert barista_id is not None
        s.add(
            Employee(
                full_name="Денис Бариста",
                role_id=barista_id,
                telegram_id=8001,
                hourly_rate=110.0,
                revenue_percent=0.0,
            )
        )
        await s.commit()

    # 1. MenuButtonCommands відновлює стандартну кнопку команд (без липкого WebApp)
    sent.clear()
    await restore_telegram_menu_button(bot)
    assert any(
        isinstance(m.get("set_chat_menu_button"), MenuButtonCommands)
        for m in sent
    )

    # 2. /start не дублює Mini App-картку, а /app надсилає її за вимогою
    prev_url = miniapp_runtime.url
    miniapp_runtime.url = "https://example-cafe.lhr.life"
    try:
        sent.clear()
        await dp.feed_update(bot, _msg_update(501, 1000, "/start", bot, username="anya"))
        assert len([m for m in sent if m.get("chat_id") == 1000]) == 1
        assert not any("Доступний візуальний Mini App" in m.get("text", "") for m in sent)

        sent.clear()
        await dp.feed_update(bot, _msg_update(502, 1000, "/app", bot, username="anya"))
        app_msg = _last_text_msg(sent, 1000)
        assert app_msg["reply_markup"].inline_keyboard[0][0].web_app.url == "https://example-cafe.lhr.life"
    finally:
        miniapp_runtime.url = prev_url

    # 3. /menu скидає стан та повертає головне меню
    sent.clear()
    await dp.feed_update(bot, _msg_update(503, 8001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(504, 8001, "emp:date_manual", bot))
    await dp.feed_update(bot, _msg_update(505, 8001, "/menu", bot))
    menu_msg = _last_text_msg(sent, 8001)
    assert "Головне меню" in menu_msg["text"]

    # 4. fsm:cancel містить кнопку "🏠 Головне меню" (nav:home), а nav:home повертає клавіатуру меню
    sent.clear()
    await dp.feed_update(bot, _cb_update(506, 8001, "emp:date_manual", bot))
    await dp.feed_update(bot, _cb_update(507, 8001, "fsm:cancel", bot))
    cancel_msg = _last_text_msg(sent, 8001)
    assert cancel_msg["reply_markup"].inline_keyboard[0][0].callback_data == "nav:home"

    sent.clear()
    await dp.feed_update(bot, _cb_update(508, 8001, "nav:home", bot))
    home_msg = _last_text_msg(sent, 8001)
    assert "Головне меню" in home_msg["text"]
    assert home_msg["reply_markup"] is not None

    # 5. Послідовна навігація "⬅️ Назад" у сценарії зміни працівника
    await dp.feed_update(bot, _msg_update(509, 8001, BTN_EMP_SHIFT, bot))
    await dp.feed_update(bot, _cb_update(510, 8001, "emp:date:2026-11-15", bot))
    await dp.feed_update(bot, _msg_update(511, 8001, "10:00-20:00", bot))
    await dp.feed_update(bot, _cb_update(512, 8001, "emp:banquet:yes", bot))
    guests_msg = _last_text_msg(sent, 8001)
    guests_cbs = [b.callback_data for row in guests_msg["reply_markup"].inline_keyboard for b in row]
    assert "emp:banquet_back" in guests_cbs

    sent.clear()
    await dp.feed_update(bot, _cb_update(513, 8001, "emp:banquet_back", bot))
    banquet_msg = _last_text_msg(sent, 8001)
    assert "Чи був у цей день банкет?" in banquet_msg["text"]
    banquet_cbs = [b.callback_data for row in banquet_msg["reply_markup"].inline_keyboard for b in row]
    assert "emp:time_back" in banquet_cbs

    sent.clear()
    await dp.feed_update(bot, _cb_update(514, 8001, "emp:time_back", bot))
    time_msg = _last_text_msg(sent, 8001)
    assert "15.11.2026" in time_msg["text"]
    time_cbs = [b.callback_data for row in time_msg["reply_markup"].inline_keyboard for b in row]
    assert "emp:shift_menu" in time_cbs

    sent.clear()
    await dp.feed_update(bot, _cb_update(515, 8001, "emp:shift_menu", bot))
    dates_msg = _last_text_msg(sent, 8001)
    assert "Обери дату зміни" in dates_msg["text"]
    dates_cbs = [b.callback_data for row in dates_msg["reply_markup"].inline_keyboard for b in row]
    assert "nav:home" in dates_cbs
