from __future__ import annotations

import calendar
import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from app.database.models import Adjustment, DailyRevenue, Employee, Payout, PayoutRequest, Shift
from app.services.payout_requests import format_card_display, mask_card, serialize_payout_request
from app.services.payroll import (
    compute_shift_breakdown,
    get_all_employees_summary,
    get_employee_summary,
    q2_dec,
    q2_float,
    to_dec,
)
from app.utils.datetime_utils import WEEKDAYS_FULL_UK, WEEKDAYS_SHORT_UK, format_date_with_day


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (float(s[mid - 1]) + float(s[mid])) / 2.0


def _mad(values: list[float], center: float | None = None) -> float:
    if not values:
        return 0.0
    c = _median(values) if center is None else center
    devs = [abs(v - c) for v in values]
    return _median(devs)


def modified_z_scores(values: list[float]) -> list[float]:
    """Iglewicz-Hoaglin Modified Z-score: M_i = 0.6745 * (x_i - median) / MAD."""
    if not values:
        return []
    med = _median(values)
    mad = _mad(values, med)
    if mad <= 1e-9:
        return [0.0 for _ in values]
    scale = 0.6745 / mad
    return [(v - med) * scale for v in values]


async def predict_employee_shift_patterns(
    session: AsyncSession,
    employee: Employee,
    target_date: date,
) -> dict[str, Any]:
    """Визначає типові зміни працівника: минулий час, патерн цього дня тижня і часті варіанти."""
    shifts = (
        await session.scalars(
            select(Shift)
            .where(Shift.employee_id == employee.id)
            .order_by(Shift.work_date.desc(), Shift.id.desc())
            .limit(90)
        )
    ).all()

    last_range: dict[str, Any] | None = None
    if employee.last_start_time and employee.last_end_time:
        last_range = {
            "start_time": employee.last_start_time,
            "end_time": employee.last_end_time,
            "label": f"{employee.last_start_time}-{employee.last_end_time}",
            "source": "last",
            "title": "Як минулого разу",
        }
    elif shifts:
        s0 = shifts[0]
        last_range = {
            "start_time": s0.start_time,
            "end_time": s0.end_time,
            "label": f"{s0.start_time}-{s0.end_time}",
            "source": "last",
            "title": "Як минулого разу",
        }

    target_wd = target_date.weekday()
    wd_counter: Counter[tuple[str, str]] = Counter()
    overall_counter: Counter[tuple[str, str]] = Counter()

    for s in shifts:
        pair = (s.start_time, s.end_time)
        overall_counter[pair] += 1
        if s.work_date.weekday() == target_wd:
            wd_counter[pair] += 1

    weekday_mode: dict[str, Any] | None = None
    if wd_counter:
        top_pair, count = wd_counter.most_common(1)[0]
        if count >= 2:
            weekday_mode = {
                "start_time": top_pair[0],
                "end_time": top_pair[1],
                "label": f"{top_pair[0]}-{top_pair[1]}",
                "source": "weekday",
                "title": f"Типовий час у {WEEKDAYS_SHORT_UK[target_wd]}",
                "count": count,
            }

    overall_mode: dict[str, Any] | None = None
    if overall_counter:
        top_pair, count = overall_counter.most_common(1)[0]
        if count >= 2:
            overall_mode = {
                "start_time": top_pair[0],
                "end_time": top_pair[1],
                "label": f"{top_pair[0]}-{top_pair[1]}",
                "source": "mode",
                "title": "Найчастіша зміна",
                "count": count,
            }

    presets: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for item in (last_range, weekday_mode, overall_mode):
        if item and item["label"] not in seen_labels:
            seen_labels.add(item["label"])
            presets.append(item)

    return {
        "last_time": last_range,
        "weekday_pattern": weekday_mode,
        "overall_pattern": overall_mode,
        "suggested_times": presets,
    }


