from __future__ import annotations

import secrets
import string
from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Adjustment, Employee, Role, Shift
from app.filters import IsOwnerOrAdminFilter
from app.handlers.helpers import parse_positive_float, safe_edit_text
from app.keyboards.inline import (
    cancel_kb,
    owner_add_banquet_skip_kb,
    owner_admin_perms_kb,
    owner_delete_confirm_kb,
    owner_emp_shifts_kb,
    owner_employee_card_kb,
    owner_employee_summary_periods_kb,
    owner_relink_confirm_kb,
    owner_roles_kb,
    owner_staff_list_kb,
)
from app.keyboards.reply import (
    BTN_OWN_ADMIN_PERMS,
    BTN_OWN_STAFF,
    admin_main_menu,
    employee_main_menu,
)
from app.services.access import (
    ActorContext,
    get_admin_perms,
    get_all_owner_ids,
    get_connected_admin_employees,
    is_admin_role_name,
    toggle_admin_perm,
)
from app.services.payroll import (
    EmployeePeriodSummary,
    compute_shift_breakdown,
    delete_employee_safely,
    get_employee_summary,
    get_recent_revenues,
    set_employee_banquet_rate,
)
from app.states import OwnerStaffStates
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

router = Router(name="owner_staff")
router.message.filter(IsOwnerOrAdminFilter())
router.callback_query.filter(IsOwnerOrAdminFilter())

_INVITE_ALPHABET = string.ascii_letters + string.digits
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_EMP_SHIFTS_PAGE_SIZE = 10


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


def _generate_invite_code(length: int = 10) -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(length))


async def _get_or_create_role(session: AsyncSession, name: str) -> Role:
    cleaned = name.strip()
    existing = await session.scalar(select(Role).where(Role.name.ilike(cleaned)))
    if existing is not None:
        return existing
    role = Role(name=cleaned)
    session.add(role)
    await session.flush()
    return role


async def _ensure_invite_link(
    session: AsyncSession,
    bot: Bot,
    emp: Employee,
) -> str | None:
    if emp.telegram_id is not None:
        return None
    if not emp.invite_code:
        emp.invite_code = _generate_invite_code()
        await session.commit()
        await session.refresh(emp)
    me = await bot.me()
    if not me.username:
        return None
    return f"https://t.me/{me.username}?start={emp.invite_code}"


async def _render_employee_card_text(
    session: AsyncSession,
    emp: Employee,
    today: date,
    invite_link: str | None = None,
    viewer_actor: ActorContext | None = None,
) -> str:
    summary = await get_employee_summary(session, emp, today.replace(day=1), today)
    role_name = emp.role.name if emp.role else "—"
    if emp.telegram_id:
        tg_status = f"🟢 Підключено{f' (@{esc(emp.telegram_username)})' if emp.telegram_username else ' (успішно)'}"
    else:
        tg_status = "⏳ Очікує переходу за посиланням"

    can_view_salaries = viewer_actor.has_perm("can_view_salaries_reports") if viewer_actor else True
    can_manage = viewer_actor.has_perm("can_manage_staff") if viewer_actor else True
    target_is_admin = bool(emp.role and is_admin_role_name(emp.role.name))
    can_show_invite = can_manage and ((viewer_actor.is_owner if viewer_actor else True) or not target_is_admin)

    lines = [
        f"👤 <b>{esc(emp.full_name)}</b>",
        f"🏷 Посада: <b>{esc(role_name)}</b>",
        f"📱 Статус: {tg_status}",
    ]

    if can_view_salaries:
        banquet_txt = (
            f"<b>{fmt_money(emp.banquet_rate_per_person)} / люд.</b>"
            if emp.banquet_rate_per_person is not None
            else "<b>Не вказано ⚠️</b>"
        )
        lines.extend([
            "",
            f"💵 Ставка за годину: <b>{fmt_money(emp.hourly_rate)}</b>",
            f"📈 Відсоток від каси: <b>{fmt_percent(emp.revenue_percent)}</b>",
            f"🎉 Тариф банкету: {banquet_txt}",
            "",
            f"📅 За поточний місяць: <b>{summary.shifts_count}</b> змін ({fmt_hours(summary.total_hours)})",
            f"👛 Поточний залишок до видачі: <b>{fmt_money(summary.balance_due)}</b>",
        ])
    else:
        lines.extend([
            "",
            f"📅 За поточний місяць: <b>{summary.shifts_count}</b> змін ({fmt_hours(summary.total_hours)})",
        ])

    if emp.telegram_id is None and invite_link and can_show_invite:
        lines.extend([
            "",
            "🔗 <b>Посилання-запрошення (можна одразу переслати працівнику):</b>",
            invite_link,
        ])
    return "\n".join(lines)


