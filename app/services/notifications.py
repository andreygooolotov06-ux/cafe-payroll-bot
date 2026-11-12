from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from aiogram import Bot
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Employee, PayoutRequest, Role, Shift
from app.keyboards.inline import HOME_BTN, owner_notify_banquet_kb
from app.keyboards.reply import admin_main_menu, employee_main_menu
from app.services.access import (
    ActorContext,
    AdminPerms,
    get_all_owner_ids,
    get_connected_admin_employees,
)
from app.services.payout_requests import format_card_display, mask_card
from app.services.payroll import q2_float, to_dec
from app.utils.datetime_utils import format_date_short, format_date_with_day
from app.utils.formatting import esc, fmt_hours, fmt_money, fmt_percent

logger = logging.getLogger(__name__)


async def notify_employee_message(
    bot: Bot,
    emp: Employee,
    text: str,
    reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
) -> None:
    if not emp.telegram_id:
        return
    try:
        await bot.send_message(emp.telegram_id, text, reply_markup=reply_markup)
    except Exception as exc:
        logger.warning("Не вдалося надіслати сповіщення працівнику %s: %s", emp.id, exc)


async def notify_owners_admin_action(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    text: str,
) -> None:
    if not actor.is_admin or actor.employee is None:
        return
    owner_ids = await get_all_owner_ids(session, settings)
    for owner_id in owner_ids:
        try:
            await bot.send_message(
                owner_id,
                f"🔔 <b>Адмін {esc(actor.employee.full_name)}:</b> {text}",
            )
        except Exception as exc:
            logger.warning("Не вдалося надіслати сповіщення власниці %s: %s", owner_id, exc)


async def notify_owners_employee_connected(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    linked_emp: Employee,
) -> None:
    role_name = linked_emp.role.name if linked_emp.role else "працівник"
    uname_str = f" (@{esc(linked_emp.telegram_username)})" if linked_emp.telegram_username else ""
    alert_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Відкрити картку", callback_data=f"own:emp:{linked_emp.id}")]
        ]
    )
    owner_ids = await get_all_owner_ids(session, settings)
    for owner_id in owner_ids:
        try:
            await bot.send_message(
                owner_id,
                f"🟢 <b>{esc(linked_emp.full_name)}</b> ({esc(role_name)}){uname_str} "
                "перейшов за посиланням та успішно підключився до бота! ✨\n"
                "Статус змінено з ⏳ очікування на 🟢 <b>Підключено</b>.",
                reply_markup=alert_kb,
            )
        except Exception as exc:
            logger.warning("Не вдалося надіслати сповіщення власниці %s: %s", owner_id, exc)


async def notify_owners_missing_banquet_rate(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    employee: Employee,
    work_date: date,
    guests: int | None,
) -> None:
    role_name = employee.role.name if employee.role else "працівник"
    guests_text = f"{guests} люд." if guests is not None else "кількість людей ще не вказано"
    text = (
        "Привіт, Аню! 🎀\n"
        f"Працівник <b>{esc(employee.full_name)}</b> ({esc(role_name)}) позначив банкет за "
        f"<b>{format_date_with_day(work_date)}</b> ({guests_text}), але в його картці ще не вказано тариф за людину.\n"
        "Заповнимо зараз, щоб усе порахувалось?"
    )
    kb = owner_notify_banquet_kb(employee.id)
    owner_ids = await get_all_owner_ids(session, settings)
    for owner_id in owner_ids:
        try:
            await bot.send_message(owner_id, text, reply_markup=kb)
        except Exception as exc:
            logger.warning("Не вдалося надіслати сповіщення власниці %s: %s", owner_id, exc)


