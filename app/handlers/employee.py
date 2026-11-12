from __future__ import annotations

import logging
from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import DailyRevenue, Employee, PayoutRequest, Shift
from app.filters import IsEmployeeFilter
from app.handlers.helpers import parse_positive_float, parse_positive_int, safe_edit_text
from app.keyboards.inline import (
    cancel_kb,
    employee_banquet_choice_kb,
    employee_card_and_requests_kb,
    employee_date_kb,
    employee_guests_skip_kb,
    employee_payout_req_amount_kb,
    employee_salary_periods_kb,
    employee_shift_actions_kb,
    employee_shifts_list_kb,
    employee_time_choice_kb,
)
from app.keyboards.reply import (
    BTN_EMP_MY_SALARY,
    BTN_EMP_MY_SHIFTS,
    BTN_EMP_PAYOUT_REQ,
    BTN_EMP_SHIFT,
)
from app.services.access import ActorContext, get_all_owner_ids
from app.services.notifications import notify_owners_payout_request_created
from app.services.payout_requests import (
    cancel_payout_request,
    create_payout_request,
    format_card_display,
    save_employee_card,
)
from app.services.payroll import (
    EmployeePeriodSummary,
    ShiftBreakdown,
    compute_shift_breakdown,
    get_employee_summary,
)
from app.states import EmployeeShiftStates
from app.utils.datetime_utils import (
    format_date_short,
    format_date_with_day,
    get_period_bounds,
    parse_date_input,
    parse_date_range_input,
    parse_time_range,
    today_local,
)
from app.utils.formatting import esc, fmt_hours, fmt_money, fmt_percent

router = Router(name="employee")
router.message.filter(IsEmployeeFilter())
router.callback_query.filter(IsEmployeeFilter())
logger = logging.getLogger(__name__)

SHIFTS_PAGE_SIZE = 8


def render_shift_card_text(bd: ShiftBreakdown, for_owner: bool = False) -> str:
    s = bd.shift
    lines = [
        f"📅 <b>{format_date_with_day(s.work_date, full=True)}</b>",
        f"⏱ Час: <b>{s.start_time}-{s.end_time}</b> ({fmt_hours(s.hours)})",
        f"💵 За годинами: <b>{fmt_money(bd.base_pay)}</b> ({fmt_money(s.hourly_rate_snapshot)}/год)",
    ]
    if s.percent_snapshot > 0:
        if bd.daily_revenue is not None:
            lines.append(
                f"📈 Відсоток ({fmt_percent(s.percent_snapshot)} від каси {fmt_money(bd.daily_revenue)}): "
                f"<b>{fmt_money(bd.percent_pay)}</b>"
            )
        else:
            lines.append(
                f"📈 Відсоток ({fmt_percent(s.percent_snapshot)}): очікуємо касу дня ⏳"
            )

    if s.is_banquet:
        if bd.missing_banquet_rate and bd.missing_banquet_guests:
            if for_owner:
                lines.append("🎉 Банкет: не вказано тариф та кількість людей ⚠️")
            else:
                lines.append("🎉 Банкет: <b>Внесіть кількість людей</b> 👥 • <b>Звернись до Ані</b> 🙏")
        elif bd.missing_banquet_rate:
            if for_owner:
                lines.append(f"🎉 Банкет ({s.banquet_guests} люд.): не вказано тариф банкету ⚠️")
            else:
                lines.append(f"🎉 Банкет ({s.banquet_guests} люд.): <b>Звернись до Ані</b> 🙏")
        elif bd.missing_banquet_guests:
            lines.append("🎉 Банкет: <b>Внесіть кількість людей</b> 👥")
        else:
            lines.append(
                f"🎉 Банкет: <b>{fmt_money(bd.banquet_pay)}</b> "
                f"({s.banquet_guests} люд. × {fmt_money(s.banquet_rate_snapshot or 0)})"
            )

    lines.append(f"✨ Разом за зміну: <b>{fmt_money(bd.total_pay)}</b>")
    return "\n".join(lines)