async def _build_employee_card_view(
    session: AsyncSession,
    bot: Bot,
    emp: Employee,
    settings: Settings,
    prefix: str = "",
    actor: ActorContext | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    today = today_local(settings.tz)
    can_manage = actor.has_perm("can_manage_staff") if actor else True
    target_is_admin = bool(emp.role and is_admin_role_name(emp.role.name))
    can_link = can_manage and ((actor.is_owner if actor else True) or not target_is_admin)
    invite_link = await _ensure_invite_link(session, bot, emp) if can_link else None
    card_text = await _render_employee_card_text(
        session,
        emp,
        today,
        invite_link=invite_link,
        viewer_actor=actor,
    )
    text = f"{prefix}\n\n{card_text}" if prefix else card_text
    kb = owner_employee_card_kb(emp, invite_link=invite_link, actor=actor)
    return text, kb


def _render_owner_employee_summary_text(summary: EmployeePeriodSummary, period_title: str) -> str:
    emp = summary.employee
    role_name = emp.role.name if emp.role else "—"
    lines = [
        f"📊 <b>Звіт: {esc(emp.full_name)}</b> ({esc(role_name)})",
        f"🗓 Період: <b>{esc(period_title)}</b>",
        "",
        f"🕒 Змін: <b>{summary.shifts_count}</b> ({fmt_hours(summary.total_hours)})",
        f"💵 За годинами: <b>{fmt_money(summary.base_pay)}</b>",
        f"📈 Відсотки від каси: <b>{fmt_money(summary.percent_pay)}</b>",
        f"🎉 Банкети: <b>{fmt_money(summary.banquet_pay)}</b>",
    ]
    if summary.bonuses > 0:
        lines.append(f"➕ Премії: <b>{fmt_money(summary.bonuses)}</b>")
    if summary.fines > 0:
        lines.append(f"➖ Штрафи: <b>{fmt_money(summary.fines)}</b>")

    lines.extend([
        "",
        f"✨ Нараховано за період: <b>{fmt_money(summary.earned_period)}</b>",
        f"💸 Виплачено за період: <b>{fmt_money(summary.paid_period)}</b>",
        f"👛 Загальний залишок до видачі: <b>{fmt_money(summary.balance_due)}</b>",
    ])

    if summary.shift_breakdowns:
        lines.append("")
        lines.append("<b>Зміни за період:</b>")
        for b in summary.shift_breakdowns[:15]:
            s = b.shift
            extra = ""
            if s.is_banquet:
                if b.missing_banquet_rate or b.missing_banquet_guests:
                    extra = " | 🎉 банкет (неповні дані ⚠️)"
                else:
                    extra = f" | 🎉 {s.banquet_guests} люд."
            lines.append(
                f"• {format_date_with_day(s.work_date)}: {s.start_time}-{s.end_time} "
                f"({fmt_hours(s.hours)}) → <b>{fmt_money(b.total_pay)}</b>{extra}"
            )
        if len(summary.shift_breakdowns) > 15:
            lines.append(f"<i>...та ще {len(summary.shift_breakdowns) - 15} змін (дивись у Excel)</i>")

    return "\n".join(lines)


async def _show_staff_list(
    target_msg: Message,
    session: AsyncSession,
    actor: ActorContext,
    edit_in_place: bool,
) -> None:
    employees = (
        await session.scalars(
            select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.full_name.asc())
        )
    ).all()
    if not employees:
        text = "👥 <b>Працівники</b>\nПоки що список порожній. Додай першого працівника кнопкою нижче 🌸"
    else:
        text = (
            f"👥 <b>Працівники ({len(employees)})</b>\n"
            "Натисни на працівника, щоб відкрити картку:"
        )
    kb = owner_staff_list_kb(list(employees), can_add=actor.has_perm("can_manage_staff"))
    if edit_in_place:
        await safe_edit_text(target_msg, text, reply_markup=kb)
    else:
        await target_msg.answer(text, reply_markup=kb)


# ----------------- ПРАВА АДМІНІСТРАТОРА (ТІЛЬКИ ВЛАСНИЦЯ) -----------------

def _admin_perms_header_text() -> str:
    return (
        "⚙️ <b>Права доступу для ролі «Адмін»</b>\n"
        "Обери, які вкладки та дії бачитиме співробітник із посадою <b>Адмін</b> "
        "(окрім його власних змін та зарплати):\n"
        "<i>Натисни на пункт, щоб увімкнути ✅ або вимкнути ❌</i>"
    )


@router.message(StateFilter("*"), F.text == BTN_OWN_ADMIN_PERMS)
async def menu_admin_perms(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.is_owner:
        await _deny_msg(message, actor)
        return
    await state.clear()
    perms = await get_admin_perms(session)
    await message.answer(_admin_perms_header_text(), reply_markup=owner_admin_perms_kb(perms))


@router.callback_query(StateFilter("*"), F.data.startswith("own:adm_toggle:"))
async def cb_admin_perm_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    actor: ActorContext,
) -> None:
    if not actor.is_owner:
        await _deny_cb(callback)
        return
    field_name = (callback.data or "").split(":")[2]
    try:
        perms = await toggle_admin_perm(session, field_name)
    except ValueError:
        await callback.answer("Невідоме налаштування", show_alert=True)
        return

    if callback.message:
        await safe_edit_text(
            callback.message,
            _admin_perms_header_text(),
            reply_markup=owner_admin_perms_kb(perms),
        )

    admins = await get_connected_admin_employees(session)
    for adm in admins:
        if adm.telegram_id:
            try:
                await bot.send_message(
                    adm.telegram_id,
                    "🔔 <b>Аня оновила права доступу для ролі Адмін</b> — меню внизу оновлено ✨",
                    reply_markup=admin_main_menu(perms),
                )
            except Exception:
                pass

    await callback.answer("Права оновлено ✅")


# ----------------- СПИСОК ТА КАРТКА ПРАЦІВНИКА -----------------

