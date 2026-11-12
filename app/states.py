from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class EmployeeShiftStates(StatesGroup):
    waiting_manual_date = State()
    waiting_time_range = State()
    waiting_banquet_choice = State()
    waiting_banquet_guests = State()
    waiting_edit_hours = State()
    waiting_fill_guests = State()
    waiting_salary_custom_period = State()
    waiting_card_number = State()
    waiting_payout_req_card = State()
    waiting_payout_req_amount = State()
    waiting_payout_req_comment = State()


class OwnerStaffStates(StatesGroup):
    add_name = State()
    add_role_pick = State()
    add_role_custom = State()
    add_hourly_rate = State()
    add_percent = State()
    add_banquet_rate = State()

    edit_role_custom = State()
    edit_hourly_rate = State()
    edit_percent = State()
    edit_banquet_rate = State()

    edit_emp_hours_date = State()
    edit_emp_hours_time = State()

    add_bonus_amount = State()
    add_bonus_comment = State()
    add_fine_amount = State()
    add_fine_comment = State()

    emp_summary_custom_period = State()


class OwnerFinanceStates(StatesGroup):
    waiting_revenue_manual_date = State()
    waiting_revenue_amount = State()
    waiting_report_custom_period = State()

    waiting_payout_period_dates = State()
    waiting_payout_custom_amount = State()
    waiting_payout_custom_comment = State()
    waiting_payout_confirm = State()
