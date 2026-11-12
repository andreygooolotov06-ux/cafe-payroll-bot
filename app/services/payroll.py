from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from app.database.models import Adjustment, DailyRevenue, Employee, Payout, Shift

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")


def to_dec(value: float | int | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def q2_dec(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def q2_float(value: Decimal) -> float:
    return float(q2_dec(value))


@dataclass(frozen=True)
class ShiftBreakdown:
    shift: Shift
    base_pay: float
    daily_revenue: float | None
    percent_pay: float
    revenue_missing: bool
    banquet_pay: float
    missing_banquet_rate: bool
    missing_banquet_guests: bool
    total_pay: float


@dataclass(frozen=True)
class EmployeePeriodSummary:
    employee: Employee
    date_from: date
    date_to: date
    shifts_count: int
    total_hours: float
    base_pay: float
    percent_pay: float
    banquet_pay: float
    bonuses: float
    fines: float
    earned_period: float
    paid_period: float
    all_time_earned: float
    all_time_paid: float
    balance_due: float
    shift_breakdowns: list[ShiftBreakdown]
    missing_banquet_rate_dates: list[date]
    missing_banquet_guests_dates: list[date]
    missing_revenue_dates: list[date]


def compute_shift_breakdown(shift: Shift, revenue_map: dict[date, float]) -> ShiftBreakdown:
    hours_d = to_dec(shift.hours)
    rate_d = to_dec(shift.hourly_rate_snapshot)
    pct_d = to_dec(shift.percent_snapshot)

    base_pay_d = q2_dec(hours_d * rate_d)
    daily_revenue = revenue_map.get(shift.work_date)

    revenue_missing = daily_revenue is None and pct_d > 0
    if daily_revenue is not None and pct_d > 0:
        percent_pay_d = q2_dec((to_dec(daily_revenue) * pct_d) / _HUNDRED)
    else:
        percent_pay_d = Decimal("0.00")

    missing_banquet_rate = False
    missing_banquet_guests = False
    banquet_pay_d = Decimal("0.00")

    if shift.is_banquet:
        missing_banquet_rate = shift.banquet_rate_snapshot is None
        missing_banquet_guests = shift.banquet_guests is None
        if not missing_banquet_rate and not missing_banquet_guests:
            banquet_pay_d = q2_dec(
                Decimal(int(shift.banquet_guests or 0)) * to_dec(shift.banquet_rate_snapshot)
            )

    total_pay_d = q2_dec(base_pay_d + percent_pay_d + banquet_pay_d)
    return ShiftBreakdown(
        shift=shift,
        base_pay=float(base_pay_d),
        daily_revenue=daily_revenue,
        percent_pay=float(percent_pay_d),
        revenue_missing=revenue_missing,
        banquet_pay=float(banquet_pay_d),
        missing_banquet_rate=missing_banquet_rate,
        missing_banquet_guests=missing_banquet_guests,
        total_pay=float(total_pay_d),
    )


async def _load_revenue_map(
    session: AsyncSession,
    dates: set[date],
) -> dict[date, float]:
    if not dates:
        return {}
    rows = (
        await session.scalars(select(DailyRevenue).where(DailyRevenue.work_date.in_(dates)))
    ).all()
    return {row.work_date: row.amount for row in rows}


async def _compute_all_time_totals(
    session: AsyncSession,
    employee_id: int,
    revenue_map_cache: dict[date, float] | None = None,
) -> tuple[float, float]:
    all_shifts = (
        await session.scalars(select(Shift).where(Shift.employee_id == employee_id))
    ).all()

    if revenue_map_cache is None:
        needed_dates = {s.work_date for s in all_shifts}
        revenue_map = await _load_revenue_map(session, needed_dates)
    else:
        missing_dates = {s.work_date for s in all_shifts if s.work_date not in revenue_map_cache}
        if missing_dates:
            revenue_map_cache.update(await _load_revenue_map(session, missing_dates))
        revenue_map = revenue_map_cache

    shifts_earned_d = sum(
        (to_dec(compute_shift_breakdown(s, revenue_map).total_pay) for s in all_shifts),
        Decimal("0.00"),
    )

    all_adjustments = (
        await session.scalars(select(Adjustment).where(Adjustment.employee_id == employee_id))
    ).all()
    bonuses_d = sum(
        (to_dec(a.amount) for a in all_adjustments if a.kind == "bonus"),
        Decimal("0.00"),
    )
    fines_d = sum(
        (to_dec(a.amount) for a in all_adjustments if a.kind == "fine"),
        Decimal("0.00"),
    )

    all_payout_amounts = (
        await session.scalars(select(Payout.amount).where(Payout.employee_id == employee_id))
    ).all()
    all_paid_d = sum((to_dec(amt) for amt in all_payout_amounts), Decimal("0.00"))

    all_time_earned = q2_float(shifts_earned_d + bonuses_d - fines_d)
    return all_time_earned, q2_float(all_paid_d)


async def get_employee_summary(
    session: AsyncSession,
    employee: Employee,
    date_from: date,
    date_to: date,
    revenue_map_cache: dict[date, float] | None = None,
) -> EmployeePeriodSummary:
    period_shifts = (
        await session.scalars(
            select(Shift)
            .where(
                Shift.employee_id == employee.id,
                Shift.work_date >= date_from,
                Shift.work_date <= date_to,
            )
            .order_by(Shift.work_date.desc())
        )
    ).all()

    if revenue_map_cache is None:
        rev_map = await _load_revenue_map(session, {s.work_date for s in period_shifts})
    else:
        needed = {s.work_date for s in period_shifts if s.work_date not in revenue_map_cache}
        if needed:
            revenue_map_cache.update(await _load_revenue_map(session, needed))
        rev_map = revenue_map_cache

    breakdowns = [compute_shift_breakdown(s, rev_map) for s in period_shifts]

    total_hours_d = q2_dec(sum((to_dec(b.shift.hours) for b in breakdowns), Decimal("0.00")))
    base_pay_d = q2_dec(sum((to_dec(b.base_pay) for b in breakdowns), Decimal("0.00")))
    percent_pay_d = q2_dec(sum((to_dec(b.percent_pay) for b in breakdowns), Decimal("0.00")))
    banquet_pay_d = q2_dec(sum((to_dec(b.banquet_pay) for b in breakdowns), Decimal("0.00")))

    missing_rate_dates = sorted(
        {b.shift.work_date for b in breakdowns if b.missing_banquet_rate}
    )
    missing_guests_dates = sorted(
        {b.shift.work_date for b in breakdowns if b.missing_banquet_guests}
    )
    missing_rev_dates = sorted(
        {b.shift.work_date for b in breakdowns if b.revenue_missing}
    )

    period_adj = (
        await session.scalars(
            select(Adjustment).where(
                Adjustment.employee_id == employee.id,
                Adjustment.adj_date >= date_from,
                Adjustment.adj_date <= date_to,
            )
        )
    ).all()
    bonuses_d = q2_dec(
        sum((to_dec(a.amount) for a in period_adj if a.kind == "bonus"), Decimal("0.00"))
    )
    fines_d = q2_dec(
        sum((to_dec(a.amount) for a in period_adj if a.kind == "fine"), Decimal("0.00"))
    )

    period_payout_amounts = (
        await session.scalars(
            select(Payout.amount).where(
                Payout.employee_id == employee.id,
                Payout.paid_date >= date_from,
                Payout.paid_date <= date_to,
            )
        )
    ).all()
    paid_period_d = q2_dec(sum((to_dec(amt) for amt in period_payout_amounts), Decimal("0.00")))

    earned_period_d = q2_dec(base_pay_d + percent_pay_d + banquet_pay_d + bonuses_d - fines_d)

    all_time_earned, all_time_paid = await _compute_all_time_totals(
        session, employee.id, revenue_map_cache=rev_map
    )
    balance_due = q2_float(to_dec(all_time_earned) - to_dec(all_time_paid))

    return EmployeePeriodSummary(
        employee=employee,
        date_from=date_from,
        date_to=date_to,
        shifts_count=len(breakdowns),
        total_hours=float(total_hours_d),
        base_pay=float(base_pay_d),
        percent_pay=float(percent_pay_d),
        banquet_pay=float(banquet_pay_d),
        bonuses=float(bonuses_d),
        fines=float(fines_d),
        earned_period=float(earned_period_d),
        paid_period=float(paid_period_d),
        all_time_earned=all_time_earned,
        all_time_paid=all_time_paid,
        balance_due=balance_due,
        shift_breakdowns=breakdowns,
        missing_banquet_rate_dates=missing_rate_dates,
        missing_banquet_guests_dates=missing_guests_dates,
        missing_revenue_dates=missing_rev_dates,
    )


async def get_all_employees_summary(
    session: AsyncSession,
    date_from: date,
    date_to: date,
    *,
    include_inactive_with_activity: bool = True,
) -> list[EmployeePeriodSummary]:
    """Балк-розрахунок по всіх працівниках за 5 SQL-запитів (без N+1)."""
    emp_stmt = select(Employee).order_by(Employee.is_active.desc(), Employee.full_name.asc())
    if not include_inactive_with_activity:
        emp_stmt = emp_stmt.where(Employee.is_active.is_(True))
    employees = list((await session.scalars(emp_stmt)).all())
    if not employees:
        return []

    emp_ids = [e.id for e in employees]

    rev_rows = (await session.execute(select(DailyRevenue.work_date, DailyRevenue.amount))).all()
    rev_map: dict[date, float] = {work_date: float(amount) for work_date, amount in rev_rows}

    all_shifts = (
        await session.scalars(
            select(Shift)
            .options(raiseload(Shift.employee))
            .where(Shift.employee_id.in_(emp_ids))
            .order_by(Shift.work_date.desc())
        )
    ).all()

    all_time_shifts_earned: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    period_breakdowns_by_emp: dict[int, list[ShiftBreakdown]] = defaultdict(list)

    for sh in all_shifts:
        bd = compute_shift_breakdown(sh, rev_map)
        eid = sh.employee_id
        all_time_shifts_earned[eid] += to_dec(bd.total_pay)
        if date_from <= sh.work_date <= date_to:
            period_breakdowns_by_emp[eid].append(bd)

    all_adj_rows = (
        await session.execute(
            select(Adjustment.employee_id, Adjustment.adj_date, Adjustment.kind, Adjustment.amount).where(
                Adjustment.employee_id.in_(emp_ids)
            )
        )
    ).all()

    all_bonuses: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    all_fines: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    period_bonuses: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    period_fines: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))

    for eid, adj_date, kind, amount in all_adj_rows:
        amt_d = to_dec(amount)
        if kind == "bonus":
            all_bonuses[eid] += amt_d
            if date_from <= adj_date <= date_to:
                period_bonuses[eid] += amt_d
        elif kind == "fine":
            all_fines[eid] += amt_d
            if date_from <= adj_date <= date_to:
                period_fines[eid] += amt_d

    all_payout_rows = (
        await session.execute(
            select(Payout.employee_id, Payout.paid_date, Payout.amount).where(
                Payout.employee_id.in_(emp_ids)
            )
        )
    ).all()

    all_paid_by_emp: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    period_paid_by_emp: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))

    for eid, paid_date, amount in all_payout_rows:
        amt_d = to_dec(amount)
        all_paid_by_emp[eid] += amt_d
        if date_from <= paid_date <= date_to:
            period_paid_by_emp[eid] += amt_d

    summaries: list[EmployeePeriodSummary] = []
    for emp in employees:
        eid = emp.id
        breakdowns = period_breakdowns_by_emp.get(eid, [])
        total_hours_d = q2_dec(sum((to_dec(b.shift.hours) for b in breakdowns), Decimal("0.00")))
        base_pay_d = q2_dec(sum((to_dec(b.base_pay) for b in breakdowns), Decimal("0.00")))
        percent_pay_d = q2_dec(sum((to_dec(b.percent_pay) for b in breakdowns), Decimal("0.00")))
        banquet_pay_d = q2_dec(sum((to_dec(b.banquet_pay) for b in breakdowns), Decimal("0.00")))

        bonuses_d = q2_dec(period_bonuses[eid])
        fines_d = q2_dec(period_fines[eid])
        paid_period_d = q2_dec(period_paid_by_emp[eid])
        earned_period_d = q2_dec(base_pay_d + percent_pay_d + banquet_pay_d + bonuses_d - fines_d)

        all_time_earned = q2_float(all_time_shifts_earned[eid] + all_bonuses[eid] - all_fines[eid])
        all_time_paid = q2_float(all_paid_by_emp[eid])
        balance_due = q2_float(to_dec(all_time_earned) - to_dec(all_time_paid))

        summary = EmployeePeriodSummary(
            employee=emp,
            date_from=date_from,
            date_to=date_to,
            shifts_count=len(breakdowns),
            total_hours=float(total_hours_d),
            base_pay=float(base_pay_d),
            percent_pay=float(percent_pay_d),
            banquet_pay=float(banquet_pay_d),
            bonuses=float(bonuses_d),
            fines=float(fines_d),
            earned_period=float(earned_period_d),
            paid_period=float(paid_period_d),
            all_time_earned=all_time_earned,
            all_time_paid=all_time_paid,
            balance_due=balance_due,
            shift_breakdowns=breakdowns,
            missing_banquet_rate_dates=sorted({b.shift.work_date for b in breakdowns if b.missing_banquet_rate}),
            missing_banquet_guests_dates=sorted({b.shift.work_date for b in breakdowns if b.missing_banquet_guests}),
            missing_revenue_dates=sorted({b.shift.work_date for b in breakdowns if b.revenue_missing}),
        )

        # Активних показуємо завжди; видалених показуємо лише якщо за період є зміни/рухи
        if emp.is_active or (
            summary.shifts_count > 0
            or summary.bonuses > 0
            or summary.fines > 0
            or summary.paid_period > 0
        ):
            summaries.append(summary)

    return summaries