def render_employee_salary_text(summary: EmployeePeriodSummary, period_title: str) -> str:
    emp = summary.employee
    role_name = emp.role.name if emp.role else "—"
    lines = [
        f"💰 <b>Моя зарплата — {esc(period_title)}</b>",
        f"👤 {esc(emp.full_name)} ({esc(role_name)})",
        "",
        f"📅 Відпрацьовано змін: <b>{summary.shifts_count}</b> ({fmt_hours(summary.total_hours)})",
        f"💵 За годинами: <b>{fmt_money(summary.base_pay)}</b>",
        f"📈 Відсотки від каси: <b>{fmt_money(summary.percent_pay)}</b>",
        f"🎉 Банкети: <b>{fmt_money(summary.banquet_pay)}</b>",
    ]
    if summary.bonuses > 0:
        lines.append(f"🎁 Премії: <b>+{fmt_money(summary.bonuses)}</b>")
    if summary.fines > 0:
        lines.append(f"📌 Штрафи: <b>−{fmt_money(summary.fines)}</b>")

    lines.extend([
        "",
        f"✨ Нараховано за період: <b>{fmt_money(summary.earned_period)}</b>",
        f"💸 Виплачено за період: <b>{fmt_money(summary.paid_period)}</b>",
        f"👛 Поточний залишок до видачі: <b>{fmt_money(summary.balance_due)}</b>",
    ])

    warnings: list[str] = []
    if summary.missing_banquet_rate_dates:
        dates_str = ", ".join(format_date_short(d) for d in summary.missing_banquet_rate_dates)
        warnings.append(f"🙏 Звернись до Ані — не вказано тариф банкету за: {dates_str}")
    if summary.missing_banquet_guests_dates:
        dates_str = ", ".join(format_date_short(d) for d in summary.missing_banquet_guests_dates)
        warnings.append(f"👥 Внесіть кількість людей на банкеті за: {dates_str}")
    if summary.missing_revenue_dates:
        dates_str = ", ".join(format_date_short(d) for d in summary.missing_revenue_dates)
        warnings.append(f"⏳ Каса дня ще не внесена за: {dates_str}")

    if warnings:
        lines.append("")
        lines.extend(warnings)

    return "\n".join(lines)


async def notify_owners_missing_banquet_rate(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    employee: Employee,
    work_date: date,
    guests: int | None,
) -> None:
    from app.keyboards.inline import owner_notify_banquet_kb

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


async def _load_shift_breakdown(session: AsyncSession, shift: Shift) -> ShiftBreakdown:
    rev = await session.scalar(
        select(DailyRevenue.amount).where(DailyRevenue.work_date == shift.work_date)
    )
    rev_map = {shift.work_date: rev} if rev is not None else {}
    return compute_shift_breakdown(shift, rev_map)


async def _get_last_time_range(session: AsyncSession, emp: Employee) -> tuple[str, str] | None:
    if emp.last_start_time and emp.last_end_time:
        return emp.last_start_time, emp.last_end_time
    last_shift = await session.scalar(
        select(Shift)
        .where(Shift.employee_id == emp.id)
        .order_by(Shift.work_date.desc(), Shift.id.desc())
    )
    if last_shift is not None:
        return last_shift.start_time, last_shift.end_time
    return None


async def _process_picked_date(
    target_msg: Message,
    state: FSMContext,
    session: AsyncSession,
    emp: Employee,
    work_date: date,
    edit_in_place: bool = False,
) -> None:
    existing = await session.scalar(
        select(Shift).where(Shift.employee_id == emp.id, Shift.work_date == work_date)
    )
    if existing is not None:
        await state.clear()
        bd = await _load_shift_breakdown(session, existing)
        text = (
            "За цю дату зміна вже внесена ✨\n\n"
            + render_shift_card_text(bd)
        )
        kb = employee_shift_actions_kb(existing, show_back=False)
        if edit_in_place:
            await safe_edit_text(target_msg, text, reply_markup=kb)
        else:
            await target_msg.answer(text, reply_markup=kb)
        return

    last_time = await _get_last_time_range(session, emp)
    await state.update_data(work_date=work_date.isoformat())
    await state.set_state(EmployeeShiftStates.waiting_time_range)
    if last_time is not None:
        prompt = (
            f"Обрана дата: <b>{format_date_with_day(work_date, full=True)}</b> 📅\n"
            "Натисни кнопку з минулим часом або введи інший у форматі <b>ГГ:ХХ-ГГ:ХХ</b> "
            "(наприклад, <code>10:00-22:00</code> або <code>18:00-02:00</code>):"
        )
    else:
        prompt = (
            f"Обрана дата: <b>{format_date_with_day(work_date, full=True)}</b> 📅\n"
            "Введи час зміни у форматі <b>ГГ:ХХ-ГГ:ХХ</b> (наприклад, <code>10:00-22:00</code> або <code>18:00-02:00</code>):"
        )
    kb = employee_time_choice_kb(last_time)
    if edit_in_place:
        await safe_edit_text(target_msg, prompt, reply_markup=kb)
    else:
        await target_msg.answer(prompt, reply_markup=kb)


