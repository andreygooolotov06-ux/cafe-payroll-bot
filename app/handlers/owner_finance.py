from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Adjustment, DailyRevenue, Employee, Payout, PayoutRequest, Shift
from app.filters import IsOwnerOrAdminFilter
from app.handlers.helpers import parse_positive_float, safe_edit_text
from app.keyboards.inline import (
    HOME_BTN,
    cancel_kb,
    owner_pay_confirm_kb,
    owner_payout_employee_kb,
    owner_payouts_menu_kb,
    owner_reports_kb,
    owner_revenue_menu_kb,
)
from app.keyboards.reply import (
    BTN_OWN_PAYOUTS,
    BTN_OWN_REPORTS,
    BTN_OWN_REVENUE,
    admin_main_menu,
)
from app.services.access import ActorContext, get_all_owner_ids
from app.services.excel_export import build_payroll_excel
from app.services.notifications import (
    notify_employee_payout_request_accepted,
    notify_employee_payout_request_rejected,
    notify_payout_created,
    owner_payout_request_approved_kb,
    owner_payout_request_pending_kb,
)
from app.services.payout_requests import (
    accept_payout_request,
    format_card_display,
    mark_payout_request_paid,
    reject_payout_request,
)
from app.services.payroll import (
    EmployeePeriodSummary,
    get_all_employees_summary,
    get_employee_summary,
    get_recent_revenues,
    q2_float,
    to_dec,
)
from app.states import OwnerFinanceStates
from app.utils.datetime_utils import (
    format_date_short,
    format_date_with_day,
    get_period_bounds,
    parse_date_input,
    parse_date_range_input,
    today_local,
)
from app.utils.formatting import esc, fmt_hours, fmt_money, fmt_percent

router = Router(name="owner_finance")
router.message.filter(IsOwnerOrAdminFilter())
router.callback_query.filter(IsOwnerOrAdminFilter())


async def _deny_cb(callback: CallbackQuery) -> None:
    await callback.answer("Недостатньо прав 🔒", show_alert=True)


async def _deny_msg(message: Message, actor: ActorContext) -> None:
    if actor.is_admin:
        await message.answer("Недостатньо прав 🔒", reply_markup=admin_main_menu(actor.admin_perms))
    else:
        await message.answer("Недостатньо прав 🔒")