async def set_employee_banquet_rate(
    session: AsyncSession,
    employee: Employee,
    rate: float,
) -> list[date]:
    """Оновлює банкетну ставку працівника та перераховує ті банкети, де ставка була None."""
    employee.banquet_rate_per_person = q2_float(to_dec(rate))

    pending_shifts = (
        await session.scalars(
            select(Shift)
            .where(
                Shift.employee_id == employee.id,
                Shift.is_banquet.is_(True),
                Shift.banquet_rate_snapshot.is_(None),
            )
            .order_by(Shift.work_date.asc())
        )
    ).all()

    updated_dates: list[date] = []
    for s in pending_shifts:
        s.banquet_rate_snapshot = employee.banquet_rate_per_person
        updated_dates.append(s.work_date)

    await session.commit()
    await session.refresh(employee)
    return updated_dates


async def delete_employee_safely(session: AsyncSession, employee: Employee) -> str:
    """Видаляє працівника повністю, якщо історії немає, або архівує та забирає доступ."""
    shifts_count = await session.scalar(
        select(func.count(Shift.id)).where(Shift.employee_id == employee.id)
    )
    adj_count = await session.scalar(
        select(func.count(Adjustment.id)).where(Adjustment.employee_id == employee.id)
    )
    payout_count = await session.scalar(
        select(func.count(Payout.id)).where(Payout.employee_id == employee.id)
    )

    if (shifts_count or 0) == 0 and (adj_count or 0) == 0 and (payout_count or 0) == 0:
        await session.delete(employee)
        await session.commit()
        return "hard"

    employee.is_active = False
    employee.telegram_id = None
    employee.telegram_username = None
    employee.invite_code = None
    await session.commit()
    return "soft"


async def get_recent_revenues(session: AsyncSession, dates: list[date]) -> dict[date, float]:
    return await _load_revenue_map(session, set(dates))