# ----------------- 1. ВНЕСЕННЯ / РЕДАГУВАННЯ ЗМІНИ -----------------

@router.message(StateFilter("*"), F.text == BTN_EMP_SHIFT)
async def menu_enter_shift(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    await state.clear()
    today = today_local(settings.tz)
    await message.answer(
        "Обери дату зміни або введи її вручну у форматі <b>ДД.ММ.РРРР</b> 📅",
        reply_markup=employee_date_kb(today),
    )


@router.callback_query(StateFilter("*"), F.data == "emp:shift_menu")
async def cb_shift_menu(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    await state.clear()
    today = today_local(settings.tz)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Обери дату зміни або введи її вручну у форматі <b>ДД.ММ.РРРР</b> 📅",
            reply_markup=employee_date_kb(today),
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data == "emp:date_manual")
async def cb_date_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(EmployeeShiftStates.waiting_manual_date)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Введи дату зміни у форматі <b>ДД.ММ.РРРР</b> (наприклад, <code>12.11.2026</code>) 📅",
            reply_markup=cancel_kb(back_callback="emp:shift_menu"),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_manual_date)
async def msg_manual_date(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    try:
        work_date = parse_date_input(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"Ой, не вийшло: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="emp:shift_menu"),
        )
        return

    assert actor.employee is not None
    await _process_picked_date(message, state, session, actor.employee, work_date, edit_in_place=False)


@router.callback_query(StateFilter("*"), F.data.startswith("emp:date:"))
async def cb_date_picked(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    await state.clear()
    raw_date = (callback.data or "").split(":", 2)[2]
    work_date = date.fromisoformat(raw_date)
    assert actor.employee is not None
    if callback.message:
        await _process_picked_date(
            callback.message, state, session, actor.employee, work_date, edit_in_place=True
        )
    await callback.answer()


@router.callback_query(EmployeeShiftStates.waiting_time_range, F.data == "emp:time_last")
async def cb_time_last(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    last_time = await _get_last_time_range(session, actor.employee)
    if last_time is None:
        await callback.answer("Немає збереженого часу — введи вручну 😊", show_alert=True)
        return
    start_time, end_time = last_time
    _, _, hours = parse_time_range(f"{start_time}-{end_time}")
    await state.update_data(start_time=start_time, end_time=end_time, hours=hours)
    await state.set_state(EmployeeShiftStates.waiting_banquet_choice)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Години пораховано: <b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ⏱\n"
            "Чи був у цей день банкет?",
            reply_markup=employee_banquet_choice_kb(),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_time_range)
async def msg_time_range(message: Message, state: FSMContext) -> None:
    try:
        start_time, end_time, hours = parse_time_range(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"Ой, перевір формат часу: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="emp:shift_menu"),
        )
        return

    await state.update_data(start_time=start_time, end_time=end_time, hours=hours)
    await state.set_state(EmployeeShiftStates.waiting_banquet_choice)
    await message.answer(
        f"Години пораховано: <b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ⏱\n"
        "Чи був у цей день банкет?",
        reply_markup=employee_banquet_choice_kb(),
    )


@router.callback_query(StateFilter("*"), F.data == "emp:time_back")
async def cb_time_back(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    data = await state.get_data()
    raw_date = data.get("work_date")
    if not raw_date or actor.employee is None:
        await state.clear()
        today = today_local(settings.tz)
        if callback.message:
            await safe_edit_text(
                callback.message,
                "Обери дату зміни або введи її вручну у форматі <b>ДД.ММ.РРРР</b> 📅",
                reply_markup=employee_date_kb(today),
            )
        await callback.answer()
        return

    work_date = date.fromisoformat(str(raw_date))
    last_time = await _get_last_time_range(session, actor.employee)
    await state.set_state(EmployeeShiftStates.waiting_time_range)
    if last_time is not None:
        prompt = (
            f"Обрана дата: <b>{format_date_with_day(work_date, full=True)}</b> 📅\n"
            "Натисни кнопку з минулим часом або введи інший у форматі <b>ГГ:ХХ-ГГ:ХХ</b> "
            "(наприклад, <code>10:00-22:00</code> або <code>18:00-02:00</code>):"
        )
    else:
        prompt = (
            f"Обрана дата: <b>{format_date_with_day(work_date, full=True)}</b> 📅\n"
            "Введи час зміни у форматі <b>ГГ:ХХ-ГГ:ХХ</b> (наприклад, <code>10:00-22:00</code> або <code>18:00-02:00</code>):"
        )
    if callback.message:
        await safe_edit_text(
            callback.message,
            prompt,
            reply_markup=employee_time_choice_kb(last_time),
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data == "emp:banquet_back")
async def cb_banquet_back(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
) -> None:
    data = await state.get_data()
    start_time = data.get("start_time")
    end_time = data.get("end_time")
    hours = data.get("hours")
    if not start_time or not end_time or hours is None:
        await state.clear()
        today = today_local(settings.tz)
        if callback.message:
            await safe_edit_text(
                callback.message,
                "Обери дату зміни або введи її вручну у форматі <b>ДД.ММ.РРРР</b> 📅",
                reply_markup=employee_date_kb(today),
            )
        await callback.answer()
        return

    await state.set_state(EmployeeShiftStates.waiting_banquet_choice)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Години пораховано: <b>{start_time}-{end_time}</b> ({fmt_hours(float(hours))}) ⏱\n"
            "Чи був у цей день банкет?",
            reply_markup=employee_banquet_choice_kb(),
        )
    await callback.answer()


async def _finalize_new_shift(
    target_msg: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    emp: Employee,
    is_banquet: bool,
    banquet_guests: int | None,
    edit_in_place: bool,
) -> None:
    data = await state.get_data()
    work_date = date.fromisoformat(data["work_date"])
    start_time: str = data["start_time"]
    end_time: str = data["end_time"]
    hours: float = float(data["hours"])
    await state.clear()

    # Повторна перевірка від дубля у випадку подвійного натискання
    existing = await session.scalar(
        select(Shift).where(Shift.employee_id == emp.id, Shift.work_date == work_date)
    )
    if existing is not None:
        bd = await _load_shift_breakdown(session, existing)
        kb = employee_shift_actions_kb(existing, show_back=False)
        msg_txt = "За цю дату зміна вже збережена ✨\n\n" + render_shift_card_text(bd)
        if edit_in_place:
            await safe_edit_text(target_msg, msg_txt, reply_markup=kb)
        else:
            await target_msg.answer(msg_txt, reply_markup=kb)
        return

    shift = Shift(
        employee_id=emp.id,
        work_date=work_date,
        start_time=start_time,
        end_time=end_time,
        hours=hours,
        hourly_rate_snapshot=emp.hourly_rate,
        percent_snapshot=emp.revenue_percent,
        is_banquet=is_banquet,
        banquet_guests=banquet_guests if is_banquet else None,
        banquet_rate_snapshot=emp.banquet_rate_per_person if is_banquet else None,
    )
    emp.last_start_time = start_time
    emp.last_end_time = end_time
    session.add(shift)
    await session.commit()
    await session.refresh(shift)

    if is_banquet and emp.banquet_rate_per_person is None:
        await notify_owners_missing_banquet_rate(session, bot, settings, emp, work_date, banquet_guests)

    bd = await _load_shift_breakdown(session, shift)
    notes: list[str] = ["Зміну збережено! 🌸"]
    if is_banquet and emp.banquet_rate_per_person is None:
        notes.append("Для розрахунку банкету — <b>звернись до Ані</b> 🙏 (вона вже отримала сповіщення).")
    if is_banquet and banquet_guests is None:
        notes.append("Не забудь потім натиснути <b>«Внести кількість людей»</b> 👥")

    full_text = "\n".join(notes) + "\n\n" + render_shift_card_text(bd)
    kb = employee_shift_actions_kb(shift, show_back=False)
    if edit_in_place:
        await safe_edit_text(target_msg, full_text, reply_markup=kb)
    else:
        await target_msg.answer(full_text, reply_markup=kb)


@router.callback_query(EmployeeShiftStates.waiting_banquet_choice, F.data == "emp:banquet:no")
async def cb_banquet_no(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    if callback.message:
        await _finalize_new_shift(
            callback.message,
            state,
            session,
            bot,
            settings,
            actor.employee,
            is_banquet=False,
            banquet_guests=None,
            edit_in_place=True,
        )
    await callback.answer()


@router.callback_query(EmployeeShiftStates.waiting_banquet_choice, F.data == "emp:banquet:yes")
async def cb_banquet_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EmployeeShiftStates.waiting_banquet_guests)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Скільки було людей на банкеті? 👥\nВведи кількість числом (наприклад, <code>15</code>):",
            reply_markup=employee_guests_skip_kb(),
        )
    await callback.answer()


@router.callback_query(EmployeeShiftStates.waiting_banquet_guests, F.data == "emp:guests:skip")
async def cb_guests_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    if callback.message:
        await _finalize_new_shift(
            callback.message,
            state,
            session,
            bot,
            settings,
            actor.employee,
            is_banquet=True,
            banquet_guests=None,
            edit_in_place=True,
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_banquet_guests)
async def msg_banquet_guests(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    try:
        guests = parse_positive_int(message.text or "")
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=employee_guests_skip_kb())
        return

    assert actor.employee is not None
    await _finalize_new_shift(
        message,
        state,
        session,
        bot,
        settings,
        actor.employee,
        is_banquet=True,
        banquet_guests=guests,
        edit_in_place=False,
    )


# ----------------- РЕДАГУВАННЯ ГОДИН ТА КІЛЬКОСТІ ЛЮДЕЙ -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("emp:edit_hours:"))
async def cb_edit_hours_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    shift_id = int((callback.data or "").split(":")[2])
    shift = await session.get(Shift, shift_id)
    assert actor.employee is not None
    if shift is None or shift.employee_id != actor.employee.id:
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return

    await state.clear()
    await state.update_data(edit_shift_id=shift.id)
    await state.set_state(EmployeeShiftStates.waiting_edit_hours)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Зміна за <b>{format_date_with_day(shift.work_date)}</b> (зараз: <code>{shift.start_time}-{shift.end_time}</code>).\n"
            "Введи новий час у форматі <b>ГГ:ХХ-ГГ:ХХ</b> ⏱",
            reply_markup=cancel_kb(back_callback=f"emp:shift_view:{shift.id}"),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_edit_hours)