async def notify_revenue_saved(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    work_date: date,
    amount: float,
    shifts_that_day: list[Shift],
) -> list[dict[str, object]]:
    pct_items: list[dict[str, object]] = []
    amt_d = to_dec(amount)
    for s in shifts_that_day:
        if s.percent_snapshot > 0:
            bonus = q2_float((amt_d * to_dec(s.percent_snapshot)) / Decimal("100"))
            pct_items.append({
                "employee_id": s.employee_id,
                "full_name": s.employee.full_name if s.employee else "—",
                "percent": s.percent_snapshot,
                "bonus": bonus,
            })
            if s.employee and s.employee.telegram_id and s.employee.is_active:
                await notify_employee_message(
                    bot,
                    s.employee,
                    f"📈 <b>{esc(actor.actor_label)}</b> вніс(ла) касу за "
                    f"<b>{format_date_with_day(work_date)}</b>: <b>{fmt_money(amount)}</b>.\n"
                    f"Твій відсоток ({fmt_percent(s.percent_snapshot)}): <b>+{fmt_money(bonus)}</b> ✨",
                )

    await notify_owners_admin_action(
        session,
        bot,
        settings,
        actor,
        f"вніс(ла) касу за {format_date_with_day(work_date)}: <b>{fmt_money(amount)}</b>",
    )
    return pct_items


async def notify_rate_changed(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    emp: Employee,
    kind: str,
    new_val: float,
    updated_dates: list[date] | None = None,
) -> None:
    if kind == "hourly":
        await notify_employee_message(
            bot,
            emp,
            f"💵 <b>{esc(actor.actor_label)}</b> змінив(ла) твою ставку за годину: "
            f"тепер <b>{fmt_money(new_val)}/год</b> ✨\n"
            "<i>(Для вже відпрацьованих змін збереглася попередня ставка.)</i>",
        )
        await notify_owners_admin_action(
            session,
            bot,
            settings,
            actor,
            f"змінив(ла) ставку працівнику <b>{esc(emp.full_name)}</b> → <b>{fmt_money(new_val)}/год</b>",
        )
    elif kind == "percent":
        await notify_employee_message(
            bot,
            emp,
            f"📈 <b>{esc(actor.actor_label)}</b> змінив(ла) твій відсоток від каси: "
            f"тепер <b>{fmt_percent(new_val)}</b> ✨",
        )
        await notify_owners_admin_action(
            session,
            bot,
            settings,
            actor,
            f"змінив(ла) відсоток від каси працівнику <b>{esc(emp.full_name)}</b> → <b>{fmt_percent(new_val)}</b>",
        )
    elif kind == "banquet":
        if updated_dates:
            dates_str = ", ".join(format_date_short(d) for d in updated_dates)
            emp_msg = (
                f"🎉 <b>{esc(actor.actor_label)}</b> додав(ла) твій банкетний тариф "
                f"(<b>{fmt_money(new_val)}/люд.</b>) — банкети за {dates_str} вже пораховано ✨"
            )
        else:
            emp_msg = (
                f"🎉 <b>{esc(actor.actor_label)}</b> оновив(ла) твій банкетний тариф: "
                f"<b>{fmt_money(new_val)}/люд.</b> ✨"
            )
        await notify_employee_message(bot, emp, emp_msg)
        await notify_owners_admin_action(
            session,
            bot,
            settings,
            actor,
            f"оновив(ла) банкетний тариф працівнику <b>{esc(emp.full_name)}</b> → <b>{fmt_money(new_val)}/люд.</b>",
        )