@router.message(StateFilter("*"), F.text == BTN_OWN_STAFF)
async def menu_staff(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not (actor.is_owner or actor.admin_perms.can_open_staff_tab):
        await _deny_msg(message, actor)
        return
    await state.clear()
    await _show_staff_list(message, session, actor, edit_in_place=False)


@router.callback_query(StateFilter("*"), F.data == "own:staff_list")
async def cb_staff_list(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not (actor.is_owner or actor.admin_perms.can_open_staff_tab):
        await _deny_cb(callback)
        return
    await state.clear()
    if callback.message:
        await _show_staff_list(callback.message, session, actor, edit_in_place=True)
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp:"))
async def cb_employee_card(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not (actor.is_owner or actor.admin_perms.can_open_staff_tab):
        await _deny_cb(callback)
        return
    await state.clear()
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return

    text, kb = await _build_employee_card_view(session, bot, emp, settings, actor=actor)
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb,
            link_preview_options=_NO_PREVIEW,
        )
    await callback.answer()


# ----------------- ЗМІНИ ПРАЦІВНИКА (ПЕРЕГЛЯД ДЛЯ ВЛАСНИЦІ / АДМІНА) -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_shifts:"))
async def cb_owner_emp_shifts(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not (
        actor.has_perm("can_view_staff_shifts")
        or actor.has_perm("can_edit_hours")
        or actor.has_perm("can_view_salaries_reports")
    ):
        await _deny_cb(callback)
        return

    parts = (callback.data or "").split(":")
    emp_id, page = int(parts[2]), int(parts[3])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return

    await state.clear()
    offset = page * _EMP_SHIFTS_PAGE_SIZE
    rows = (
        await session.scalars(
            select(Shift)
            .where(Shift.employee_id == emp.id)
            .order_by(Shift.work_date.desc())
            .offset(offset)
            .limit(_EMP_SHIFTS_PAGE_SIZE + 1)
        )
    ).all()
    has_next = len(rows) > _EMP_SHIFTS_PAGE_SIZE
    shifts = list(rows[:_EMP_SHIFTS_PAGE_SIZE])

    can_view_money = actor.has_perm("can_view_salaries_reports")
    rev_map = (
        await get_recent_revenues(session, [s.work_date for s in shifts])
        if can_view_money and shifts
        else {}
    )

    lines = [f"📋 <b>Зміни: {esc(emp.full_name)}</b>", ""]
    if not shifts:
        lines.append("Змін поки що немає 😊")
    else:
        for s in shifts:
            extra = ""
            if s.is_banquet:
                guests_part = f"{s.banquet_guests} люд." if s.banquet_guests is not None else "люди не вказані ⚠️"
                extra = f" | 🎉 {guests_part}"
            pay_part = ""
            if can_view_money:
                bd = compute_shift_breakdown(s, rev_map)
                pay_part = f" → <b>{fmt_money(bd.total_pay)}</b>"
            lines.append(
                f"• {format_date_with_day(s.work_date)}: <b>{s.start_time}-{s.end_time}</b> "
                f"({fmt_hours(s.hours)}){pay_part}{extra}"
            )

    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines),
            reply_markup=owner_emp_shifts_kb(emp.id, page, has_next),
        )
    await callback.answer()


# ----------------- ДОДАВАННЯ ПРАЦІВНИКА -----------------

async def _emp_cancel_kb(state: FSMContext) -> InlineKeyboardMarkup:
    emp_id = (await state.get_data()).get("target_emp_id")
    return cancel_kb(back_callback=f"own:emp:{emp_id}") if emp_id else cancel_kb()


@router.callback_query(StateFilter("*"), F.data == "own:emp_add")
async def cb_add_emp_start(
    callback: CallbackQuery,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    await state.clear()
    await state.set_state(OwnerStaffStates.add_name)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "➕ <b>Новий працівник</b>\nВведи ім'я (наприклад, <code>Олена Коваленко</code>):",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.add_name)
async def msg_add_emp_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    full_name = (message.text or "").strip()
    if len(full_name) < 2 or len(full_name) > 100:
        await message.answer(
            "Введи коректне ім'я (від 2 до 100 символів) 😊",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
        return

    await state.update_data(full_name=full_name)
    await state.set_state(OwnerStaffStates.add_role_pick)
    roles = list((await session.scalars(select(Role).order_by(Role.id.asc()))).all())
    await message.answer(
        f"Ім'я: <b>{esc(full_name)}</b> 🌸\nОбери посаду:",
        reply_markup=owner_roles_kb(roles, mode="add"),
    )


@router.callback_query(OwnerStaffStates.add_role_pick, F.data == "own:emp_add_role_new")
async def cb_add_emp_role_new(
    callback: CallbackQuery,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_cb(callback)
        return
    await state.set_state(OwnerStaffStates.add_role_custom)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Введи назву нової посади (наприклад, <code>Офіціант</code> або <code>Касир</code>):",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.add_role_custom)
async def msg_add_emp_role_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    role_name = (message.text or "").strip()
    if len(role_name) < 2 or len(role_name) > 60:
        await message.answer(
            "Назва посади має бути від 2 до 60 символів 😊",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
        return
    if not actor.is_owner and is_admin_role_name(role_name):
        await message.answer(
            "Тільки Аня може призначати роль Адміна 🔒",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
        return
    role = await _get_or_create_role(session, role_name)
    await session.commit()
    await state.update_data(role_id=role.id, role_name=role.name)
    await state.set_state(OwnerStaffStates.add_hourly_rate)
    await message.answer(
        f"Посада: <b>{esc(role.name)}</b> 🏷\nВведи ставку за годину (у грн, наприклад <code>120</code> або <code>0</code>):",
        reply_markup=cancel_kb(back_callback="own:staff_list"),
    )


@router.callback_query(OwnerStaffStates.add_role_pick, F.data.startswith("own:emp_add_role:"))
async def cb_add_emp_role_pick(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_cb(callback)
        return
    role_id = int((callback.data or "").split(":")[2])
    role = await session.get(Role, role_id)
    if role is None:
        await callback.answer("Посаду не знайдено", show_alert=True)
        return
    if not actor.is_owner and is_admin_role_name(role.name):
        await callback.answer("Тільки Аня може призначати роль Адміна 🔒", show_alert=True)
        return
    await state.update_data(role_id=role.id, role_name=role.name)
    await state.set_state(OwnerStaffStates.add_hourly_rate)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Посада: <b>{esc(role.name)}</b> 🏷\nВведи ставку за годину (у грн, наприклад <code>120</code> або <code>0</code>):",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.add_hourly_rate)
async def msg_add_emp_hourly(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        rate = parse_positive_float(message.text or "", allow_zero=True)
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
        return

    await state.update_data(hourly_rate=rate)
    await state.set_state(OwnerStaffStates.add_percent)
    await message.answer(
        f"Ставка: <b>{fmt_money(rate)}/год</b> 💵\n"
        "Введи <b>відсоток від каси дня</b> (наприклад, <code>3</code>, <code>2.5</code> або <code>0</code>):",
        reply_markup=cancel_kb(back_callback="own:staff_list"),
    )


@router.message(OwnerStaffStates.add_percent)
async def msg_add_emp_percent(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        pct = parse_positive_float(message.text or "", allow_zero=True)
    except ValueError as exc:
        await message.answer(
            f"Ой: {esc(exc)} 😊",
            reply_markup=cancel_kb(back_callback="own:staff_list"),
        )
        return

    await state.update_data(revenue_percent=pct)
    await state.set_state(OwnerStaffStates.add_banquet_rate)
    await message.answer(
        f"Відсоток від каси: <b>{fmt_percent(pct)}</b> 📈\n"
        "Введи <b>тариф за 1 людину на банкеті</b> (у грн, наприклад <code>50</code>), "
        "або пропусти цей крок, якщо вкажеш пізніше:",
        reply_markup=owner_add_banquet_skip_kb(),
    )


async def _create_employee_finish(
    target_msg: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    banquet_rate: float | None,
    edit_in_place: bool,
) -> None:
    data = await state.get_data()
    await state.clear()

    invite_code = _generate_invite_code()
    emp = Employee(
        full_name=data["full_name"],
        role_id=int(data["role_id"]),
        hourly_rate=float(data["hourly_rate"]),
        revenue_percent=float(data["revenue_percent"]),
        banquet_rate_per_person=banquet_rate,
        invite_code=invite_code,
        is_active=True,
    )
    session.add(emp)
    await session.commit()
    await session.refresh(emp)

    text, kb = await _build_employee_card_view(
        session,
        bot,
        emp,
        settings,
        prefix="Працівника створено! 🎉 Надішли йому персональне посилання нижче:",
        actor=actor,
    )
    if edit_in_place:
        await safe_edit_text(
            target_msg,
            text,
            reply_markup=kb,
            link_preview_options=_NO_PREVIEW,
        )
    else:
        await target_msg.answer(
            text,
            reply_markup=kb,
            link_preview_options=_NO_PREVIEW,
        )


@router.callback_query(OwnerStaffStates.add_banquet_rate, F.data == "own:emp_add_banquet_skip")
async def cb_add_emp_banquet_skip(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_cb(callback)
        return
    if callback.message:
        await _create_employee_finish(
            callback.message, state, session, bot, settings, actor, banquet_rate=None, edit_in_place=True
        )
    await callback.answer()


@router.message(OwnerStaffStates.add_banquet_rate)
async def msg_add_emp_banquet_rate(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        rate = parse_positive_float(message.text or "", allow_zero=False)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=owner_add_banquet_skip_kb())
        return
    await _create_employee_finish(
        message, state, session, bot, settings, actor, banquet_rate=rate, edit_in_place=False
    )


# ----------------- ДОПОМІЖНІ СПОВІЩЕННЯ ПРАЦІВНИКАМ -----------------

async def _notify_emp(
    bot: Bot,
    emp: Employee,
    text: str,
    reply_markup: ReplyKeyboardMarkup | None = None,
) -> None:
    if not emp.telegram_id:
        return
    try:
        await bot.send_message(emp.telegram_id, text, reply_markup=reply_markup)
    except Exception:
        pass


# ----------------- РЕДАГУВАННЯ СТАВКИ, %, БАНКЕТУ, ПОСАДИ -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_rate:"))
async def cb_edit_rate_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp.id)
    await state.set_state(OwnerStaffStates.edit_hourly_rate)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Поточна ставка для <b>{esc(emp.full_name)}</b>: <b>{fmt_money(emp.hourly_rate)}/год</b>.\n"
            "Введи нову ставку за годину (минулі зміни збережуть стару ставку):",
            reply_markup=cancel_kb(f"own:emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.edit_hourly_rate)
async def msg_edit_rate_val(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        val = parse_positive_float(message.text or "", allow_zero=True)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return
    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    await state.clear()
    if emp is None or not emp.is_active:
        await message.answer("Працівника не знайдено")
        return
    emp.hourly_rate = val
    await session.commit()
    await session.refresh(emp)

    await _notify_emp(
        bot,
        emp,
        f"💵 <b>{esc(actor.actor_label)}</b> змінив(ла) твою ставку за годину: "
        f"тепер <b>{fmt_money(val)}/год</b> ✨\n"
        "<i>(Для вже відпрацьованих змін збереглася попередня ставка.)</i>",
    )
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"змінив(ла) ставку працівнику <b>{esc(emp.full_name)}</b> → <b>{fmt_money(val)}/год</b>",
    )

    text, kb = await _build_employee_card_view(
        session, bot, emp, settings, prefix="Ставку оновлено ✨", actor=actor
    )
    await message.answer(text, reply_markup=kb, link_preview_options=_NO_PREVIEW)


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_pct:"))
async def cb_edit_pct_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp.id)
    await state.set_state(OwnerStaffStates.edit_percent)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Поточний відсоток для <b>{esc(emp.full_name)}</b>: <b>{fmt_percent(emp.revenue_percent)}</b>.\n"
            "Введи новий % від каси дня (наприклад, <code>3.5</code> або <code>0</code>):",
            reply_markup=cancel_kb(f"own:emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.edit_percent)
async def msg_edit_pct_val(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        val = parse_positive_float(message.text or "", allow_zero=True)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return
    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    await state.clear()
    if emp is None or not emp.is_active:
        await message.answer("Працівника не знайдено")
        return
    emp.revenue_percent = val
    await session.commit()
    await session.refresh(emp)

    await _notify_emp(
        bot,
        emp,
        f"📈 <b>{esc(actor.actor_label)}</b> змінив(ла) твій відсоток від каси: "
        f"тепер <b>{fmt_percent(val)}</b> ✨",
    )
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"змінив(ла) відсоток від каси працівнику <b>{esc(emp.full_name)}</b> → <b>{fmt_percent(val)}</b>",
    )

    text, kb = await _build_employee_card_view(
        session, bot, emp, settings, prefix="Відсоток оновлено ✨", actor=actor
    )
    await message.answer(text, reply_markup=kb, link_preview_options=_NO_PREVIEW)


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_banquet:"))
async def cb_edit_banquet_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp.id)
    await state.set_state(OwnerStaffStates.edit_banquet_rate)
    cur = fmt_money(emp.banquet_rate_per_person) + "/люд." if emp.banquet_rate_per_person is not None else "не вказано"
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Тариф банкету для <b>{esc(emp.full_name)}</b> (зараз: <b>{cur}</b>).\n"
            "Введи суму в грн за <b>1 людину</b> на банкеті (наприклад, <code>50</code>):",
            reply_markup=cancel_kb(f"own:emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.edit_banquet_rate)
async def msg_edit_banquet_val(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        val = parse_positive_float(message.text or "", allow_zero=False)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return
    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    await state.clear()
    if emp is None or not emp.is_active:
        await message.answer("Працівника не знайдено")
        return

    updated_dates = await set_employee_banquet_rate(session, emp, val)
    extra = ""
    if updated_dates:
        dates_str = ", ".join(format_date_short(d) for d in updated_dates)
        extra = f"\n🎉 Автоматично перераховано банкети за: {dates_str}"
        emp_msg = (
            f"🎉 <b>{esc(actor.actor_label)}</b> додав(ла) твій банкетний тариф "
            f"(<b>{fmt_money(val)}/люд.</b>) — банкети за {dates_str} вже пораховано ✨"
        )
    else:
        emp_msg = (
            f"🎉 <b>{esc(actor.actor_label)}</b> оновив(ла) твій банкетний тариф: "
            f"<b>{fmt_money(val)}/люд.</b> ✨"
        )
    await _notify_emp(bot, emp, emp_msg)
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"оновив(ла) банкетний тариф працівнику <b>{esc(emp.full_name)}</b> → <b>{fmt_money(val)}/люд.</b>",
    )

    text, kb = await _build_employee_card_view(
        session,
        bot,
        emp,
        settings,
        prefix=f"Тариф банкету оновлено!{extra}",
        actor=actor,
    )
    await message.answer(text, reply_markup=kb, link_preview_options=_NO_PREVIEW)


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_role:"))
async def cb_edit_role_pick(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await callback.answer("Тільки Аня може змінювати роль Адміна 🔒", show_alert=True)
        return
    await state.clear()
    roles = list((await session.scalars(select(Role).order_by(Role.id.asc()))).all())
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Обери нову посаду для <b>{esc(emp.full_name)}</b>:",
            reply_markup=owner_roles_kb(roles, mode="edit", emp_id=emp.id),
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_set_role_new:"))
async def cb_edit_role_new(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await callback.answer("Тільки Аня може змінювати роль Адміна 🔒", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp_id)
    await state.set_state(OwnerStaffStates.edit_role_custom)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "Введи нову назву посади:",
            reply_markup=cancel_kb(f"own:emp:{emp_id}"),
        )
    await callback.answer()


async def _apply_role_change_and_notify(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
    emp: Employee,
    role: Role,
) -> None:
    was_admin = bool(emp.role and is_admin_role_name(emp.role.name))
    is_now_admin = is_admin_role_name(role.name)

    emp.role_id = role.id
    emp.role = role
    await session.commit()
    await session.refresh(emp, attribute_names=["role"])

    if emp.telegram_id:
        if is_now_admin and not was_admin:
            perms = await get_admin_perms(session)
            await _notify_emp(
                bot,
                emp,
                f"👑 <b>{esc(actor.actor_label)}</b> призначив(ла) тобі посаду <b>{esc(role.name)}</b>!\n"
                "Інструменти адміністратора вже відкрито в меню внизу ✨",
                reply_markup=admin_main_menu(perms),
            )
        elif was_admin and not is_now_admin:
            await _notify_emp(
                bot,
                emp,
                f"🏷 <b>{esc(actor.actor_label)}</b> змінив(ла) твою посаду на <b>{esc(role.name)}</b>.\n"
                "Меню внизу оновлено ✨",
                reply_markup=employee_main_menu(),
            )
        else:
            await _notify_emp(
                bot,
                emp,
                f"🏷 <b>{esc(actor.actor_label)}</b> змінив(ла) твою посаду на <b>{esc(role.name)}</b> ✨",
            )

    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"змінив(ла) посаду працівнику <b>{esc(emp.full_name)}</b> → <b>{esc(role.name)}</b>",
    )


@router.message(OwnerStaffStates.edit_role_custom)
async def msg_edit_role_custom(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    role_name = (message.text or "").strip()
    if len(role_name) < 2 or len(role_name) > 60:
        await message.answer("Назва посади має бути від 2 до 60 символів 😊", reply_markup=await _emp_cancel_kb(state))
        return
    if not actor.is_owner and is_admin_role_name(role_name):
        await message.answer("Тільки Аня може призначати роль Адміна 🔒", reply_markup=await _emp_cancel_kb(state))
        return
    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    await state.clear()
    if emp is None or not emp.is_active:
        await message.answer("Працівника не знайдено")
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await _deny_msg(message, actor)
        return

    role = await _get_or_create_role(session, role_name)
    await _apply_role_change_and_notify(session, bot, settings, actor, emp, role)
    text, kb = await _build_employee_card_view(
        session, bot, emp, settings, prefix="Посаду оновлено ✨", actor=actor
    )
    await message.answer(text, reply_markup=kb, link_preview_options=_NO_PREVIEW)


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_set_role:"))
async def cb_edit_role_apply(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await state.clear()
        await _deny_cb(callback)
        return
    parts = (callback.data or "").split(":")
    emp_id, role_id = int(parts[2]), int(parts[3])
    emp = await session.get(Employee, emp_id)
    role = await session.get(Role, role_id)
    if emp is None or not emp.is_active or role is None:
        await state.clear()
        await callback.answer("Дані не знайдено", show_alert=True)
        return
    if not actor.is_owner and (
        (emp.role and is_admin_role_name(emp.role.name)) or is_admin_role_name(role.name)
    ):
        await state.clear()
        await callback.answer("Тільки Аня може призначати або змінювати роль Адміна 🔒", show_alert=True)
        return
    await state.clear()
    await _apply_role_change_and_notify(session, bot, settings, actor, emp, role)
    text, kb = await _build_employee_card_view(
        session, bot, emp, settings, prefix="Посаду оновлено ✨", actor=actor
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb,
            link_preview_options=_NO_PREVIEW,
        )
    await callback.answer()


# ----------------- ЗМІНА ГОДИН ПРАЦІВНИКА ВЛАСНИЦЕЮ / АДМІНОМ -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_hours:"))
async def cb_owner_edit_hours_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_edit_hours"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp.id)
    await state.set_state(OwnerStaffStates.edit_emp_hours_date)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"🕒 <b>Зміна годин для {esc(emp.full_name)}</b>\n"
            "Введи дату зміни у форматі <b>ДД.ММ.РРРР</b> (наприклад, <code>12.11.2026</code>):",
            reply_markup=cancel_kb(f"own:emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.edit_emp_hours_date)
async def msg_owner_edit_hours_date(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_edit_hours"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        work_date = parse_date_input(message.text or "")
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return

    data = await state.get_data()
    emp_id = int(data["target_emp_id"])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await state.clear()
        await message.answer("Працівника не знайдено")
        return

    shift = await session.scalar(
        select(Shift).where(Shift.employee_id == emp.id, Shift.work_date == work_date)
    )
    await state.update_data(work_date=work_date.isoformat())
    await state.set_state(OwnerStaffStates.edit_emp_hours_time)

    if shift is not None:
        info = f"Зараз внесено: <code>{shift.start_time}-{shift.end_time}</code> ({fmt_hours(shift.hours)})."
    else:
        info = "За цей день зміни ще не було — можеш додати години прямо зараз."

    await message.answer(
        f"Дата: <b>{format_date_with_day(work_date, full=True)}</b> 📅\n"
        f"{info}\nВведи новий час у форматі <b>ГГ:ХХ-ГГ:ХХ</b>:",
        reply_markup=cancel_kb(f"own:emp:{emp.id}"),
    )


@router.message(OwnerStaffStates.edit_emp_hours_time)
async def msg_owner_edit_hours_time(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_edit_hours"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        start_time, end_time, hours = parse_time_range(message.text or "")
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return

    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    work_date = date.fromisoformat(data["work_date"])
    await state.clear()
    if emp is None or not emp.is_active:
        await message.answer("Працівника не знайдено")
        return

    shift = await session.scalar(
        select(Shift).where(Shift.employee_id == emp.id, Shift.work_date == work_date)
    )
    old_range: str | None = None
    if shift is None:
        shift = Shift(
            employee_id=emp.id,
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            hours=hours,
            hourly_rate_snapshot=emp.hourly_rate,
            percent_snapshot=emp.revenue_percent,
            is_banquet=False,
        )
        session.add(shift)
        action_word = "додано"
        emp_msg = (
            f"🕒 <b>{esc(actor.actor_label)}</b> додав(ла) тобі зміну за "
            f"<b>{format_date_with_day(work_date)}</b>: "
            f"<b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ✨"
        )
    else:
        old_range = f"{shift.start_time}-{shift.end_time}"
        shift.start_time = start_time
        shift.end_time = end_time
        shift.hours = hours
        action_word = "оновлено"
        emp_msg = (
            f"🕒 <b>{esc(actor.actor_label)}</b> змінив(ла) твої години за "
            f"<b>{format_date_with_day(work_date)}</b>:\n"
            f"Було: <code>{old_range}</code> → Стало: <b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ✨"
        )

    await session.commit()
    await _notify_emp(bot, emp, emp_msg)
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"{action_word} години працівнику <b>{esc(emp.full_name)}</b> за "
        f"{format_date_with_day(work_date)}: <b>{start_time}-{end_time}</b> ({fmt_hours(hours)})",
    )

    prefix = (
        f"Години за <b>{format_date_with_day(work_date)}</b> {action_word}: "
        f"<b>{start_time}-{end_time}</b> ({fmt_hours(hours)}) ✨"
    )
    text, kb = await _build_employee_card_view(
        session, bot, emp, settings, prefix=prefix, actor=actor
    )
    await message.answer(text, reply_markup=kb, link_preview_options=_NO_PREVIEW)


# ----------------- ПРЕМІЇ ТА ШТРАФИ -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_bonus:"))
async def cb_bonus_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_adjust_money"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp.id, adj_kind="bonus")
    await state.set_state(OwnerStaffStates.add_bonus_amount)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"➕ <b>Премія для {esc(emp.full_name)}</b>\nВведи суму премії (у грн):",
            reply_markup=cancel_kb(f"own:emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.add_bonus_amount)
async def msg_bonus_amount(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_adjust_money"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        amount = parse_positive_float(message.text or "", allow_zero=False)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return
    await state.update_data(adj_amount=amount)
    await state.set_state(OwnerStaffStates.add_bonus_comment)
    await message.answer(
        f"Сума премії: <b>{fmt_money(amount)}</b>.\nВведи короткий коментар (або <code>-</code>, якщо без коментаря):",
        reply_markup=await _emp_cancel_kb(state),
    )


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_fine:"))
async def cb_fine_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_adjust_money"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_emp_id=emp.id, adj_kind="fine")
    await state.set_state(OwnerStaffStates.add_fine_amount)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"➖ <b>Штраф для {esc(emp.full_name)}</b>\nВведи суму штрафу (у грн):",
            reply_markup=cancel_kb(f"own:emp:{emp.id}"),
        )
    await callback.answer()


@router.message(OwnerStaffStates.add_fine_amount)
async def msg_fine_amount(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_adjust_money"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    try:
        amount = parse_positive_float(message.text or "", allow_zero=False)
    except ValueError as exc:
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return
    await state.update_data(adj_amount=amount)
    await state.set_state(OwnerStaffStates.add_fine_comment)
    await message.answer(
        f"Сума штрафу: <b>{fmt_money(amount)}</b>.\nВведи причину (або <code>-</code>, якщо без коментаря):",
        reply_markup=await _emp_cancel_kb(state),
    )


@router.message(StateFilter(OwnerStaffStates.add_bonus_comment, OwnerStaffStates.add_fine_comment))
async def msg_adjustment_comment(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_adjust_money"):
        await state.clear()
        await _deny_msg(message, actor)
        return
    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    kind: str = data["adj_kind"]
    amount = float(data["adj_amount"])
    raw_comment = (message.text or "").strip()
    comment = None if raw_comment in {"", "-", "—"} else raw_comment[:240]
    await state.clear()

    if emp is None or not emp.is_active:
        await message.answer("Працівника не знайдено")
        return

    today = today_local(settings.tz)
    adj = Adjustment(
        employee_id=emp.id,
        adj_date=today,
        kind=kind,
        amount=amount,
        comment=comment,
    )
    session.add(adj)
    await session.commit()

    new_summary = await get_employee_summary(session, emp, today.replace(day=1), today)
    if kind == "bonus":
        comment_str = f"\nКоментар: <i>{esc(comment)}</i>" if comment else ""
        emp_msg = (
            f"🎁 <b>{esc(actor.actor_label)}</b> нарахував(ла) тобі премію: "
            f"<b>+{fmt_money(amount)}</b>!{comment_str}\n"
            f"👛 Поточний залишок до видачі: <b>{fmt_money(new_summary.balance_due)}</b> 🌸"
        )
        adm_word = f"додав(ла) премію <b>+{fmt_money(amount)}</b>"
    else:
        comment_str = f"\nПричина: <i>{esc(comment)}</i>" if comment else ""
        emp_msg = (
            f"📌 <b>{esc(actor.actor_label)}</b> зафіксував(ла) штраф: "
            f"<b>−{fmt_money(amount)}</b>.{comment_str}\n"
            f"👛 Поточний залишок до видачі: <b>{fmt_money(new_summary.balance_due)}</b>"
        )
        adm_word = f"зафіксував(ла) штраф <b>−{fmt_money(amount)}</b>"

    await _notify_emp(bot, emp, emp_msg)
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"{adm_word} працівнику <b>{esc(emp.full_name)}</b>"
        + (f" ({esc(comment)})" if comment else ""),
    )

    label = "Премію додано 🎁" if kind == "bonus" else "Штраф зафіксовано 📌"
    text, kb = await _build_employee_card_view(
        session, bot, emp, settings, prefix=f"{label} ({fmt_money(amount)})", actor=actor
    )
    await message.answer(text, reply_markup=kb, link_preview_options=_NO_PREVIEW)


# ----------------- ІНДИВІДУАЛЬНИЙ ЗВІТ ПО ПРАЦІВНИКУ -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_summary:"))
async def cb_emp_summary(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_view_salaries_reports"):
        await _deny_cb(callback)
        return
    parts = (callback.data or "").split(":")
    emp_id, preset = int(parts[2]), parts[3]
    emp = await session.get(Employee, emp_id)
    if emp is None:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return

    await state.clear()
    if preset == "custom":
        await state.update_data(target_emp_id=emp.id)
        await state.set_state(OwnerStaffStates.emp_summary_custom_period)
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"Звіт по <b>{esc(emp.full_name)}</b> 📊\n"
                "Введи дату (<code>12.11.2026</code>) або період (<code>01.11.2026-12.11.2026</code>):",
                reply_markup=cancel_kb(f"own:emp:{emp.id}"),
            )
        await callback.answer()
        return

    today = today_local(settings.tz)
    d1, d2, label = get_period_bounds(preset, today)
    summary = await get_employee_summary(session, emp, d1, d2)
    if callback.message:
        await safe_edit_text(
            callback.message,
            _render_owner_employee_summary_text(summary, label),
            reply_markup=owner_employee_summary_periods_kb(emp.id),
        )
    await callback.answer()


@router.message(OwnerStaffStates.emp_summary_custom_period)
async def msg_emp_summary_custom(
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
        await message.answer(f"Ой: {esc(exc)} 😊", reply_markup=await _emp_cancel_kb(state))
        return

    data = await state.get_data()
    emp = await session.get(Employee, int(data["target_emp_id"]))
    await state.clear()
    if emp is None:
        await message.answer("Працівника не знайдено")
        return

    label = format_date_with_day(d1) if d1 == d2 else f"{format_date_short(d1)}–{format_date_short(d2)}"
    summary = await get_employee_summary(session, emp, d1, d2)
    await message.answer(
        _render_owner_employee_summary_text(summary, label),
        reply_markup=owner_employee_summary_periods_kb(emp.id),
    )


# ----------------- ПОСИЛАННЯ-ЗАПРОШЕННЯ, ПЕРЕПРИВ'ЯЗКА ТА ВИДАЛЕННЯ -----------------

@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_link:"))
async def cb_emp_invite_link(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await _deny_cb(callback)
        return
    if emp.telegram_id is not None:
        await callback.answer("Цей працівник вже підключив свій Telegram 🟢", show_alert=True)
        return

    link = await _ensure_invite_link(session, bot, emp)
    if not link:
        await callback.answer("Не вдалося згенерувати посилання", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(
            f"Привіт, <b>{esc(emp.full_name)}</b>! 🌸\n"
            "Перейди за цим посиланням і натисни <b>Start</b> (Розпочати), щоб підключитися до бота кафе:\n"
            f"{link}"
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_relink_ask:"))
async def cb_emp_relink_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await _deny_cb(callback)
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"🔄 Точно скинути прив'язку Telegram для <b>{esc(emp.full_name)}</b>?\n"
            "Попередній акаунт одразу втратить доступ до бота, а ти отримаєш нове посилання-запрошення.",
            reply_markup=owner_relink_confirm_kb(emp.id),
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_relink_yes:"))
async def cb_emp_relink_yes(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    await state.clear()
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await _deny_cb(callback)
        return

    emp.telegram_id = None
    emp.telegram_username = None
    emp.invite_code = _generate_invite_code()
    await session.commit()
    await session.refresh(emp)

    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"скинув(ла) Telegram-прив'язку працівнику <b>{esc(emp.full_name)}</b>",
    )

    text, kb = await _build_employee_card_view(
        session,
        bot,
        emp,
        settings,
        prefix="🔄 Прив'язку Telegram скинуто! Надішли працівнику нове посилання нижче:",
        actor=actor,
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb,
            link_preview_options=_NO_PREVIEW,
        )
    await callback.answer("Нове посилання створено 🔄")


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_del_ask:"))
async def cb_emp_delete_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника не знайдено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await _deny_cb(callback)
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"Точно видалити працівника <b>{esc(emp.full_name)}</b>? 🗑\n"
            "Доступ до бота для нього одразу закриється.",
            reply_markup=owner_delete_confirm_kb(emp.id),
        )
    await callback.answer()


@router.callback_query(StateFilter("*"), F.data.startswith("own:emp_del_yes:"))
async def cb_emp_delete_yes(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    if not actor.has_perm("can_manage_staff"):
        await _deny_cb(callback)
        return
    await state.clear()
    emp_id = int((callback.data or "").split(":")[2])
    emp = await session.get(Employee, emp_id)
    if emp is None or not emp.is_active:
        await callback.answer("Працівника вже видалено", show_alert=True)
        return
    if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
        await _deny_cb(callback)
        return

    name = emp.full_name
    await delete_employee_safely(session, emp)
    await _notify_owners_about_admin(
        session,
        bot,
        settings,
        actor,
        f"видалив(ла) працівника <b>{esc(name)}</b>",
    )
    await callback.answer(f"Працівника {name} видалено")
    if callback.message:
        await _show_staff_list(callback.message, session, actor, edit_in_place=True)