async def msg_edit_hours_value(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    data = await state.get_data()
    edit_shift_id = data.get("edit_shift_id")
    back_cb = f"emp:shift_view:{edit_shift_id}" if edit_shift_id else None
    try:
        start_time, end_time, hours = parse_time_range(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"Ой, перевір формат часу: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback=back_cb),
        )
        return

    shift_id = int(edit_shift_id)
    shift = await session.get(Shift, shift_id)
    assert actor.employee is not None
    if shift is None or shift.employee_id != actor.employee.id:
        await state.clear()
        await message.answer("Недостатньо прав 🔒")
        return

    shift.start_time = start_time
    shift.end_time = end_time
    shift.hours = hours
    actor.employee.last_start_time = start_time
    actor.employee.last_end_time = end_time
    await session.commit()
    await session.refresh(shift)
    await state.clear()

    bd = await _load_shift_breakdown(session, shift)
    await message.answer(
        "Години оновлено! 🌸\n\n" + render_shift_card_text(bd),
        reply_markup=employee_shift_actions_kb(shift, show_back=False),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("emp:fill_guests:"))
async def cb_fill_guests_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    shift_id = int((callback.data or "").split(":")[2])
    shift = await session.get(Shift, shift_id)
    assert actor.employee is not None
    if shift is None or shift.employee_id != actor.employee.id:
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return

    await state.clear()
    await state.update_data(fill_shift_id=shift.id)
    await state.set_state(EmployeeShiftStates.waiting_fill_guests)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Банкет за <b>{format_date_with_day(shift.work_date)}</b> 🎉\n"
            "Внесіть кількість людей (наприклад, <code>20</code>):",
            reply_markup=cancel_kb(back_callback=f"emp:shift_view:{shift.id}"),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_fill_guests)
