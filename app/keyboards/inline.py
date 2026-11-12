from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.engine import DEFAULT_ROLES
from app.database.models import Employee, PayoutRequest, Role, Shift
from app.services.access import ADMIN_PERM_LABELS, ActorContext, AdminPerms, is_admin_role_name
from app.services.payout_requests import mask_card
from app.services.payroll import EmployeePeriodSummary
from app.utils.datetime_utils import format_date_short, format_date_with_day
from app.utils.formatting import fmt_money

HOME_BTN = InlineKeyboardButton(text="🏠 Головне меню", callback_data="nav:home")


def cancel_kb(
    callback_data: str = "fsm:cancel",
    back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    if back_callback:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback),
                    InlineKeyboardButton(text="✖️ Скасувати", callback_data=callback_data),
                ]
            ]
        )
    if callback_data != "fsm:cancel":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data),
                    InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Скасувати", callback_data=callback_data)]]
    )


# ----------------- КЛАВІАТУРИ ПРАЦІВНИКА -----------------

def employee_date_kb(today: date) -> InlineKeyboardMarkup:
    yesterday = today - timedelta(days=1)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Сьогодні ({format_date_with_day(today)})",
                    callback_data=f"emp:date:{today.isoformat()}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Вчора ({format_date_with_day(yesterday)})",
                    callback_data=f"emp:date:{yesterday.isoformat()}",
                )
            ],
            [InlineKeyboardButton(text="🔎 Інша дата (ДД.ММ.РРРР)", callback_data="emp:date_manual")],
            [
                InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
                HOME_BTN,
            ],
        ]
    )


def employee_time_choice_kb(last_time: tuple[str, str] | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if last_time is not None:
        st, et = last_time
        rows.append([
            InlineKeyboardButton(
                text=f"⚡ Як минулого разу ({st}-{et})",
                callback_data="emp:time_last",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Інша дата", callback_data="emp:shift_menu"),
        InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def employee_banquet_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="☕ Звичайна зміна", callback_data="emp:banquet:no")],
            [InlineKeyboardButton(text="🎉 Так, був банкет", callback_data="emp:banquet:yes")],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="emp:time_back"),
                InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
            ],
        ]
    )


def employee_guests_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Вкажу кількість людей пізніше", callback_data="emp:guests:skip")],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="emp:banquet_back"),
                InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
            ],
        ]
    )