async def notify_role_changed(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    emp: Employee,
    role: Role,
    was_admin: bool,
    is_now_admin: bool,
    perms: AdminPerms,
) -> None:
    if emp.telegram_id:
        if is_now_admin and not was_admin:
            await notify_employee_message(
                bot,
                emp,
                f"👑 <b>{esc(actor.actor_label)}</b> призначив(ла) тобі посаду <b>{esc(role.name)}</b>!\n"
                "Інструменти адміністратора вже відкрито в меню внизу ✨",
                reply_markup=admin_main_menu(perms),
            )
        elif was_admin and not is_now_admin:
            await notify_employee_message(
                bot,
                emp,
                f"🏷 <b>{esc(actor.actor_label)}</b> змінив(ла) твою посаду на <b>{esc(role.name)}</b>.\n"
                "Меню внизу оновлено ✨",
                reply_markup=employee_main_menu(),
            )
        else:
            await notify_employee_message(
                bot,
                emp,
                f"🏷 <b>{esc(actor.actor_label)}</b> змінив(ла) твою посаду на <b>{esc(role.name)}</b> ✨",
            )

    await notify_owners_admin_action(
        session,
        bot,
        settings,
        actor,
        f"змінив(ла) посаду працівнику <b>{esc(emp.full_name)}</b> → <b>{esc(role.name)}</b>",
    )


async def notify_shift_edited_by_manager(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    emp: Employee,
    work_date: date,
    start_time: str,
    end_time: str,
    hours: float,
    created: bool,
    old_range: str | None = None,
) -> None:
    if created:
        action_word = "додано"
        emp_msg = (
            f"🕒 <b>{esc(actor.actor_label)}</b> додав(ла) тобі зміну за "
            f"<b>{format_date_with_day(work_date)}</b>: "
            f"<b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ✨"
        )
    else:
        action_word = "оновлено"
        emp_msg = (
            f"🕒 <b>{esc(actor.actor_label)}</b> змінив(ла) твої години за "
            f"<b>{format_date_with_day(work_date)}</b>:\n"
            f"Було: <code>{old_range}</code> → Стало: <b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ✨"
        )
    await notify_employee_message(bot, emp, emp_msg)
    await notify_owners_admin_action(
        session,
        bot,
        settings,
        actor,
        f"{action_word} години працівнику <b>{esc(emp.full_name)}</b> за "
        f"{format_date_with_day(work_date)}: <b>{start_time}-{end_time}</b> ({fmt_hours(hours)})",
    )


async def notify_adjustment_created(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    emp: Employee,
    kind: str,
    amount: float,
    comment: str | None,
    balance_due: float,
) -> None:
    if kind == "bonus":
        comment_str = f"\nКоментар: <i>{esc(comment)}</i>" if comment else ""
        emp_msg = (
            f"🎁 <b>{esc(actor.actor_label)}</b> нарахував(ла) тобі премію: "
            f"<b>+{fmt_money(amount)}</b>!{comment_str}\n"
            f"👛 Поточний залишок до видачі: <b>{fmt_money(balance_due)}</b> 🌸"
        )
        adm_word = f"додав(ла) премію <b>+{fmt_money(amount)}</b>"
    else:
        comment_str = f"\nПричина: <i>{esc(comment)}</i>" if comment else ""
        emp_msg = (
            f"📌 <b>{esc(actor.actor_label)}</b> зафіксував(ла) штраф: "
            f"<b>−{fmt_money(amount)}</b>.{comment_str}\n"
            f"👛 Поточний залишок до видачі: <b>{fmt_money(balance_due)}</b>"
        )
        adm_word = f"зафіксував(ла) штраф <b>−{fmt_money(amount)}</b>"

    await notify_employee_message(bot, emp, emp_msg)
    await notify_owners_admin_action(
        session,
        bot,
        settings,
        actor,
        f"{adm_word} працівнику <b>{esc(emp.full_name)}</b>"
        + (f" ({esc(comment)})" if comment else ""),
    )


async def notify_payout_created(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    emp: Employee,
    amount: float,
    comment: str | None,
    balance_due: float,
) -> None:
    if emp.telegram_id:
        comment_str = f" ({esc(comment)})" if comment else ""
        await notify_employee_message(
            bot,
            emp,
            f"💸 <b>{esc(actor.actor_label)}</b> зафіксував(ла) тобі виплату: "
            f"<b>{fmt_money(amount)}</b>{comment_str}\n"
            f"Твій поточний залишок до видачі: <b>{fmt_money(balance_due)}</b> 🌸",
        )
    comment_note = f" ({esc(comment)})" if comment else ""
    await notify_owners_admin_action(
        session,
        bot,
        settings,
        actor,
        f"оформив(ла) виплату працівнику <b>{esc(emp.full_name)}</b>: "
        f"<b>{fmt_money(amount)}</b>{comment_note}",
    )