async def msg_fill_guests_value(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    data = await state.get_data()
    fill_shift_id = data.get("fill_shift_id")
    back_cb = f"emp:shift_view:{fill_shift_id}" if fill_shift_id else None
    try:
        guests = parse_positive_int(message.text or "")
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=cancel_kb(back_callback=back_cb))
        return

    shift_id = int(fill_shift_id)
    shift = await session.get(Shift, shift_id)
    assert actor.employee is not None
    if shift is None or shift.employee_id != actor.employee.id:
        await state.clear()
        await message.answer("Недостатньо прав 🔒")
        return

    shift.banquet_guests = guests
    await session.commit()
    await session.refresh(shift)
    await state.clear()

    if shift.banquet_rate_snapshot is None:
        await notify_owners_missing_banquet_rate(session, bot, settings, actor.employee, shift.work_date, guests)

    bd = await _load_shift_breakdown(session, shift)
    header = "Кількість людей збережено! 🌸"
    if shift.banquet_rate_snapshot is None:
        header += "\nДля розрахунку банкету — <b>звернись до Ані</b> 🙏"
    await message.answer(
        header + "\n\n" + render_shift_card_text(bd),
        reply_markup=employee_shift_actions_kb(shift, show_back=False),
    )


# ----------------- 2. МОЯ ЗАРПЛАТА -----------------