def employee_shift_actions_kb(
    shift: Shift,
    back_callback: str = "emp:shifts_page:0",
    show_back: bool = True,
) -> InlineKeyboardMarkup:
    """Після внесення зміни для дати залишаємо ТІЛЬКИ кнопку зміни годин (і заповнення людей, якщо не вказано)."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Змінити години", callback_data=f"emp:edit_hours:{shift.id}")]
    ]
    if shift.is_banquet and shift.banquet_guests is None:
        rows.append(
            [InlineKeyboardButton(text="👥 Внести кількість людей", callback_data=f"emp:fill_guests:{shift.id}")]
        )
    if show_back:
        rows.append([
            InlineKeyboardButton(text="⬅️ До списку змін", callback_data=back_callback),
            HOME_BTN,
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def employee_salary_periods_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сьогодні", callback_data="emp:salary:today"),
                InlineKeyboardButton(text="Вчора", callback_data="emp:salary:yesterday"),
            ],
            [
                InlineKeyboardButton(text="Цей тиждень", callback_data="emp:salary:week"),
                InlineKeyboardButton(text="2 тижні", callback_data="emp:salary:two_weeks"),
            ],
            [
                InlineKeyboardButton(text="Цей місяць", callback_data="emp:salary:month"),
                InlineKeyboardButton(text="Минулий місяць", callback_data="emp:salary:last_month"),
            ],
            [InlineKeyboardButton(text="📅 Свій період", callback_data="emp:salary:custom")],
            [HOME_BTN],
        ]
    )


def employee_shifts_list_kb(
    shifts: list[Shift],
    page: int,
    has_next: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in shifts:
        mark = "🎉" if s.is_banquet else "🕒"
        warn = " ⚠️" if (s.is_banquet and (s.banquet_guests is None or s.banquet_rate_snapshot is None)) else ""
        label = f"{mark} {format_date_with_day(s.work_date)} | {s.start_time}-{s.end_time}{warn}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"emp:shift_view:{s.id}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Новіші", callback_data=f"emp:shifts_page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Старіші ▶️", callback_data=f"emp:shifts_page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🔎 Знайти / змінити за датою", callback_data="emp:date_manual")])
    rows.append([HOME_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def employee_card_and_requests_kb(
    has_card: bool,
    active_pending_req: PayoutRequest | None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if active_pending_req is None:
        rows.append([InlineKeyboardButton(text="💸 Надіслати запит на виплату", callback_data="emp:req_new")])
    else:
        rows.append([
            InlineKeyboardButton(
                text=f"✖️ Скасувати запит ({fmt_money(active_pending_req.amount)})",
                callback_data=f"emp:req_cancel:{active_pending_req.id}",
            )
        ])
    card_btn_txt = "💳 Змінити номер картки" if has_card else "💳 Ввести номер картки"
    rows.append([InlineKeyboardButton(text=card_btn_txt, callback_data="emp:card_edit")])
    rows.append([HOME_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def employee_payout_req_amount_kb(balance_due: float) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if balance_due > 0:
        rows.append([
            InlineKeyboardButton(
                text=f"💯 Запросити весь залишок ({fmt_money(balance_due)})",
                callback_data="emp:req_amt_full",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="emp:card_menu"),
        InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ----------------- КЛАВІАТУРИ ВЛАСНИЦІ (АНІ) -----------------

def owner_staff_list_kb(employees: list[Employee], can_add: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for emp in employees:
        status = "🟢" if emp.telegram_id else "⏳"
        role_name = emp.role.name if emp.role else "—"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {emp.full_name} ({role_name})",
                    callback_data=f"own:emp:{emp.id}",
                )
            ]
        )
    if can_add:
        rows.append([InlineKeyboardButton(text="➕ Додати працівника", callback_data="own:emp_add")])
    rows.append([HOME_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_roles_kb(
    roles: list[Role],
    mode: str = "add",
    emp_id: int | None = None,
) -> InlineKeyboardMarkup:
    default_order = {name.lower(): idx for idx, name in enumerate(DEFAULT_ROLES)}
    ordered_roles = sorted(
        roles,
        key=lambda r: (default_order.get(r.name.strip().lower(), 999), r.id),
    )

    role_buttons: list[InlineKeyboardButton] = []
    for r in ordered_roles:
        if mode == "add":
            cb = f"own:emp_add_role:{r.id}"
        else:
            cb = f"own:emp_set_role:{emp_id}:{r.id}"
        role_buttons.append(InlineKeyboardButton(text=r.name, callback_data=cb))

    rows: list[list[InlineKeyboardButton]] = [
        role_buttons[i : i + 2] for i in range(0, len(role_buttons), 2)
    ]

    new_cb = "own:emp_add_role_new" if mode == "add" else f"own:emp_set_role_new:{emp_id}"
    rows.append([InlineKeyboardButton(text="➕ Інша посада", callback_data=new_cb)])
    back_cb = "own:staff_list" if mode == "add" else f"own:emp:{emp_id}"
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb),
        InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_add_banquet_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Пропустити (вкажу пізніше)", callback_data="own:emp_add_banquet_skip")],
            [
                InlineKeyboardButton(text="⬅️ До списку", callback_data="own:staff_list"),
                InlineKeyboardButton(text="✖️ Скасувати", callback_data="fsm:cancel"),
            ],
        ]
    )


def owner_employee_card_kb(
    emp: Employee,
    invite_link: str | None = None,
    actor: ActorContext | None = None,
) -> InlineKeyboardMarkup:
    can_manage = actor.has_perm("can_manage_staff") if actor else True
    can_edit_hours = actor.has_perm("can_edit_hours") if actor else True
    can_view_salaries = actor.has_perm("can_view_salaries_reports") if actor else True
    can_adjust = actor.has_perm("can_adjust_money") if actor else True
    can_payout = actor.has_perm("can_payout") if actor else True
    can_view_shifts = actor.has_perm("can_view_staff_shifts") if actor else True

    target_is_admin = bool(emp.role and is_admin_role_name(emp.role.name))
    can_edit_target_admin = (actor.is_owner if actor else True) or not target_is_admin

    rows: list[list[InlineKeyboardButton]] = []
    if emp.telegram_id is None and invite_link and can_manage and can_edit_target_admin:
        share_text = f"Привіт, {emp.full_name}! Перейди за цим посиланням, щоб підключитися до нашого бота кафе 🌸"
        share_url = (
            f"https://t.me/share/url?url={quote(invite_link, safe='')}"
            f"&text={quote(share_text, safe='')}"
        )
        rows.append([InlineKeyboardButton(text="📤 Надіслати запрошення працівнику", url=share_url)])

    if can_edit_hours:
        rows.append([InlineKeyboardButton(text="🕒 Змінити години за дату", callback_data=f"own:emp_hours:{emp.id}")])
    if can_view_shifts or can_edit_hours or can_view_salaries:
        rows.append([InlineKeyboardButton(text="📋 Зміни працівника", callback_data=f"own:emp_shifts:{emp.id}:0")])
    if can_view_salaries:
        rows.append([InlineKeyboardButton(text="📊 Звіт по працівнику", callback_data=f"own:emp_summary:{emp.id}:month")])

    if can_manage:
        rows.append([
            InlineKeyboardButton(text="✏️ Ставка/год", callback_data=f"own:emp_rate:{emp.id}"),
            InlineKeyboardButton(text="📈 Змінити %", callback_data=f"own:emp_pct:{emp.id}"),
        ])
        row_banquet_role = [InlineKeyboardButton(text="🎉 Тариф банкету", callback_data=f"own:emp_banquet:{emp.id}")]
        if can_edit_target_admin:
            row_banquet_role.append(InlineKeyboardButton(text="🏷 Посада", callback_data=f"own:emp_role:{emp.id}"))
        rows.append(row_banquet_role)

    if can_adjust:
        rows.append([
            InlineKeyboardButton(text="➕ Премія", callback_data=f"own:emp_bonus:{emp.id}"),
            InlineKeyboardButton(text="➖ Штраф", callback_data=f"own:emp_fine:{emp.id}"),
        ])

    if can_payout:
        rows.append([InlineKeyboardButton(text="💸 Виплатити", callback_data=f"own:pay_emp:{emp.id}")])

    if can_manage and can_edit_target_admin:
        if emp.telegram_id is None:
            rows.append([InlineKeyboardButton(text="📨 Окреме повідомлення для пересилки", callback_data=f"own:emp_link:{emp.id}")])
        else:
            rows.append([InlineKeyboardButton(text="🔄 Скинути Telegram (нове посилання)", callback_data=f"own:emp_relink_ask:{emp.id}")])
        rows.append([InlineKeyboardButton(text="🗑 Видалити працівника", callback_data=f"own:emp_del_ask:{emp.id}")])

    rows.append([
        InlineKeyboardButton(text="⬅️ До списку працівників", callback_data="own:staff_list"),
        HOME_BTN,
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_relink_confirm_kb(emp_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Так, скинути і дати нове посилання", callback_data=f"own:emp_relink_yes:{emp_id}")],
            [InlineKeyboardButton(text="⬅️ Ні, залишити", callback_data=f"own:emp:{emp_id}")],
        ]
    )


def owner_emp_shifts_kb(emp_id: int, page: int, has_next: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Новіші", callback_data=f"own:emp_shifts:{emp_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Старіші ▶️", callback_data=f"own:emp_shifts:{emp_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="⬅️ До картки працівника", callback_data=f"own:emp:{emp_id}"),
        HOME_BTN,
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_admin_perms_kb(perms: AdminPerms) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in ADMIN_PERM_LABELS:
        mark = "✅" if perms.has(key) else "❌"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {label}",
                callback_data=f"own:adm_toggle:{key}",
            )
        ])
    rows.append([HOME_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_employee_summary_periods_kb(emp_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сьогодні", callback_data=f"own:emp_summary:{emp_id}:today"),
                InlineKeyboardButton(text="Вчора", callback_data=f"own:emp_summary:{emp_id}:yesterday"),
            ],
            [
                InlineKeyboardButton(text="Цей тиждень", callback_data=f"own:emp_summary:{emp_id}:week"),
                InlineKeyboardButton(text="2 тижні", callback_data=f"own:emp_summary:{emp_id}:two_weeks"),
            ],
            [
                InlineKeyboardButton(text="Цей місяць", callback_data=f"own:emp_summary:{emp_id}:month"),
                InlineKeyboardButton(text="Минулий місяць", callback_data=f"own:emp_summary:{emp_id}:last_month"),
            ],
            [InlineKeyboardButton(text="📅 Свій період", callback_data=f"own:emp_summary:{emp_id}:custom")],
            [
                InlineKeyboardButton(text="⬅️ До картки працівника", callback_data=f"own:emp:{emp_id}"),
                HOME_BTN,
            ],
        ]
    )


def owner_delete_confirm_kb(emp_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Так, видалити", callback_data=f"own:emp_del_yes:{emp_id}")],
            [InlineKeyboardButton(text="⬅️ Ні, залишити", callback_data=f"own:emp:{emp_id}")],
        ]
    )


def owner_notify_banquet_kb(emp_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Вказати ставку за банкет", callback_data=f"own:emp_banquet:{emp_id}")],
            [InlineKeyboardButton(text="👤 Відкрити картку працівника", callback_data=f"own:emp:{emp_id}")],
        ]
    )


def owner_revenue_menu_kb(recent_dates: list[date], rev_map: dict[date, float]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for d in recent_dates:
        amt = rev_map.get(d)
        if amt is not None:
            label = f"✅ {format_date_with_day(d)} — {fmt_money(amt)}"
        else:
            label = f"⚪ {format_date_with_day(d)} — не внесено"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"own:rev_date:{d.isoformat()}")])

    rows.append([InlineKeyboardButton(text="🔎 Інша дата (ДД.ММ.РРРР)", callback_data="own:rev_manual")])
    rows.append([HOME_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_reports_kb(date_from: date, date_to: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сьогодні", callback_data="own:rep:today"),
                InlineKeyboardButton(text="Вчора", callback_data="own:rep:yesterday"),
            ],
            [
                InlineKeyboardButton(text="Цей тиждень", callback_data="own:rep:week"),
                InlineKeyboardButton(text="2 тижні", callback_data="own:rep:two_weeks"),
            ],
            [
                InlineKeyboardButton(text="Цей місяць", callback_data="own:rep:month"),
                InlineKeyboardButton(text="Минулий місяць", callback_data="own:rep:last_month"),
            ],
            [InlineKeyboardButton(text="📅 Свій період", callback_data="own:rep:custom")],
            [
                InlineKeyboardButton(
                    text=f"📥 Завантажити Excel ({format_date_short(date_from)}–{format_date_short(date_to)})",
                    callback_data=f"own:rep_xlsx:{date_from.isoformat()}:{date_to.isoformat()}",
                )
            ],
            [HOME_BTN],
        ]
    )


def owner_payouts_menu_kb(
    summaries: list[EmployeePeriodSummary],
    open_requests: list[PayoutRequest] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if open_requests:
        for req in open_requests:
            emp_name = req.employee.full_name if req.employee else f"#{req.employee_id}"
            icon = "⏳" if req.status == "pending" else "✅"
            rows.append([
                InlineKeyboardButton(
                    text=f"{icon} Запит: {emp_name} — {fmt_money(req.amount)}",
                    callback_data=f"own:req_view:{req.id}",
                )
            ])
    for s in summaries:
        if not s.employee.is_active and abs(s.balance_due) < 0.01:
            continue
        card_tag = " 💳" if s.employee.card_number else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{s.employee.full_name}{card_tag} — {fmt_money(s.balance_due)}",
                    callback_data=f"own:pay_emp:{s.employee.id}",
                )
            ]
        )
    rows.append([HOME_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_payout_employee_kb(
    emp_id: int,
    balance_due: float,
    card_digits: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if card_digits:
        rows.append([
            InlineKeyboardButton(
                text=f"📋 Скопіювати картку ({mask_card(card_digits)})",
                copy_text=CopyTextButton(text=card_digits),
            )
        ])
    rows.extend([
        [InlineKeyboardButton(text="📅 Виплатити за дати / період", callback_data=f"own:pay_period:{emp_id}")],
        [InlineKeyboardButton(text="💵 Ввести суму вручну (аванс)", callback_data=f"own:pay_custom:{emp_id}")],
    ])
    if balance_due > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"💯 Закрити весь залишок ({fmt_money(balance_due)})",
                    callback_data=f"own:pay_full:{emp_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="📜 Історія виплат / премій", callback_data=f"own:pay_hist:{emp_id}")])
    rows.append([InlineKeyboardButton(text="👤 Картка працівника", callback_data=f"own:emp:{emp_id}")])
    rows.append([
        InlineKeyboardButton(text="⬅️ До списку балансів", callback_data="own:pay_menu"),
        HOME_BTN,
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def owner_pay_confirm_kb(emp_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Підтвердити виплату", callback_data="own:pay_confirm_ok")],
            [InlineKeyboardButton(text="✖️ Скасувати", callback_data=f"own:pay_emp:{emp_id}")],
        ]
    )
