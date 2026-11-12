from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.services.payroll import EmployeePeriodSummary, q2_float, to_dec
from app.utils.datetime_utils import format_date_short, format_date_with_day


def build_payroll_excel(
    summaries: list[EmployeePeriodSummary],
    date_from: date,
    date_to: date,
) -> bytes:
    wb = Workbook()

    # Аркуш 1: Загальна звітність (без групування по посадах)
    ws = wb.active
    ws.title = "Загальна звітність"

    header_fill = PatternFill(start_color="2B547E", end_color="2B547E", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    bold_font = Font(bold=True, size=11)
    total_fill = PatternFill(start_color="EEF3F8", end_color="EEF3F8", fill_type="solid")

    ws.append([f"Зарплатна звітність кафе за період: {format_date_short(date_from)} – {format_date_short(date_to)}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])

    headers = [
        "Працівник",
        "Змін",
        "Годин",
        "За годинами (грн)",
        "% від каси (грн)",
        "Банкети (грн)",
        "Премії (грн)",
        "Штрафи (грн)",
        "Нараховано за період (грн)",
        "Виплачено за період (грн)",
        "Залишок до видачі (грн)",
        "Примітки",
    ]
    ws.append(headers)
    header_row = 3
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=header_row, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    totals = {
        "shifts": 0,
        "hours": Decimal("0.00"),
        "base": Decimal("0.00"),
        "percent": Decimal("0.00"),
        "banquet": Decimal("0.00"),
        "bonuses": Decimal("0.00"),
        "fines": Decimal("0.00"),
        "earned": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "balance": Decimal("0.00"),
    }

    for summary in summaries:
        name = summary.employee.full_name
        if not summary.employee.is_active:
            name += " (видалений)"

        notes: list[str] = []
        if summary.missing_banquet_rate_dates:
            notes.append("Не вказано тариф банкету")
        if summary.missing_banquet_guests_dates:
            notes.append("Не вказано людей на банкеті")
        if summary.missing_revenue_dates:
            notes.append("Не внесена каса за частину днів")

        ws.append([
            name,
            summary.shifts_count,
            q2_float(to_dec(summary.total_hours)),
            q2_float(to_dec(summary.base_pay)),
            q2_float(to_dec(summary.percent_pay)),
            q2_float(to_dec(summary.banquet_pay)),
            q2_float(to_dec(summary.bonuses)),
            q2_float(to_dec(summary.fines)),
            q2_float(to_dec(summary.earned_period)),
            q2_float(to_dec(summary.paid_period)),
            q2_float(to_dec(summary.balance_due)),
            "; ".join(notes) if notes else "—",
        ])

        totals["shifts"] += summary.shifts_count
        totals["hours"] += to_dec(summary.total_hours)
        totals["base"] += to_dec(summary.base_pay)
        totals["percent"] += to_dec(summary.percent_pay)
        totals["banquet"] += to_dec(summary.banquet_pay)
        totals["bonuses"] += to_dec(summary.bonuses)
        totals["fines"] += to_dec(summary.fines)
        totals["earned"] += to_dec(summary.earned_period)
        totals["paid"] += to_dec(summary.paid_period)
        totals["balance"] += to_dec(summary.balance_due)

    total_row_idx = ws.max_row + 1
    ws.append([
        "РАЗОМ",
        totals["shifts"],
        q2_float(totals["hours"]),
        q2_float(totals["base"]),
        q2_float(totals["percent"]),
        q2_float(totals["banquet"]),
        q2_float(totals["bonuses"]),
        q2_float(totals["fines"]),
        q2_float(totals["earned"]),
        q2_float(totals["paid"]),
        q2_float(totals["balance"]),
        "",
    ])
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=total_row_idx, column=col_idx)
        cell.font = bold_font
        cell.fill = total_fill

    # Аркуш 2: Деталізація змін
    ws_shifts = wb.create_sheet(title="Деталізація змін")
    shift_headers = [
        "Дата",
        "Працівник",
        "Час",
        "Годин",
        "Ставка/год",
        "За годинами (грн)",
        "Каса дня (грн)",
        "% ставка",
        "% від каси (грн)",
        "Банкет (людей)",
        "Тариф банкету",
        "Банкет (грн)",
        "Всього за зміну (грн)",
        "Статус даних",
    ]
    ws_shifts.append(shift_headers)
    for col_idx in range(1, len(shift_headers) + 1):
        cell = ws_shifts.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    all_shift_rows: list[tuple[date, str, list[object]]] = []
    for summary in summaries:
        emp_name = summary.employee.full_name
        if not summary.employee.is_active:
            emp_name += " (видалений)"
        for b in summary.shift_breakdowns:
            s = b.shift
            issues: list[str] = []
            if b.missing_banquet_rate:
                issues.append("Звернись до Ані (немає тарифу банкету)")
            if b.missing_banquet_guests:
                issues.append("Не вказано кількість людей")
            if b.revenue_missing:
                issues.append("Не внесена каса дня")

            all_shift_rows.append((
                s.work_date,
                emp_name,
                [
                    format_date_with_day(s.work_date),
                    emp_name,
                    f"{s.start_time}-{s.end_time}",
                    round(s.hours, 2),
                    round(s.hourly_rate_snapshot, 2),
                    round(b.base_pay, 2),
                    round(b.daily_revenue, 2) if b.daily_revenue is not None else "Не внесена",
                    round(s.percent_snapshot, 2),
                    round(b.percent_pay, 2),
                    s.banquet_guests if s.is_banquet and s.banquet_guests is not None else ("Не вказано" if s.is_banquet else "—"),
                    round(s.banquet_rate_snapshot, 2) if s.is_banquet and s.banquet_rate_snapshot is not None else ("Не вказано" if s.is_banquet else "—"),
                    round(b.banquet_pay, 2),
                    round(b.total_pay, 2),
                    "; ".join(issues) if issues else "ОК",
                ],
            ))

    all_shift_rows.sort(key=lambda item: (item[0], item[1]))
    for _, _, row_vals in all_shift_rows:
        ws_shifts.append(row_vals)

    for sheet in (ws, ws_shifts):
        for col in sheet.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val = "" if cell.value is None else str(cell.value)
                if cell.row == 1 and sheet == ws:
                    continue
                max_len = max(max_len, len(val))
            sheet.column_dimensions[col_letter].width = min(max(max_len + 4, 12), 36)

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