@router.message(StateFilter("*"), F.text == BTN_EMP_MY_SALARY)
async def menu_my_salary(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    await state.clear()
    assert actor.employee is not None
    today = today_local(settings.tz)
    d1, d2, label = get_period_bounds("month", today)
    summary = await get_employee_summary(session, actor.employee, d1, d2)
    await message.answer(
        render_employee_salary_text(summary, label),
        reply_markup=employee_salary_periods_kb(),
    )


@router.callback_query(StateFilter("*"), F.data == "emp:salary:custom")
async def cb_my_salary_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(EmployeeShiftStates.waiting_salary_custom_period)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Введи дату (<code>12.11.2026</code>) або період (<code>01.11.2026-12.11.2026</code>) 📅",
            reply_markup=cancel_kb(back_callback="emp:salary:month"),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_salary_custom_period)
async def msg_my_salary_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    try:
        d1, d2 = parse_date_range_input(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="emp:salary:month"),
        )
        return

    await state.clear()
    assert actor.employee is not None
    label = (
        format_date_with_day(d1)
        if d1 == d2
        else f"{format_date_short(d1)}–{format_date_short(d2)}"
    )
    summary = await get_employee_summary(session, actor.employee, d1, d2)
    await message.answer(
        render_employee_salary_text(summary, label),
        reply_markup=employee_salary_periods_kb(),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("emp:salary:"))
