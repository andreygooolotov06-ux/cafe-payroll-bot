from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

WEEKDAYS_SHORT_UK = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
WEEKDAYS_FULL_UK = (
    "Понеділок",
    "Вівторок",
    "Середа",
    "Четвер",
    "П'ятниця",
    "Субота",
    "Неділя",
)

_DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")
_TIME_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*$")
_DATE_RANGE_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*$")


def today_local(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def format_date_short(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def format_date_with_day(d: date, full: bool = False) -> str:
    day_name = WEEKDAYS_FULL_UK[d.weekday()] if full else WEEKDAYS_SHORT_UK[d.weekday()]
    return f"{d.strftime('%d.%m.%Y')} ({day_name})"


def parse_date_input(raw: str) -> date:
    match = _DATE_RE.match(raw)
    if not match:
        raise ValueError("Формат дати має бути ДД.ММ.РРРР, наприклад 12.11.2026")
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError("Такої дати не існує у календарі. Перевір число та місяць") from exc


def parse_date_range_input(raw: str) -> tuple[date, date]:
    match = _DATE_RANGE_RE.match(raw)
    if match:
        d1 = parse_date_input(match.group(1))
        d2 = parse_date_input(match.group(2))
        if d1 > d2:
            d1, d2 = d2, d1
        return d1, d2

    # Можна ввести і одну дату, тоді період = 1 день
    single = parse_date_input(raw)
    return single, single


def parse_time_range(raw: str) -> tuple[str, str, float]:
    """Парсить інтервал ГГ:ХХ-ГГ:ХХ і повертає (start_hhmm, end_hhmm, hours).

    Підтримує нічні зміни з переходом через північ (наприклад, 18:00-02:00 -> 8.0 год).
    """
    match = _TIME_RANGE_RE.match(raw)
    if not match:
        raise ValueError("Введи час у форматі ГГ:ХХ-ГГ:ХХ, наприклад 10:00-22:00")

    sh, sm, eh, em = (int(match.group(i)) for i in range(1, 5))
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        raise ValueError("Години мають бути від 00 до 23, хвилини — від 00 до 59")

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    if end_minutes == start_minutes:
        raise ValueError("Час початку і кінця не можуть збігатися")

    if end_minutes < start_minutes:
        end_minutes += 24 * 60

    diff_minutes = end_minutes - start_minutes
    if diff_minutes > 24 * 60:
        raise ValueError("Зміна не може тривати більше 24 годин")

    hours_dec = (Decimal(diff_minutes) / Decimal("60")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    hours = float(hours_dec)
    return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}", hours


def get_period_bounds(preset: str, ref_date: date) -> tuple[date, date, str]:
    if preset == "today":
        return ref_date, ref_date, f"Сьогодні ({format_date_with_day(ref_date)})"
    if preset == "yesterday":
        y = ref_date - timedelta(days=1)
        return y, y, f"Вчора ({format_date_with_day(y)})"
    if preset == "week":
        start = ref_date - timedelta(days=ref_date.weekday())
        return start, ref_date, f"Цей тиждень ({format_date_short(start)}–{format_date_short(ref_date)})"
    if preset == "two_weeks":
        start = ref_date - timedelta(days=13)
        return start, ref_date, f"Останні 14 днів ({format_date_short(start)}–{format_date_short(ref_date)})"
    if preset == "month":
        start = ref_date.replace(day=1)
        return start, ref_date, f"Цей місяць ({format_date_short(start)}–{format_date_short(ref_date)})"
    if preset == "last_month":
        first_this = ref_date.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return start, end, f"Минулий місяць ({format_date_short(start)}–{format_date_short(end)})"
    raise ValueError(f"Невідомий період: {preset}")