async def notify_admin_perms_changed(
    session: AsyncSession,
    bot: Bot,
    perms: AdminPerms,
) -> None:
    admins = await get_connected_admin_employees(session)
    for adm in admins:
        if adm.telegram_id:
            await notify_employee_message(
                bot,
                adm,
                "🔔 <b>Аня оновила права доступу для ролі Адмін</b> — меню внизу оновлено ✨",
                reply_markup=admin_main_menu(perms),
            )


def owner_payout_request_pending_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Прийняти і показати картку",
                    callback_data=f"own:req_accept:{req_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Відхилити",
                    callback_data=f"own:req_reject:{req_id}",
                )
            ],
            [
                InlineKeyboardButton(text="💸 Всі виплати", callback_data="own:pay_menu"),
                HOME_BTN,
            ],
        ]
    )


def owner_payout_request_approved_kb(
    req_id: int,
    clean_card: str | None,
    amount: float,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if clean_card:
        rows.append([
            InlineKeyboardButton(
                text=f"📋 Скопіювати картку ({mask_card(clean_card)})",
                copy_text=CopyTextButton(text=clean_card),
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text=f"💸 Позначити виплаченим ({fmt_money(amount)})",
            callback_data=f"own:req_pay:{req_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="❌ Відхилити запит",
            callback_data=f"own:req_reject:{req_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(text="💸 Всі виплати", callback_data="own:pay_menu"),
        HOME_BTN,
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_owners_payout_request_created(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    emp: Employee,
    req: PayoutRequest,
    current_balance_due: float,
) -> None:
    role_name = emp.role.name if emp.role else "працівник"
    comment_line = f"\n💬 Коментар: <i>{esc(req.comment)}</i>" if req.comment else ""
    text = (
        "💸 <b>Новий запит на виплату!</b>\n\n"
        f"👤 Працівник: <b>{esc(emp.full_name)}</b> ({esc(role_name)})\n"
        f"💰 Сума запиту: <b>{fmt_money(req.amount)}</b>\n"
        f"👛 Поточний залишок: <b>{fmt_money(current_balance_due)}</b>{comment_line}\n"
        "💳 Картка прикріплена — натисни <b>«Прийняти»</b>, щоб отримати номер для переказу ✨"
    )
    kb = owner_payout_request_pending_kb(req.id)
    owner_ids = await get_all_owner_ids(session, settings)
    for owner_id in owner_ids:
        try:
            await bot.send_message(owner_id, text, reply_markup=kb)
        except Exception as exc:
            logger.warning("Не вдалося надіслати запит на виплату власниці %s: %s", owner_id, exc)


async def notify_employee_payout_request_accepted(
    bot: Bot,
    emp: Employee,
    req: PayoutRequest,
) -> None:
    card = format_card_display(req.card_snapshot or emp.card_number) or "твою картку"
    await notify_employee_message(
        bot,
        emp,
        f"✅ <b>Аня прийняла твій запит на виплату {fmt_money(req.amount)}!</b>\n"
        f"Незабаром очікуй переказ на картку <code>{esc(card)}</code> 💳",
    )


async def notify_employee_payout_request_rejected(
    bot: Bot,
    emp: Employee,
    req: PayoutRequest,
) -> None:
    note = f"\nПричина: <i>{esc(req.review_note)}</i>" if req.review_note else ""
    await notify_employee_message(
        bot,
        emp,
        f"❌ <b>Запит на виплату {fmt_money(req.amount)} відхилено.</b>{note}\n"
        "За потреби уточни деталі в Ані або подай новий запит.",
    )