async def cb_my_salary_preset(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    await state.clear()
    preset = (callback.data or "").split(":")[2]
    today = today_local(settings.tz)
    d1, d2, label = get_period_bounds(preset, today)
    assert actor.employee is not None
    summary = await get_employee_summary(session, actor.employee, d1, d2)
    if callback.message:
        await safe_edit_text(
            callback.message,
            render_employee_salary_text(summary, label),
            reply_markup=employee_salary_periods_kb(),
        )
    await callback.answer()


# ----------------- 3. МОЇ ЗМІНИ -----------------

async def _render_my_shifts_page(
    session: AsyncSession,
    emp: Employee,
    page: int,
) -> tuple[str, list[Shift], bool]:
    offset = page * SHIFTS_PAGE_SIZE
    rows = (
        await session.scalars(
            select(Shift)
            .where(Shift.employee_id == emp.id)
            .order_by(Shift.work_date.desc())
            .offset(offset)
            .limit(SHIFTS_PAGE_SIZE + 1)
        )
    ).all()
    has_next = len(rows) > SHIFTS_PAGE_SIZE
    shifts = list(rows[:SHIFTS_PAGE_SIZE])
    if not shifts and page == 0:
        text = "У тебе ще немає внесених змін 🌸\nНатисни «🕒 Внести / змінити зміну», щоб додати першу!"
    else:
        text = "📅 <b>Твої зміни</b>\nНатисни на потрібний день, щоб переглянути деталі або змінити години:"
    return text, shifts, has_next


@router.message(StateFilter("*"), F.text == BTN_EMP_MY_SHIFTS)
async def menu_my_shifts(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    await state.clear()
    assert actor.employee is not None
    text, shifts, has_next = await _render_my_shifts_page(session, actor.employee, 0)
    await message.answer(text, reply_markup=employee_shifts_list_kb(shifts, 0, has_next))


@router.callback_query(StateFilter("*"), F.data.startswith("emp:shifts_page:"))
async def cb_my_shifts_page(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    await state.clear()
    page = int((callback.data or "").split(":")[2])
    assert actor.employee is not None
    text, shifts, has_next = await _render_my_shifts_page(session, actor.employee, page)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=employee_shifts_list_kb(shifts, page, has_next))
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("emp:shift_view:"))
async def cb_my_shift_view(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    await state.clear()
    shift_id = int((callback.data or "").split(":")[2])
    shift = await session.get(Shift, shift_id)
    assert actor.employee is not None
    if shift is None or shift.employee_id != actor.employee.id:
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return

    bd = await _load_shift_breakdown(session, shift)
    if callback.message:
        await safe_edit_text(
            callback.message,
            render_shift_card_text(bd),
            reply_markup=employee_shift_actions_kb(shift, back_callback="emp:shifts_page:0", show_back=True),
        )
    await callback.answer()


# ----------------- 4. КАРТКА ТА ЗАПИТ НА ВИПЛАТУ -----------------

async def _render_card_and_requests_text(
    session: AsyncSession,
    emp: Employee,
    today: date,
) -> tuple[str, PayoutRequest | None]:
    summary = await get_employee_summary(session, emp, today.replace(day=1), today)
    active_req = await session.scalar(
        select(PayoutRequest)
        .where(
            PayoutRequest.employee_id == emp.id,
            PayoutRequest.status.in_(("pending", "approved")),
        )
        .order_by(PayoutRequest.created_at.desc(), PayoutRequest.id.desc())
    )
    pending_req = active_req if (active_req and active_req.status == "pending") else None
    card_disp = format_card_display(emp.card_number)
    lines = [
        "💳 <b>Картка і запит на виплату</b>\n",
        f"👛 Поточний залишок до видачі: <b>{fmt_money(summary.balance_due)}</b>",
        f"💳 Картка: <code>{esc(card_disp)}</code>" if card_disp else "💳 Картка: <i>ще не вказано ⚠️</i>",
    ]
    if active_req is not None:
        if active_req.status == "pending":
            lines.append(
                f"\n⏳ Активний запит: <b>{fmt_money(active_req.amount)}</b> — очікує рішення Ані"
            )
        elif active_req.status == "approved":
            lines.append(
                f"\n✅ Запит на <b>{fmt_money(active_req.amount)}</b> прийнято Анею — очікуй переказ!"
            )
    return "\n".join(lines), pending_req


@router.message(StateFilter("*"), F.text == BTN_EMP_PAYOUT_REQ)
async def menu_card_and_payout_req(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    await state.clear()
    assert actor.employee is not None
    today = today_local(settings.tz)
    text, pending_req = await _render_card_and_requests_text(session, actor.employee, today)
    await message.answer(
        text,
        reply_markup=employee_card_and_requests_kb(bool(actor.employee.card_number), pending_req),
    )


@router.callback_query(StateFilter("*"), F.data == "emp:card_menu")
async def cb_card_menu(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    await state.clear()
    assert actor.employee is not None
    today = today_local(settings.tz)
    text, pending_req = await _render_card_and_requests_text(session, actor.employee, today)
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=employee_card_and_requests_kb(bool(actor.employee.card_number), pending_req),
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data == "emp:card_edit")
async def cb_card_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(EmployeeShiftStates.waiting_card_number)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "💳 Введи номер своєї банківської картки (16 цифр, можна з пробілами, наприклад <code>4149 4991 1234 5678</code>):",
            reply_markup=cancel_kb(back_callback="emp:card_menu"),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_card_number)
async def msg_card_number(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    try:
        clean = await save_employee_card(session, actor.employee, message.text)
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="emp:card_menu"),
        )
        return
    await state.clear()
    today = today_local(settings.tz)
    text, pending_req = await _render_card_and_requests_text(session, actor.employee, today)
    await message.answer(
        f"Картку <code>{esc(format_card_display(clean) or clean)}</code> збережено! ✅\n\n" + text,
        reply_markup=employee_card_and_requests_kb(True, pending_req),
    )


@router.callback_query(StateFilter("*"), F.data == "emp:req_new")
async def cb_req_new(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    await state.clear()
    today = today_local(settings.tz)
    if not actor.employee.card_number:
        await state.set_state(EmployeeShiftStates.waiting_payout_req_card)
        if callback.message:
            await safe_edit_text(
                callback.message,
                "💳 Спочатку введи номер картки для отримання переказу (наприклад, <code>4149 4991 1234 5678</code>):",
                reply_markup=cancel_kb(back_callback="emp:card_menu"),
            )
        await callback.answer()
        return

    summary = await get_employee_summary(session, actor.employee, today.replace(day=1), today)
    await state.set_state(EmployeeShiftStates.waiting_payout_req_amount)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"💸 <b>Запит на виплату</b>\n"
            f"Твій поточний залишок: <b>{fmt_money(summary.balance_due)}</b>\n"
            f"Картка: <code>{esc(format_card_display(actor.employee.card_number) or '')}</code>\n\n"
            "Введи суму запиту (у грн) або натисни кнопку нижче:",
            reply_markup=employee_payout_req_amount_kb(summary.balance_due),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_payout_req_card)
async def msg_req_card_first(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    try:
        clean = await save_employee_card(session, actor.employee, message.text)
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="emp:card_menu"),
        )
        return
    today = today_local(settings.tz)
    summary = await get_employee_summary(session, actor.employee, today.replace(day=1), today)
    await state.set_state(EmployeeShiftStates.waiting_payout_req_amount)
    await message.answer(
        f"Картку <code>{esc(format_card_display(clean) or clean)}</code> збережено ✅\n\n"
        f"Твій залишок до видачі: <b>{fmt_money(summary.balance_due)}</b>.\n"
        "Тепер введи суму запиту (у грн) або натисни кнопку нижче:",
        reply_markup=employee_payout_req_amount_kb(summary.balance_due),
    )