def build_theil_sen_revenue_forecast(
    revenue_rows: list[DailyRevenue],
    ref_date: date,
    recent_labor_ratio: float,
    horizon_days: int = 7,
) -> dict[str, Any]:
    """Прогноз каси на 7 днів: робастна медіанна регресія Тейла-Сена + індекс дня тижня з Лапласівським згладжуванням + MAD-інтервал 80%."""
    window_start = ref_date - timedelta(days=56)
    history = sorted(
        [r for r in revenue_rows if window_start <= r.work_date <= ref_date and r.amount >= 0],
        key=lambda r: r.work_date,
    )
    n = len(history)
    if n < 3:
        return {
            "status": "insufficient_data",
            "history_days": n,
            "min_days_required": 3,
            "message": f"Для AI-прогнозу потрібно щонайменше 3 дні внесеної каси (зараз {n}).",
            "days": [],
            "projected_7d_revenue": 0.0,
            "projected_7d_low": 0.0,
            "projected_7d_high": 0.0,
            "trend_per_day": 0.0,
            "trend_direction": "flat",
        }

    t0 = history[0].work_date
    ts = [float((r.work_date - t0).days) for r in history]
    ys = [float(r.amount) for r in history]
    med_y = max(1.0, _median(ys))

    # 1. Непараметричний медіанний нахил Тейла-Сена
    slopes: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            dt = ts[j] - ts[i]
            if dt > 0:
                slopes.append((ys[j] - ys[i]) / dt)
    raw_slope = _median(slopes) if slopes else 0.0

    # Демпфування для малих вибірок (N < 7) та захист від екстремальної екстраполяції
    damping = min(1.0, max(0.0, (n - 2) / 5.0))
    slope = raw_slope * damping
    max_abs_slope = med_y * 0.05  # макс ±5% медіанної каси в день як довгостроковий нахил
    slope = max(-max_abs_slope, min(max_abs_slope, slope))

    # 2. Експоненційно зважений рівень, прив'язаний до ref_date
    t_max = ts[-1]
    t_ref = float((ref_date - t0).days)
    weights: list[float] = []
    detrended: list[float] = []
    for t_val, y_val in zip(ts, ys):
        w = math.exp(-(t_max - t_val) / 14.0)
        weights.append(w)
        detrended.append(y_val - slope * t_val)
    w_sum = sum(weights) or 1.0
    intercept = sum(w * a for w, a in zip(weights, detrended)) / w_sum
    level_at_ref = max(med_y * 0.2, intercept + slope * t_ref)

    def trend_baseline(t_val: float) -> float:
        return max(med_y * 0.15, intercept + slope * t_val)

    # 3. Емпірично-байєсівський індекс дня тижня (стиснення до 1.0 з пріором k=2)
    weekday_factors: dict[int, float] = {wd: 1.0 for wd in range(7)}
    if n >= 7:
        by_wd: dict[int, list[float]] = defaultdict(list)
        for r, t_val, y_val in zip(history, ts, ys):
            base = max(med_y * 0.25, trend_baseline(t_val))
            ratio = max(0.5, min(1.75, y_val / base))
            by_wd[r.work_date.weekday()].append(ratio)

        raw_factors: dict[int, float] = {}
        for wd in range(7):
            obs = by_wd.get(wd, [])
            nw = len(obs)
            if nw == 0:
                raw_factors[wd] = 1.0
            else:
                alpha = nw / (nw + 2.0)
                raw_factors[wd] = alpha * _median(obs) + (1.0 - alpha) * 1.0

        mean_factor = (sum(raw_factors.values()) / 7.0) or 1.0
        weekday_factors = {wd: raw_factors[wd] / mean_factor for wd in range(7)}

    # 4. Робастна шкала похибок через MAD для 80% інтервалу довіри (z = 1.2816)
    residuals: list[float] = []
    for r, t_val, y_val in zip(history, ts, ys):
        fitted = trend_baseline(t_val) * weekday_factors[r.work_date.weekday()]
        residuals.append(y_val - fitted)

    res_med = _median(residuals)
    res_mad = _mad(residuals, res_med)
    sigma = 1.4826 * res_mad if res_mad > 0 else med_y * 0.12
    sigma = max(med_y * 0.08, min(med_y * 0.45, sigma))
    z_80 = 1.2816

    days_out: list[dict[str, Any]] = []
    proj_sum = Decimal("0.00")
    low_sum = Decimal("0.00")
    high_sum = Decimal("0.00")

    labor_ratio = max(0.0, min(0.8, float(recent_labor_ratio)))

    for h in range(1, horizon_days + 1):
        d = ref_date + timedelta(days=h)
        wd = d.weekday()
        base_proj = max(0.0, level_at_ref + slope * h) * weekday_factors[wd]
        half_width = z_80 * sigma * math.sqrt(1.0 + 0.08 * h)
        low_v = max(0.0, base_proj - half_width)
        high_v = base_proj + half_width

        p_dec = q2_dec(to_dec(base_proj))
        l_dec = q2_dec(to_dec(low_v))
        h_dec = q2_dec(to_dec(high_v))
        est_pay_dec = q2_dec(p_dec * to_dec(labor_ratio))

        proj_sum += p_dec
        low_sum += l_dec
        high_sum += h_dec

        days_out.append({
            "date": d.isoformat(),
            "label": d.strftime("%d.%m"),
            "weekday_short": WEEKDAYS_SHORT_UK[wd],
            "weekday_full": WEEKDAYS_FULL_UK[wd],
            "weekday_factor": round(weekday_factors[wd], 3),
            "predicted_revenue": float(p_dec),
            "low_revenue": float(l_dec),
            "high_revenue": float(h_dec),
            "estimated_payroll": float(est_pay_dec),
        })

    if slope > med_y * 0.005:
        trend_dir = "up"
    elif slope < -med_y * 0.005:
        trend_dir = "down"
    else:
        trend_dir = "flat"

    return {
        "status": "ready",
        "history_days": n,
        "min_days_required": 3,
        "model": "Theil-Sen + Empirical-Bayes Weekday Index + MAD-80%",
        "trend_per_day": q2_float(to_dec(slope)),
        "trend_direction": trend_dir,
        "median_revenue": q2_float(to_dec(med_y)),
        "residual_sigma": q2_float(to_dec(sigma)),
        "weekday_factors": {WEEKDAYS_SHORT_UK[wd]: round(weekday_factors[wd], 2) for wd in range(7)},
        "projected_7d_revenue": q2_float(proj_sum),
        "projected_7d_low": q2_float(low_sum),
        "projected_7d_high": q2_float(high_sum),
        "days": days_out,
    }


