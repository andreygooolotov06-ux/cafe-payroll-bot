from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.services.access import AdminPerms

# Кнопки головного меню працівника
BTN_EMP_SHIFT = "🕒 Внести / змінити зміну"
BTN_EMP_MY_SALARY = "💰 Моя зарплата"
BTN_EMP_MY_SHIFTS = "📅 Мої зміни"
BTN_EMP_PAYOUT_REQ = "💳 Картка і запит на виплату"

# Кнопки головного меню власниці (Ані) та адміна
BTN_OWN_STAFF = "👥 Працівники"
BTN_OWN_REVENUE = "💵 Каса дня"
BTN_OWN_REPORTS = "📊 Загальна звітність"
BTN_OWN_PAYOUTS = "💸 Виплати і баланси"
BTN_OWN_ADMIN_PERMS = "⚙️ Права адміна"

EMPLOYEE_MENU_TEXTS = {BTN_EMP_SHIFT, BTN_EMP_MY_SALARY, BTN_EMP_MY_SHIFTS, BTN_EMP_PAYOUT_REQ}
OWNER_MENU_TEXTS = {
    BTN_OWN_STAFF,
    BTN_OWN_REVENUE,
    BTN_OWN_REPORTS,
    BTN_OWN_PAYOUTS,
    BTN_OWN_ADMIN_PERMS,
}


def employee_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_EMP_SHIFT)],
            [KeyboardButton(text=BTN_EMP_MY_SALARY), KeyboardButton(text=BTN_EMP_MY_SHIFTS)],
            [KeyboardButton(text=BTN_EMP_PAYOUT_REQ)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_main_menu(perms: AdminPerms) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_EMP_SHIFT)],
        [KeyboardButton(text=BTN_EMP_MY_SALARY), KeyboardButton(text=BTN_EMP_MY_SHIFTS)],
        [KeyboardButton(text=BTN_EMP_PAYOUT_REQ)],
    ]
    admin_btns: list[KeyboardButton] = []
    if perms.can_open_staff_tab:
        admin_btns.append(KeyboardButton(text=BTN_OWN_STAFF))
    if perms.can_enter_revenue:
        admin_btns.append(KeyboardButton(text=BTN_OWN_REVENUE))
    if perms.can_view_salaries_reports:
        admin_btns.append(KeyboardButton(text=BTN_OWN_REPORTS))
    if perms.can_payout:
        admin_btns.append(KeyboardButton(text=BTN_OWN_PAYOUTS))

    for i in range(0, len(admin_btns), 2):
        rows.append(admin_btns[i : i + 2])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def owner_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_OWN_STAFF), KeyboardButton(text=BTN_OWN_REVENUE)],
            [KeyboardButton(text=BTN_OWN_REPORTS), KeyboardButton(text=BTN_OWN_PAYOUTS)],
            [KeyboardButton(text=BTN_OWN_ADMIN_PERMS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