async def _notify_owners_about_admin(
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
            await bot.send_message(owner_id, f"🔔 <b>Адмін {esc(actor.employee.full_name)}:</b> {text}")
        except Exception:
            pass


# ----------------- 1. КАСА ДНЯ -----------------

async def _show_revenue_menu(
    target_msg: Message,
    session: AsyncSession,
    today: date,
    edit_in_place: bool,
) -> None:
    recent_dates = [today - timedelta(days=i) for i in range(5)]
    rev_map = await get_recent_revenues(session, recent_dates)
    text = (
        "💵 <b>Каса дня</b>\n"
        "Обери день нижче або введи будь-яку дату у форматі <b>ДД.ММ.РРРР</b>, "
        "щоб внести чи змінити загальну виручку (від неї рахується % персоналу):"
    )
    kb = owner_revenue_menu_kb(recent_dates, rev_map)
    if edit_in_place:
        await safe_edit_text(target_msg, text, reply_markup=kb)
    else:
        await target_msg.answer(text, reply_markup=kb)


@router.message(StateFilter("*"), F.text == BTN_OWN_REVENUE)
async def menu_revenue(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_enter_revenue"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    await state.clear()
    await _show_revenue_menu(message, session, today_local(settings.tz), edit_in_place=False)


@router.callback_query(StateFilter("*"), F.data == "own:rev_menu")
async def cb_revenue_menu(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_enter_revenue"):
        await _deny_cb(callback)
        return
    await state.clear()
    if callback.message:
        await _show_revenue_menu(callback.message, session, today_local(settings.tz), edit_in_place=True)
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data == "own:rev_manual")
async def cb_revenue_manual_date(
    callback: CallbackQuery,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_enter_revenue"):
        await _deny_cb(callback)
        return
    await state.clear()
    await state.set_state(OwnerFinanceStates.waiting_revenue_manual_date)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Введи дату для внесення каси у форматі <b>ДД.ММ.РРРР</b> (наприклад, <code>12.11.2026</code>):",
            reply_markup=cancel_kb("own:rev_menu"),
        )
    await callback.answer()


@router.message(OwnerFinanceStates.waiting_revenue_manual_date)
async def msg_revenue_manual_date(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_enter_revenue"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        work_date = parse_date_input(message.text or "")
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=cancel_kb("own:rev_menu"))
        return

    existing = await session.scalar(select(DailyRevenue).where(DailyRevenue.work_date == work_date))
    await state.update_data(rev_date=work_date.isoformat())
    await state.set_state(OwnerFinanceStates.waiting_revenue_amount)
    cur_txt = fmt_money(existing.amount) if existing else "ще не внесена"
    await message.answer(
        f"Каса за <b>{format_date_with_day(work_date, full=True)}</b> (зараз: <b>{cur_txt}</b>).\n"
        "Введи суму загальної виручки за день (у грн):",
        reply_markup=cancel_kb("own:rev_menu"),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("own:rev_date:"))
async def cb_revenue_date_pick(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_enter_revenue"):
        await _deny_cb(callback)
        return
    await state.clear()
    work_date = date.fromisoformat((callback.data or "").split(":")[2])
    existing = await session.scalar(select(DailyRevenue).where(DailyRevenue.work_date == work_date))
    await state.update_data(rev_date=work_date.isoformat())
    await state.set_state(OwnerFinanceStates.waiting_revenue_amount)
    cur_txt = fmt_money(existing.amount) if existing else "ще не внесена"
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Каса за <b>{format_date_with_day(work_date, full=True)}</b> (зараз: <b>{cur_txt}</b>).\n"
            "Введи суму загальної виручки за день (у грн):",
            reply_markup=cancel_kb("own:rev_menu"),
        )
    await callback.answer()


@router.message(OwnerFinanceStates.waiting_revenue_amount)
async def msg_revenue_amount(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_enter_revenue"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        amount = parse_positive_float(message.text or "", allow_zero=True)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=cancel_kb("own:rev_menu"))
        return

    data = await state.get_data()
    work_date = date.fromisoformat(data["rev_date"])
    await state.clear()

    existing = await session.scalar(select(DailyRevenue).where(DailyRevenue.work_date == work_date))
    if existing is None:
        session.add(DailyRevenue(work_date=work_date, amount=amount))
    else:
        existing.amount = amount
    await session.commit()

    shifts_that_day = (
        await session.scalars(select(Shift).where(Shift.work_date == work_date))
    ).all()

    pct_lines: list[str] = []
    for s in shifts_that_day:
        if s.percent_snapshot > 0:
            bonus = q2_float((to_dec(amount) * to_dec(s.percent_snapshot)) / Decimal("100"))
            pct_lines.append(
                f"• {esc(s.employee.full_name)} ({fmt_percent(s.percent_snapshot)}): +<b>{fmt_money(bonus)}</b>"
            )
            if s.employee and s.employee.telegram_id and s.employee.is_active:
                try:
                    await bot.send_message(
                        s.employee.telegram_id,
                        f"📈 <b>{esc(actor.actor_label)}</b> вніс(ла) касу за "
                        f"<b>{format_date_with_day(work_date)}</b>: <b>{fmt_money(amount)}</b>.\n"
                        f"Твій відсоток ({fmt_percent(s.percent_snapshot)}): <b>+{fmt_money(bonus)}</b> ✨",
                    )
                except Exception:
                    pass

    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"вніс(ла) касу за {format_date_with_day(work_date)}: <b>{fmt_money(amount)}</b>",
    )

    summary_msg = f"Касу за <b>{format_date_with_day(work_date)}</b> збережено: <b>{fmt_money(amount)}</b> ✅"
    if actor.has_perm("can_view_salaries_reports"):
        if pct_lines:
            summary_msg += "\n\nНараховані відсотки за цей день:\n" + "\n".join(pct_lines)
        else:
            summary_msg += "\n<i>За цей день поки немає змін із відсотком від каси.</i>"
    else:
        summary_msg += "\n<i>Працівникам із відсотком від каси автоматично нараховано бонус ✨</i>"

    await message.answer(summary_msg)
    await _show_revenue_menu(message, session, today_local(settings.tz), edit_in_place=False)


# ----------------- 2. ЗАГАЛЬНА ЗВІТНІСТЬ ТА EXCEL -----------------

def _render_unified_report_chunks(
    summaries: list[EmployeePeriodSummary],
    period_title: str,
) -> list[str]:
    header = (
        f"📊 <b>Загальна звітність — {esc(period_title)}</b>\n"
        "<i>(Усі працівники в одній таблиці)</i>\n"
    )
    if not summaries:
        return [header + "\nПоки що немає активних працівників чи змін за цей період 😊"]

    blocks: list[str] = []
    tot_hours = 0.0
    tot_earned = 0.0
    tot_paid = 0.0
    tot_balance = 0.0

    for idx, s in enumerate(summaries, start=1):
        name = esc(s.employee.full_name)
        if not s.employee.is_active:
            name += " <i>(видалений)</i>"

        flags: list[str] = []
        if s.missing_banquet_rate_dates:
            flags.append("⚠️ немає тарифу банкету")
        if s.missing_banquet_guests_dates:
            flags.append("⚠️ не вказані люди на банкеті")
        if s.missing_revenue_dates:
            flags.append("⏳ не внесена каса дня")
        flag_str = f"\n   {' | '.join(flags)}" if flags else ""

        adj_parts: list[str] = []
        if s.bonuses > 0:
            adj_parts.append(f"+прем. {fmt_money(s.bonuses)}")
        if s.fines > 0:
            adj_parts.append(f"−штраф {fmt_money(s.fines)}")
        adj_str = f" | {' '.join(adj_parts)}" if adj_parts else ""

        blocks.append(
            f"<b>{idx}. {name}</b> — {s.shifts_count} зм. ({fmt_hours(s.total_hours)})\n"
            f"   База: {fmt_money(s.base_pay)} | %: {fmt_money(s.percent_pay)} | Банкет: {fmt_money(s.banquet_pay)}{adj_str}\n"
            f"   ✨ Нараховано: <b>{fmt_money(s.earned_period)}</b> | 👛 До видачі: <b>{fmt_money(s.balance_due)}</b>"
            f"{flag_str}"
        )

        tot_hours += s.total_hours
        tot_earned += s.earned_period
        tot_paid += s.paid_period
        tot_balance += s.balance_due

    footer = (
        "\n━━━━━━━━━━━━━━━━━━\n"
        f"🕒 Загалом годин: <b>{fmt_hours(tot_hours)}</b>\n"
        f"✨ Разом нараховано за період: <b>{fmt_money(tot_earned)}</b>\n"
        f"💸 Разом виплачено за період: <b>{fmt_money(tot_paid)}</b>\n"
        f"👛 Загальний залишок до видачі всім: <b>{fmt_money(tot_balance)}</b>"
    )

    chunks: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n{block}"
        if len(candidate) > 3600:
            chunks.append(current)
            current = block
        else:
            current = candidate

    if len(current) + len(footer) > 4000:
        chunks.append(current)
        chunks.append(footer)
    else:
        chunks.append(current + footer)

    return chunks


async def _send_report(
    target_msg: Message,
    session: AsyncSession,
    d1: date,
    d2: date,
    period_title: str,
    edit_in_place: bool,
) -> None:
    summaries = await get_all_employees_summary(session, d1, d2)
    chunks = _render_unified_report_chunks(summaries, period_title)
    kb = owner_reports_kb(d1, d2)

    if edit_in_place and len(chunks) == 1:
        await safe_edit_text(target_msg, chunks[0], reply_markup=kb)
        return

    for idx, chunk in enumerate(chunks):
        markup = kb if idx == len(chunks) - 1 else None
        await target_msg.answer(chunk, reply_markup=markup)


@router.message(StateFilter("*"), F.text == BTN_OWN_REPORTS)
async def menu_reports(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_view_salaries_reports"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    await state.clear()
    today = today_local(settings.tz)
    d1, d2, label = get_period_bounds("month", today)
    await _send_report(message, session, d1, d2, label, edit_in_place=False)


def _payouts_nav_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💸 До виплат і балансів", callback_data="own:pay_menu"),
                HOME_BTN,
            ]
        ]
    )