async def compute_owner_analytics(
    session: AsyncSession,
    date_from: date,
    date_to: date,
    ref_date: date,
) -> dict[str, Any]:
    """Повна аналітика кафе за період: KPI, часова серія, аномалії (Modified Z-score), цілісність даних, прогноз, по ролях."""
    summaries = await get_all_employees_summary(session, date_from, date_to)

    # Запити на виплату (лічильники для головного екрану за 1 запит)
    req_counts = dict(
        (
            await session.execute(
                select(PayoutRequest.status, func.count(PayoutRequest.id))
                .where(PayoutRequest.status.in_(("pending", "approved")))
                .group_by(PayoutRequest.status)
            )
        ).all()
    )
    pending_reqs_cnt = int(req_counts.get("pending", 0) or 0)
    approved_reqs_cnt = int(req_counts.get("approved", 0) or 0)

    tot_payroll_d = Decimal("0.00")
    tot_base_d = Decimal("0.00")
    tot_percent_d = Decimal("0.00")
    tot_banquet_d = Decimal("0.00")
    tot_bonuses_d = Decimal("0.00")
    tot_fines_d = Decimal("0.00")
    tot_paid_d = Decimal("0.00")
    tot_balance_due_d = Decimal("0.00")
    tot_hours_d = Decimal("0.00")
    tot_shifts = 0

    role_stats: dict[str, dict[str, Any]] = {}
    day_payroll_map: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    day_hours_map: dict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
    day_shifts_count: dict[date, int] = defaultdict(int)
    period_shift_breakdowns = []

    for s in summaries:
        tot_payroll_d += to_dec(s.earned_period)
        tot_base_d += to_dec(s.base_pay)
        tot_percent_d += to_dec(s.percent_pay)
        tot_banquet_d += to_dec(s.banquet_pay)
        tot_bonuses_d += to_dec(s.bonuses)
        tot_fines_d += to_dec(s.fines)
        tot_paid_d += to_dec(s.paid_period)
        tot_balance_due_d += to_dec(s.balance_due)
        tot_hours_d += to_dec(s.total_hours)
        tot_shifts += s.shifts_count

        role_name = s.employee.role.name if s.employee.role else "Без посади"
        rs = role_stats.setdefault(
            role_name,
            {
                "role": role_name,
                "employees": 0,
                "shifts": 0,
                "hours": Decimal("0.00"),
                "payroll": Decimal("0.00"),
            },
        )
        rs["employees"] += 1
        rs["shifts"] += s.shifts_count
        rs["hours"] += to_dec(s.total_hours)
        rs["payroll"] += to_dec(s.earned_period)

        for bd in s.shift_breakdowns:
            wd = bd.shift.work_date
            day_payroll_map[wd] += to_dec(bd.total_pay)
            day_hours_map[wd] += to_dec(bd.shift.hours)
            day_shifts_count[wd] += 1
            period_shift_breakdowns.append((s.employee, bd))

    # Також врахувати премії/штрафи у денній часовій серії
    period_adj_rows = (
        await session.execute(
            select(Adjustment.adj_date, Adjustment.kind, Adjustment.amount).where(
                Adjustment.adj_date >= date_from,
                Adjustment.adj_date <= date_to,
            )
        )
    ).all()
    for adj_date, adj_kind, adj_amount in period_adj_rows:
        if adj_kind == "bonus":
            day_payroll_map[adj_date] += to_dec(adj_amount)
        elif adj_kind == "fine":
            day_payroll_map[adj_date] -= to_dec(adj_amount)

    cost_per_hour = q2_float(tot_payroll_d / tot_hours_d) if tot_hours_d > 0 else None

    # Денна серія ФОП та годин (до 95 днів суцільно, або присутні дати при довшому горизонті)
    span_days = (date_to - date_from).days + 1
    if span_days <= 95:
        series_dates = [date_from + timedelta(days=i) for i in range(max(1, span_days))]
    else:
        series_dates = sorted(set(day_payroll_map.keys()) | set(day_hours_map.keys()))

    daily_series: list[dict[str, Any]] = []
    for d in series_dates:
        pay_dec = q2_dec(day_payroll_map.get(d, Decimal("0.00")))
        hrs_dec = q2_dec(day_hours_map.get(d, Decimal("0.00")))
        daily_series.append({
            "date": d.isoformat(),
            "label": d.strftime("%d.%m"),
            "weekday_short": WEEKDAYS_SHORT_UK[d.weekday()],
            "payroll": float(pay_dec),
            "hours": float(hrs_dec),
            "shifts_count": int(day_shifts_count.get(d, 0)),
        })

    # Розрахунок аномалій та перевірка цілісності даних
    anomalies: list[dict[str, Any]] = []
    seen_issue_keys: set[str] = set()

    # A1. Банкетні зміни без тарифу або без кількості гостей
    for emp, bd in period_shift_breakdowns:
        s = bd.shift
        if bd.missing_banquet_rate:
            key = f"banquet_rate:{emp.id}"
            if key not in seen_issue_keys:
                seen_issue_keys.add(key)
                anomalies.append({
                    "id": key,
                    "severity": "high",
                    "category": "missing_data",
                    "title": f"Не вказано тариф банкету — {emp.full_name}",
                    "description": f"Є нерозрахований банкет за {format_date_with_day(s.work_date)}.",
                    "action_label": "Вказати тариф",
                    "action_type": "set_banquet_rate",
                    "action_payload": {"employee_id": emp.id},
                })
        if bd.missing_banquet_guests:
            anomalies.append({
                "id": f"banquet_guests:{s.id}",
                "severity": "medium",
                "category": "missing_data",
                "title": f"Не вказано гостей на банкеті — {emp.full_name}",
                "description": f"Банкет за {format_date_with_day(s.work_date)} без кількості людей.",
                "action_label": "Відкрити картку",
                "action_type": "open_employee",
                "action_payload": {"employee_id": emp.id},
            })

    # A3. Непідключені активні працівники (вже завантажені в summaries!)
    active_employees = [s.employee for s in summaries if s.employee.is_active]
    for emp in active_employees:
        if emp.telegram_id is None:
            anomalies.append({
                "id": f"unlinked:{emp.id}",
                "severity": "info",
                "category": "onboarding",
                "title": f"{emp.full_name} ще не підключив Telegram",
                "description": "Надішли персональне посилання-запрошення з картки працівника.",
                "action_label": "Відкрити запрошення",
                "action_type": "open_employee",
                "action_payload": {"employee_id": emp.id},
            })

    # A4. Статистичні викиди тривалості змін (Iglewicz-Hoaglin Modified Z-Score |M| > 2.8)
    baseline_hours_raw = (
        await session.scalars(
            select(Shift.hours).where(Shift.work_date >= ref_date - timedelta(days=45))
        )
    ).all()
    baseline_hours = [float(h) for h in baseline_hours_raw]
    if len(baseline_hours) >= 4:
        med_h = _median(baseline_hours)
        mad_h = _mad(baseline_hours, med_h)
        for emp, bd in period_shift_breakdowns:
            sh = bd.shift
            h = float(sh.hours)
            is_outlier = False
            z_val = 0.0
            if mad_h > 1e-6:
                z_val = 0.6745 * (h - med_h) / mad_h
                if abs(z_val) > 2.8:
                    is_outlier = True
            elif abs(h - med_h) >= max(2.5, med_h * 0.35):
                is_outlier = True
            if h > 14.0 or h < 2.0:
                is_outlier = True

            if is_outlier:
                direction = "дуже довга" if h >= med_h else "незвично коротка"
                z_note = f" (Z={z_val:+.1f})" if abs(z_val) > 0.1 else ""
                anomalies.append({
                    "id": f"shift_outlier:{sh.id}",
                    "severity": "medium",
                    "category": "statistical_outlier",
                    "title": f"Нетипова зміна ({h:.2g} год) — {emp.full_name}",
                    "description": (
                        f"{format_date_with_day(sh.work_date)} ({sh.start_time}-{sh.end_time}) — "
                        f"{direction} при медіані {med_h:.1f} год{z_note}."
                    ),
                    "action_label": "Перевірити години",
                    "action_type": "open_employee",
                    "action_payload": {"employee_id": emp.id, "work_date": sh.work_date.isoformat()},
                })

    roles_breakdown = [
        {
            "role": rs["role"],
            "employees": rs["employees"],
            "shifts": rs["shifts"],
            "hours": q2_float(rs["hours"]),
            "payroll": q2_float(rs["payroll"]),
            "share_pct": (
                q2_float((rs["payroll"] / tot_payroll_d) * Decimal("100"))
                if tot_payroll_d > 0
                else 0.0
            ),
        }
        for rs in sorted(role_stats.values(), key=lambda item: item["payroll"], reverse=True)
    ]

    staff_rows = []
    for s in summaries:
        emp = s.employee
        avg_hourly_effective = (
            q2_float(to_dec(s.earned_period) / to_dec(s.total_hours))
            if s.total_hours > 0
            else 0.0
        )
        staff_rows.append({
            "id": emp.id,
            "full_name": emp.full_name,
            "role_name": emp.role.name if emp.role else "—",
            "is_active": emp.is_active,
            "connected": emp.telegram_id is not None,
            "telegram_username": emp.telegram_username,
            "hourly_rate": emp.hourly_rate,
            "revenue_percent": emp.revenue_percent,
            "banquet_rate_per_person": emp.banquet_rate_per_person,
            "has_card": bool(emp.card_number),
            "card_masked": mask_card(emp.card_number),
            "shifts_count": s.shifts_count,
            "total_hours": s.total_hours,
            "base_pay": s.base_pay,
            "percent_pay": s.percent_pay,
            "banquet_pay": s.banquet_pay,
            "bonuses": s.bonuses,
            "fines": s.fines,
            "earned_period": s.earned_period,
            "paid_period": s.paid_period,
            "balance_due": s.balance_due,
            "effective_per_hour": avg_hourly_effective,
            "has_warnings": bool(
                s.missing_banquet_rate_dates
                or s.missing_banquet_guests_dates
            ),
        })

    return {
        "period": {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        "kpis": {
            "total_payroll": q2_float(tot_payroll_d),
            "base_pay": q2_float(tot_base_d),
            "percent_pay": q2_float(tot_percent_d),
            "banquet_pay": q2_float(tot_banquet_d),
            "bonuses": q2_float(tot_bonuses_d),
            "fines": q2_float(tot_fines_d),
            "paid_period": q2_float(tot_paid_d),
            "total_balance_due": q2_float(tot_balance_due_d),
            "total_hours": q2_float(tot_hours_d),
            "shifts_count": tot_shifts,
            "cost_per_hour": cost_per_hour,
            "active_staff_count": sum(1 for e in active_employees),
            "connected_staff_count": sum(1 for e in active_employees if e.telegram_id is not None),
            "pending_payout_requests": pending_reqs_cnt,
            "approved_payout_requests": approved_reqs_cnt,
        },
        "daily_series": daily_series,
        "anomalies": anomalies,
        "roles_breakdown": roles_breakdown,
        "staff": staff_rows,
    }


async def compute_employee_dashboard(
    session: AsyncSession,
    emp: Employee,
    date_from: date,
    date_to: date,
    cal_year: int,
    cal_month: int,
    today: date,
) -> dict[str, Any]:
    """Дашборд працівника для Mini App: зарплата, календар місяця, проекція доходу, підказки часу."""
    rev_cache: dict[date, float] = {}
    summary = await get_employee_summary(session, emp, date_from, date_to, revenue_map_cache=rev_cache)

    # Дані календаря за обраний (cal_year, cal_month) — не дублюємо запити, якщо період співпадає
    _, last_day = calendar.monthrange(cal_year, cal_month)
    cal_start = date(cal_year, cal_month, 1)
    cal_end = date(cal_year, cal_month, last_day)
    if cal_start == date_from and cal_end == date_to:
        month_summary = summary
    else:
        month_summary = await get_employee_summary(session, emp, cal_start, cal_end, revenue_map_cache=rev_cache)

    bd_by_date = {bd.shift.work_date: bd for bd in month_summary.shift_breakdowns}
    calendar_days: list[dict[str, Any]] = []
    for day_num in range(1, last_day + 1):
        d = date(cal_year, cal_month, day_num)
        bd = bd_by_date.get(d)
        if bd is None:
            calendar_days.append({
                "date": d.isoformat(),
                "day": day_num,
                "weekday": d.weekday(),
                "is_today": d == today,
                "has_shift": False,
            })
        else:
            sh = bd.shift
            calendar_days.append({
                "date": d.isoformat(),
                "day": day_num,
                "weekday": d.weekday(),
                "is_today": d == today,
                "has_shift": True,
                "shift_id": sh.id,
                "start_time": sh.start_time,
                "end_time": sh.end_time,
                "hours": sh.hours,
                "is_banquet": sh.is_banquet,
                "banquet_guests": sh.banquet_guests,
                "base_pay": bd.base_pay,
                "percent_pay": bd.percent_pay,
                "banquet_pay": bd.banquet_pay,
                "total_pay": bd.total_pay,
                "revenue_missing": bd.revenue_missing,
                "missing_banquet_rate": bd.missing_banquet_rate,
                "missing_banquet_guests": bd.missing_banquet_guests,
            })

    # Розумна проекція доходу на кінець місяця (на основі середнього темпу у поточному місяці)
    projected_month_total = month_summary.earned_period
    avg_per_shift = (
        q2_float(to_dec(month_summary.earned_period) / Decimal(month_summary.shifts_count))
        if month_summary.shifts_count > 0
        else 0.0
    )
    avg_per_hour = (
        q2_float(to_dec(month_summary.earned_period) / to_dec(month_summary.total_hours))
        if month_summary.total_hours > 0
        else 0.0
    )
    if cal_year == today.year and cal_month == today.month and month_summary.shifts_count > 0:
        elapsed_days = max(1, today.day)
        remaining_days = max(0, last_day - today.day)
        daily_pace = to_dec(month_summary.earned_period) / Decimal(elapsed_days)
        projected_month_total = q2_float(
            to_dec(month_summary.earned_period) + (daily_pace * Decimal(remaining_days))
        )

    patterns = await predict_employee_shift_patterns(session, emp, today)

    recent_adjustments = (
        await session.scalars(
            select(Adjustment)
            .options(raiseload(Adjustment.employee))
            .where(Adjustment.employee_id == emp.id)
            .order_by(Adjustment.adj_date.desc(), Adjustment.id.desc())
            .limit(12)
        )
    ).all()
    recent_payouts = (
        await session.scalars(
            select(Payout)
            .options(raiseload(Payout.employee))
            .where(Payout.employee_id == emp.id)
            .order_by(Payout.paid_date.desc(), Payout.id.desc())
            .limit(12)
        )
    ).all()
    recent_requests = (
        await session.scalars(
            select(PayoutRequest)
            .options(raiseload(PayoutRequest.employee))
            .where(PayoutRequest.employee_id == emp.id)
            .order_by(PayoutRequest.created_at.desc(), PayoutRequest.id.desc())
            .limit(12)
        )
    ).all()

    return {
        "employee": {
            "id": emp.id,
            "full_name": emp.full_name,
            "role_name": emp.role.name if emp.role else "—",
            "hourly_rate": emp.hourly_rate,
            "revenue_percent": emp.revenue_percent,
            "banquet_rate_per_person": emp.banquet_rate_per_person,
            "card_digits": emp.card_number,
            "card_display": format_card_display(emp.card_number),
            "card_masked": mask_card(emp.card_number),
        },
        "period": {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        "summary": {
            "shifts_count": summary.shifts_count,
            "total_hours": summary.total_hours,
            "base_pay": summary.base_pay,
            "percent_pay": summary.percent_pay,
            "banquet_pay": summary.banquet_pay,
            "bonuses": summary.bonuses,
            "fines": summary.fines,
            "earned_period": summary.earned_period,
            "paid_period": summary.paid_period,
            "all_time_earned": summary.all_time_earned,
            "all_time_paid": summary.all_time_paid,
            "balance_due": summary.balance_due,
            "missing_banquet_rate_dates": [d.isoformat() for d in summary.missing_banquet_rate_dates],
            "missing_banquet_guests_dates": [d.isoformat() for d in summary.missing_banquet_guests_dates],
            "missing_revenue_dates": [d.isoformat() for d in summary.missing_revenue_dates],
            "shifts": [
                {
                    "id": bd.shift.id,
                    "work_date": bd.shift.work_date.isoformat(),
                    "date_label": format_date_with_day(bd.shift.work_date),
                    "start_time": bd.shift.start_time,
                    "end_time": bd.shift.end_time,
                    "hours": bd.shift.hours,
                    "base_pay": bd.base_pay,
                    "percent_pay": bd.percent_pay,
                    "daily_revenue": bd.daily_revenue,
                    "banquet_pay": bd.banquet_pay,
                    "is_banquet": bd.shift.is_banquet,
                    "banquet_guests": bd.shift.banquet_guests,
                    "total_pay": bd.total_pay,
                    "revenue_missing": bd.revenue_missing,
                    "missing_banquet_rate": bd.missing_banquet_rate,
                    "missing_banquet_guests": bd.missing_banquet_guests,
                }
                for bd in summary.shift_breakdowns
            ],
        },
        "calendar": {
            "year": cal_year,
            "month": cal_month,
            "month_earned": month_summary.earned_period,
            "month_hours": month_summary.total_hours,
            "month_shifts": month_summary.shifts_count,
            "projected_month_total": projected_month_total,
            "avg_per_shift": avg_per_shift,
            "avg_per_hour": avg_per_hour,
            "days": calendar_days,
        },
        "patterns": patterns,
        "adjustments": [
            {
                "id": a.id,
                "date": a.adj_date.isoformat(),
                "date_label": format_date_with_day(a.adj_date),
                "kind": a.kind,
                "amount": a.amount,
                "comment": a.comment,
            }
            for a in recent_adjustments
        ],
        "payouts": [
            {
                "id": p.id,
                "date": p.paid_date.isoformat(),
                "date_label": format_date_with_day(p.paid_date),
                "amount": p.amount,
                "comment": p.comment,
            }
            for p in recent_payouts
        ],
        "payout_requests": [
            serialize_payout_request(
                r,
                current_balance_due=summary.balance_due,
                reveal_card=True,
                employee_override=emp,
            )
            for r in recent_requests
        ],
    }