@router.callback_query(EmployeeShiftStates.waiting_payout_req_amount, F.data == "emp:req_amt_full")
async def cb_req_amount_full(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    today = today_local(settings.tz)
    summary = await get_employee_summary(session, actor.employee, today.replace(day=1), today)
    if summary.balance_due <= 0:
        await callback.answer("Поточний залишок 0 грн — введи потрібну суму вручну", show_alert=True)
        return
    await state.update_data(req_amount=summary.balance_due)
    await state.set_state(EmployeeShiftStates.waiting_payout_req_comment)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Сума запиту: <b>{fmt_money(summary.balance_due)}</b>.\n"
            "Введи короткий коментар для Ані (наприклад, <code>за тиждень</code>) або надішли <code>-</code> без коментаря:",
            reply_markup=cancel_kb(back_callback="emp:card_menu"),
        )
    await callback.answer()


@router.message(EmployeeShiftStates.waiting_payout_req_amount)
async def msg_req_amount_manual(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    try:
        amount = parse_positive_float(message.text or "", allow_zero=False)
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="emp:card_menu"),
        )
        return
    await state.update_data(req_amount=amount)
    await state.set_state(EmployeeShiftStates.waiting_payout_req_comment)
    await message.answer(
        f"Сума запиту: <b>{fmt_money(amount)}</b>.\n"
        "Введи короткий коментар (наприклад, <code>аванс</code>) або надішли <code>-</code> без коментаря:",
        reply_markup=cancel_kb(back_callback="emp:card_menu"),
    )


@router.message(EmployeeShiftStates.waiting_payout_req_comment)
async def msg_req_comment_finish(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    data = await state.get_data()
    amount = float(data["req_amount"])
    await state.clear()
    try:
        req = await create_payout_request(
            session=session,
            emp=actor.employee,
            amount=amount,
            comment=message.text,
        )
    except ValueError as exc:
        await message.answer(f"Не вдалося створити запит: {esc(exc)} 😊")
        return

    today = today_local(settings.tz)
    summary = await get_employee_summary(session, actor.employee, today.replace(day=1), today)
    await notify_owners_payout_request_created(session, bot, settings, actor.employee, req, summary.balance_due)
    text, pending_req = await _render_card_and_requests_text(session, actor.employee, today)
    await message.answer(
        f"✅ Запит на <b>{fmt_money(req.amount)}</b> надіслано Ані! Щойно вона прийме його, ти отримаєш сповіщення 🌸\n\n" + text,
        reply_markup=employee_card_and_requests_kb(bool(actor.employee.card_number), pending_req),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("emp:req_cancel:"))
async def cb_req_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    assert actor.employee is not None
    await state.clear()
    req_id = int((callback.data or "").split(":")[2])
    try:
        await cancel_payout_request(session, req_id, actor.employee.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    today = today_local(settings.tz)
    text, pending_req = await _render_card_and_requests_text(session, actor.employee, today)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Запит скасовано 👌\n\n" + text,
            reply_markup=employee_card_and_requests_kb(bool(actor.employee.card_number), pending_req),
        )
    await callback.answer("Запит скасовано")