async def _pay_emp_cancel_kb(state: FSMContext) -> InlineKeyboardMarkup:
    emp_id = (await state.get_data()).get("pay_emp_id")
    return cancel_kb(back_callback=f"own:pay_emp:{emp_id}") if emp_id else cancel_kb(back_callback="own:pay_menu")


@router.callback_query(StateFilter("*"), F.data == "own:rep:custom")
async def cb_reports_custom(
    callback: CallbackQuery,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_view_salaries_reports"):
        await _deny_cb(callback)
        return
    await state.clear()
    await state.set_state(OwnerFinanceStates.waiting_report_custom_period)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Введи дату (<code>12.11.2026</code>) або період (<code>01.11.2026-12.11.2026</code>) 📅",
            reply_markup=cancel_kb(back_callback="own:rep:month"),
        )
    await callback.answer()


@router.message(OwnerFinanceStates.waiting_report_custom_period)
async def msg_reports_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_view_salaries_reports"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        d1, d2 = parse_date_range_input(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="own:rep:month"),
        )
        return
    await state.clear()
    label = format_date_with_day(d1) if d1 == d2 else f"{format_date_short(d1)}–{format_date_short(d2)}"
    await _send_report(message, session, d1, d2, label, edit_in_place=False)


@router.callback_query(StateFilter("*"), F.data.startswith("own:rep:"))
async def cb_reports_preset(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_view_salaries_reports"):
        await _deny_cb(callback)
        return
    await state.clear()
    preset = (callback.data or "").split(":")[2]
    today = today_local(settings.tz)
    d1, d2, label = get_period_bounds(preset, today)
    if callback.message:
        await _send_report(callback.message, session, d1, d2, label, edit_in_place=True)
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:rep_xlsx:"))
async def cb_reports_excel(
    callback: CallbackQuery,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_view_salaries_reports"):
        await _deny_cb(callback)
        return
    parts = (callback.data or "").split(":")
    d1 = date.fromisoformat(parts[2])
    d2 = date.fromisoformat(parts[3])
    summaries = await get_all_employees_summary(session, d1, d2)
    xlsx_bytes = build_payroll_excel(summaries, d1, d2)
    filename = f"zvit_{d1.isoformat()}_{d2.isoformat()}.xlsx"
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(xlsx_bytes, filename=filename),
            caption=f"📥 Excel-звіт за <b>{format_date_short(d1)}–{format_date_short(d2)}</b>",
        )
    await callback.answer("Готую файл Excel 📊")


# ----------------- 3. ВИПЛАТИ І БАЛАНСИ -----------------

async def _show_payouts_menu(
    target_msg: Message,
    session: AsyncSession,
    today: date,
    edit_in_place: bool,
) -> None:
    summaries = await get_all_employees_summary(session, today.replace(day=1), today)
    open_requests = list(
        (
            await session.scalars(
                select(PayoutRequest)
                .where(PayoutRequest.status.in_(("pending", "approved")))
                .order_by(PayoutRequest.created_at.desc(), PayoutRequest.id.desc())
                .limit(15)
            )
        ).all()
    )
    if not summaries and not open_requests:
        text = "💸 <b>Виплати і баланси</b>\nПоки що немає працівників 😊"
    else:
        total_due = q2_float(sum((to_dec(s.balance_due) for s in summaries), Decimal("0.00")))
        req_line = f"\n📬 Відкритих запитів на виплату: <b>{len(open_requests)}</b>" if open_requests else ""
        text = (
            f"💸 <b>Виплати і баланси</b>\n"
            f"Загалом до видачі персоналу: <b>{fmt_money(total_due)}</b>{req_line}\n\n"
            "Обери запит зверху або працівника нижче для виплати:"
        )
    kb = owner_payouts_menu_kb(summaries, open_requests=open_requests)
    if edit_in_place:
        await safe_edit_text(target_msg, text, reply_markup=kb)
    else:
        await target_msg.answer(text, reply_markup=kb)


async def _show_employee_payout_card(
    target_msg: Message,
    session: AsyncSession,
    emp: Employee,
    today: date,
    edit_in_place: bool,
) -> None:
    summary = await get_employee_summary(session, emp, today.replace(day=1), today)
    role_name = emp.role.name if emp.role else "—"
    card_disp = format_card_display(emp.card_number)
    card_line = (
        f"\n💳 Картка: <code>{esc(card_disp)}</code>"
        if card_disp
        else "\n💳 Картка: <i>працівник ще не додав</i>"
    )
    text = (
        f"💸 <b>Виплата для: {esc(emp.full_name)}</b> ({esc(role_name)})\n\n"
        f"✨ Всього зароблено (за весь час): <b>{fmt_money(summary.all_time_earned)}</b>\n"
        f"💵 Вже виплачено (за весь час): <b>{fmt_money(summary.all_time_paid)}</b>\n"
        f"👛 <b>Поточний залишок до видачі: {fmt_money(summary.balance_due)}</b>{card_line}\n\n"
        "Обери зручний спосіб виплати:"
    )
    kb = owner_payout_employee_kb(emp.id, summary.balance_due, card_digits=emp.card_number)
    if edit_in_place:
        await safe_edit_text(target_msg, text, reply_markup=kb)
    else:
        await target_msg.answer(text, reply_markup=kb)


@router.message(StateFilter("*"), F.text == BTN_OWN_PAYOUTS)
async def menu_payouts(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    await state.clear()
    await _show_payouts_menu(message, session, today_local(settings.tz), edit_in_place=False)


@router.callback_query(StateFilter("*"), F.data == "own:pay_menu")
async def cb_payouts_menu(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    await state.clear()
    if callback.message:
        await _show_payouts_menu(callback.message, session, today_local(settings.tz), edit_in_place=True)
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:pay_emp:"))
async def cb_payout_employee(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    await state.clear()
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if callback.message:
        await _show_employee_payout_card(
            callback.message, session, emp, today_local(settings.tz), edit_in_place=True
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:pay_period:"))
async def cb_payout_period_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(pay_emp_id=emp.id)
    await state.set_state(OwnerFinanceStates.waiting_payout_period_dates)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"📅 <b>Виплата за конкретні дні для {esc(emp.full_name)}</b>\n"
            "Введи одну дату (наприклад, <code>12.11.2026</code>) або період "
            "(наприклад, <code>11.11.2026-12.11.2026</code>) — бот сам порахує суму за ці дні:",
            reply_markup=cancel_kb(f"own:pay_emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerFinanceStates.waiting_payout_period_dates)
async def msg_payout_period_dates(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        d1, d2 = parse_date_range_input(message.text or "")
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _pay_emp_cancel_kb(state))
        return

    data = await state.get_data()
    emp = await session.get(Employee, int(data["pay_emp_id"]))
    if emp is None:
        await state.clear()
        await message.answer("Працівника не знайдено")
        return

    summary = await get_employee_summary(session, emp, d1, d2)
    period_str = format_date_with_day(d1) if d1 == d2 else f"{format_date_short(d1)}–{format_date_short(d2)}"
    comment = f"за {format_date_short(d1)}" if d1 == d2 else f"за {format_date_short(d1)}–{format_date_short(d2)}"

    if summary.earned_period <= 0:
        await state.clear()
        await message.answer(
            f"За <b>{period_str}</b> у {esc(emp.full_name)} нараховано <b>0 грн</b>.\n"
            "Можеш ввести суму вручну, якщо хочеш видати аванс 😊",
            reply_markup=owner_payout_employee_kb(emp.id, summary.balance_due, card_digits=emp.card_number),
        )
        return

    await state.update_data(pay_amount=summary.earned_period, pay_comment=comment)
    await state.set_state(OwnerFinanceStates.waiting_payout_confirm)
    await message.answer(
        f"🧮 <b>Розрахунок за {period_str} для {esc(emp.full_name)}:</b>\n"
        f"• Змін: {summary.shifts_count} ({fmt_hours(summary.total_hours)})\n"
        f"• За годинами: {fmt_money(summary.base_pay)} | %: {fmt_money(summary.percent_pay)} | Банкет: {fmt_money(summary.banquet_pay)}\n"
        f"• Нараховано за ці дні: <b>{fmt_money(summary.earned_period)}</b>\n"
        f"• Поточний загальний залишок: <b>{fmt_money(summary.balance_due)}</b>\n\n"
        f"Зафіксувати виплату <b>{fmt_money(summary.earned_period)}</b> ({esc(comment)})?",
        reply_markup=owner_pay_confirm_kb(emp.id),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("own:pay_custom:"))
async def cb_payout_custom_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(pay_emp_id=emp.id)
    await state.set_state(OwnerFinanceStates.waiting_payout_custom_amount)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"💵 <b>Вільна виплата / аванс для {esc(emp.full_name)}</b>\n"
            "Введи суму виплати (у грн):",
            reply_markup=cancel_kb(f"own:pay_emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerFinanceStates.waiting_payout_custom_amount)
async def msg_payout_custom_amount(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        amount = parse_positive_float(message.text or "", allow_zero=False)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _pay_emp_cancel_kb(state))
        return
    data = await state.get_data()
    emp_id = int(data["pay_emp_id"])
    await state.update_data(pay_amount=amount)
    await state.set_state(OwnerFinanceStates.waiting_payout_custom_comment)
    await message.answer(
        f"Сума виплати: <b>{fmt_money(amount)}</b>.\n"
        "Введи короткий коментар (наприклад, <code>аванс</code> або <code>-</code> без коментаря):",
        reply_markup=cancel_kb(f"own:pay_emp:{emp_id}"),
    )


@router.message(OwnerFinanceStates.waiting_payout_custom_comment)
async def msg_payout_custom_comment(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    data = await state.get_data()
    emp = await session.get(Employee, int(data["pay_emp_id"]))
    if emp is None:
        await state.clear()
        await message.answer("Працівника не знайдено")
        return

    amount = float(data["pay_amount"])
    raw = (message.text or "").strip()
    comment = None if raw in {"", "-", "—"} else raw[:240]
    await state.update_data(pay_comment=comment)
    await state.set_state(OwnerFinanceStates.waiting_payout_confirm)

    comment_line = f" ({esc(comment)})" if comment else ""
    await message.answer(
        f"Підтвердити виплату для <b>{esc(emp.full_name)}</b>:\n"
        f"💸 Сума: <b>{fmt_money(amount)}</b>{comment_line}?",
        reply_markup=owner_pay_confirm_kb(emp.id),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("own:pay_full:"))
async def cb_payout_full(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return

    today = today_local(settings.tz)
    summary = await get_employee_summary(session, emp, today.replace(day=1), today)
    if summary.balance_due <= 0:
        await callback.answer("Залишок вже дорівнює 0 грн", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        pay_emp_id=emp.id,
        pay_amount=summary.balance_due,
        pay_comment="повний розрахунок",
    )
    await state.set_state(OwnerFinanceStates.waiting_payout_confirm)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"💯 Закрити весь поточний залишок для <b>{esc(emp.full_name)}</b>?\n"
            f"Сума до виплати: <b>{fmt_money(summary.balance_due)}</b>",
            reply_markup=owner_pay_confirm_kb(emp.id),
        )
    await callback.answer()


@router.callback_query(OwnerFinanceStates.waiting_payout_confirm, F.data == "own:pay_confirm_ok")
async def cb_payout_confirm_ok(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await state.clear()
        await _deny_cb(callback)
        return
    data = await state.get_data()
    await state.clear()
    emp = await session.get(Employee, int(data["pay_emp_id"]))
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return

    amount = float(data["pay_amount"])
    comment: str | None = data.get("pay_comment")
    today = today_local(settings.tz)

    payout = Payout(
        employee_id=emp.id,
        paid_date=today,
        amount=amount,
        comment=comment,
    )
    session.add(payout)
    await session.commit()

    new_summary = await get_employee_summary(session, emp, today.replace(day=1), today)

    if emp.telegram_id:
        comment_str = f" ({esc(comment)})" if comment else ""
        try:
            await bot.send_message(
                emp.telegram_id,
                f"💸 <b>{esc(actor.actor_label)}</b> зафіксував(ла) тобі виплату: "
                f"<b>{fmt_money(amount)}</b>{comment_str}\n"
                f"Твій поточний залишок до видачі: <b>{fmt_money(new_summary.balance_due)}</b> 🌸",
            )
        except Exception:
            pass

    comment_note = f" ({esc(comment)})" if comment else ""
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"оформив(ла) виплату працівнику <b>{esc(emp.full_name)}</b>: "
        f"<b>{fmt_money(amount)}</b>{comment_note}",
    )

    await callback.answer("Виплату збережено ✅")
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Виплату <b>{fmt_money(amount)}</b> для <b>{esc(emp.full_name)}</b> зафіксовано! ✅\n"
            f"Новий залишок до видачі: <b>{fmt_money(new_summary.balance_due)}</b>",
            reply_markup=owner_payout_employee_kb(emp.id, new_summary.balance_due, card_digits=emp.card_number),
        )


@router.callback_query(StateFilter("*"), F.data.startswith("own:pay_hist:"))
async def cb_payout_history(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return

    payouts = (
        await session.scalars(
            select(Payout)
            .where(Payout.employee_id == emp.id)
            .order_by(Payout.paid_date.desc(), Payout.id.desc())
            .limit(10)
        )
    ).all()
    adjustments = (
        await session.scalars(
            select(Adjustment)
            .where(Adjustment.employee_id == emp.id)
            .order_by(Adjustment.adj_date.desc(), Adjustment.id.desc())
            .limit(10)
        )
    ).all()

    lines = [f"📜 <b>Історія виплат та нарахувань: {esc(emp.full_name)}</b>", ""]
    lines.append("<b>Останні виплати:</b>")
    if payouts:
        for p in payouts:
            c = f" ({esc(p.comment)})" if p.comment else ""
            lines.append(f"• {format_date_with_day(p.paid_date)}: <b>{fmt_money(p.amount)}</b>{c}")
    else:
        lines.append("• Виплат ще не було")

    lines.append("")
    lines.append("<b>Останні премії та штрафи:</b>")
    if adjustments:
        for a in adjustments:
            sign = "➕ Премія" if a.kind == "bonus" else "➖ Штраф"
            c = f" ({esc(a.comment)})" if a.comment else ""
            lines.append(f"• {format_date_with_day(a.adj_date)} — {sign}: <b>{fmt_money(a.amount)}</b>{c}")
    else:
        lines.append("• Премій і штрафів ще не було")

    today = today_local(settings.tz)
    summary = await get_employee_summary(session, emp, today.replace(day=1), today)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines),
            reply_markup=owner_payout_employee_kb(emp.id, summary.balance_due, card_digits=emp.card_number),
        )
    await callback.answer()


# ----------------- 4. ОБРОБКА ЗАПИТІВ НА ВИПЛАТУ (ЧАТ + КАРТКА) -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:req_view:"))
async def cb_payout_request_view(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    req_id = int((callback.data or "").split(":")[2])
    req = await session.get(PayoutRequest, req_id)
    if req is None or req.employee is None:
        await callback.answer("Запит не знайдено", show_alert=True)
        return
    today = today_local(settings.tz)
    summary = await get_employee_summary(session, req.employee, today.replace(day=1), today)
    role_name = req.employee.role.name if req.employee.role else "—"
    comment_line = f"\n💬 Коментар: <i>{esc(req.comment)}</i>" if req.comment else ""
    clean_card = req.employee.card_number or req.card_snapshot

    if req.status == "pending":
        text = (
            f"⏳ <b>Запит на виплату від {esc(req.employee.full_name)}</b> ({esc(role_name)})\n\n"
            f"💰 Сума запиту: <b>{fmt_money(req.amount)}</b>\n"
            f"👛 Поточний залишок: <b>{fmt_money(summary.balance_due)}</b>{comment_line}\n"
            "💳 Натисни «Прийняти», щоб побачити і скопіювати номер картки ✨"
        )
        kb = owner_payout_request_pending_kb(req.id)
    elif req.status == "approved":
        card_disp = format_card_display(clean_card) or "не вказано"
        text = (
            f"✅ <b>Запит прийнято! Перекажи {fmt_money(req.amount)}</b>\n\n"
            f"👤 Працівник: <b>{esc(req.employee.full_name)}</b> ({esc(role_name)})\n"
            f"👛 Залишок до видачі: <b>{fmt_money(summary.balance_due)}</b>{comment_line}\n"
            f"💳 Картка для переказу (натисни, щоб скопіювати):\n"
            f"<code>{esc(clean_card or '')}</code> ({esc(card_disp)})"
        )
        kb = owner_payout_request_approved_kb(req.id, clean_card, req.amount)
    else:
        text = f"Запит #{req.id} має статус: <b>{esc(req.status)}</b>."
        kb = _payouts_nav_kb()

    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:req_accept:"))
async def cb_payout_request_accept(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    req_id = int((callback.data or "").split(":")[2])
    try:
        was_pending = (await session.get(PayoutRequest, req_id))
        notify_now = was_pending is not None and was_pending.status == "pending"
        req = await accept_payout_request(session, req_id, actor.telegram_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    emp = await session.get(Employee, req.employee_id)
    if notify_now and emp is not None:
        await notify_employee_payout_request_accepted(bot, emp, req)

    clean_card = (emp.card_number if emp and emp.card_number else req.card_snapshot) or ""
    card_disp = format_card_display(clean_card) or "не вказано"
    role_name = emp.role.name if emp and emp.role else "—"
    comment_line = f"\n💬 Коментар: <i>{esc(req.comment)}</i>" if req.comment else ""

    text = (
        f"✅ <b>Запит прийнято! Перекажи {fmt_money(req.amount)}:</b>\n\n"
        f"👤 <b>{esc(emp.full_name if emp else '')}</b> ({esc(role_name)}){comment_line}\n"
        f"💳 Номер картки (натисни, щоб скопіювати):\n"
        f"<code>{esc(clean_card)}</code>\n"
        f"<i>({esc(card_disp)})</i>"
    )
    kb = owner_payout_request_approved_kb(req.id, clean_card or None, req.amount)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb)
    await callback.answer("Запит прийнято! Номер картки відкрито 💳")


@router.callback_query(StateFilter("*"), F.data.startswith("own:req_reject:"))
async def cb_payout_request_reject(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    req_id = int((callback.data or "").split(":")[2])
    try:
        req = await reject_payout_request(session, req_id, actor.telegram_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    emp = await session.get(Employee, req.employee_id)
    if emp is not None:
        await notify_employee_payout_request_rejected(bot, emp, req)

    if callback.message:
        await safe_edit_text(
            callback.message,
            f"❌ Запит на виплату <b>{fmt_money(req.amount)}</b> від <b>{esc(emp.full_name if emp else '')}</b> відхилено.",
            reply_markup=_payouts_nav_kb(),
        )
    await callback.answer("Запит відхилено")


@router.callback_query(StateFilter("*"), F.data.startswith("own:req_pay:"))
async def cb_payout_request_mark_paid(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_payout"):
        await _deny_cb(callback)
        return
    req_id = int((callback.data or "").split(":")[2])
    today = today_local(settings.tz)
    try:
        req, payout, new_balance = await mark_payout_request_paid(
            session=session,
            req_id=req_id,
            actor_tg_id=actor.telegram_id,
            paid_date=today,
        )
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    emp = await session.get(Employee, req.employee_id)
    if emp is not None:
        await notify_payout_created(session, bot, settings, actor, emp, payout.amount, payout.comment, new_balance)

    if callback.message:
        await safe_edit_text(
            callback.message,
            f"💸 <b>Виплату проведено!</b>\n"
            f"👤 Працівник: <b>{esc(emp.full_name if emp else '')}</b>\n"
            f"💵 Сума: <b>{fmt_money(payout.amount)}</b>\n"
            f"👛 Новий залишок до видачі: <b>{fmt_money(new_balance)}</b> ✅",
            reply_markup=_payouts_nav_kb(),
        )
    await callback.answer("Виплату списано з балансу ✅")
