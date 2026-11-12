from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import DailyRevenue, Employee, ScheduledRunLog, Shift
from app.services.access import get_all_owner_ids
from app.utils.datetime_utils import format_date_short, format_date_with_day
from app.utils.formatting import esc

logger = logging.getLogger(__name__)

OWNER_REVENUE_JOB = "owner_revenue_2300"
STAFF_MORNING_JOB = "staff_morning_0900"


async def _was_job_run_today(session: AsyncSession, job_key: str, target_date: date) -> bool:
    row = await session.get(ScheduledRunLog, job_key)
    return row is not None and row.last_run_date >= target_date


async def _mark_job_run(session: AsyncSession, job_key: str, target_date: date) -> None:
    row = await session.get(ScheduledRunLog, job_key)
    if row is None:
        session.add(ScheduledRunLog(job_key=job_key, last_run_date=target_date))
    elif row.last_run_date < target_date:
        row.last_run_date = target_date
    await session.commit()


async def run_owner_2300_reminder(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    target_date: date,
    force: bool = False,
) -> bool:
    if not force and await _was_job_run_today(session, OWNER_REVENUE_JOB, target_date):
        return False

    revenue = await session.scalar(
        select(DailyRevenue).where(DailyRevenue.work_date == target_date)
    )
    shifts_today = int(
        await session.scalar(
            select(func.count(Shift.id)).where(
                Shift.work_date == target_date,
                Shift.percent_snapshot > 0,
            )
        )
        or 0
    )

    sent_any = False
    if revenue is not None:
        await _mark_job_run(session, OWNER_REVENUE_JOB, target_date)
        return False

    if shifts_today <= 0:
        return False

    owner_ids = await get_all_owner_ids(session, settings)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💵 Внести касу за {format_date_short(target_date)}",
                    callback_data=f"own:rev_date:{target_date.isoformat()}",
                )
            ]
        ]
    )
    text = (
        "🌙 <b>Нагадування (23:00)</b>\n"
        f"За сьогодні (<b>{format_date_with_day(target_date)}</b>) працівники вже внесли змін: "
        f"<b>{shifts_today}</b>, але ще не вказано <b>касу дня</b> 💵\n"
        "Внеси виручку, щоб бот автоматично порахував відсотки ✨"
    )
    for owner_id in owner_ids:
        try:
            await bot.send_message(owner_id, text, reply_markup=kb)
            sent_any = True
        except Exception as exc:
            logger.warning("Не вдалося надіслати 23:00 нагадування власниці %s: %s", owner_id, exc)

    await _mark_job_run(session, OWNER_REVENUE_JOB, target_date)
    return sent_any


async def run_staff_0900_reminders(
    session: AsyncSession,
    bot: Bot,
    today: date,
    force: bool = False,
) -> int:
    if not force and await _was_job_run_today(session, STAFF_MORNING_JOB, today):
        return 0

    yesterday = today - timedelta(days=1)
    employees = (
        await session.scalars(
            select(Employee).where(
                Employee.is_active.is_(True),
                Employee.telegram_id.is_not(None),
            )
        )
    ).all()

    sent_count = 0
    for emp in employees:
        if emp.telegram_id is None:
            continue

        yesterday_shift = await session.scalar(
            select(Shift.id).where(Shift.employee_id == emp.id, Shift.work_date == yesterday)
        )
        missing_guests_shifts = (
            await session.scalars(
                select(Shift)
                .where(
                    Shift.employee_id == emp.id,
                    Shift.is_banquet.is_(True),
                    Shift.banquet_guests.is_(None),
                )
                .order_by(Shift.work_date.desc())
            )
        ).all()

        if yesterday_shift is not None and not missing_guests_shifts:
            continue

        lines = [f"☀️ <b>Доброго ранку, {esc(emp.full_name)}!</b>"]
        buttons: list[list[InlineKeyboardButton]] = []

        if yesterday_shift is None:
            lines.append(
                f"Якщо ти вчора (<b>{format_date_with_day(yesterday)}</b>) працював(ла), "
                "не забудь внести зміну в бота — це займе 2 натискання 🌸"
            )
            buttons.append([
                InlineKeyboardButton(
                    text=f"🕒 Внести зміну за {format_date_short(yesterday)}",
                    callback_data=f"emp:date:{yesterday.isoformat()}",
                )
            ])

        if missing_guests_shifts:
            dates_str = ", ".join(format_date_short(s.work_date) for s in missing_guests_shifts[:3])
            lines.append(f"👥 Не забудь вказати кількість людей на банкеті за: <b>{dates_str}</b>")
            for s in missing_guests_shifts[:2]:
                buttons.append([
                    InlineKeyboardButton(
                        text=f"👥 Внести людей ({format_date_short(s.work_date)})",
                        callback_data=f"emp:fill_guests:{s.id}",
                    )
                ])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        try:
            await bot.send_message(emp.telegram_id, "\n\n".join(lines), reply_markup=kb)
            sent_count += 1
        except Exception as exc:
            logger.warning("Не вдалося надіслати 09:00 нагадування працівнику %s: %s", emp.id, exc)

    await _mark_job_run(session, STAFF_MORNING_JOB, today)
    return sent_count


async def reminder_loop(
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
) -> None:
    """Фоновий планувальник щоденних нагадувань за київським часом (09:00 та 23:00)."""
    done_staff_date: date | None = None
    done_owner_date: date | None = None
    while True:
        try:
            now = datetime.now(settings.tz)
            today = now.date()
            if now.hour == 9 and now.minute < 30 and done_staff_date != today:
                async with session_maker() as session:
                    await run_staff_0900_reminders(session, bot, today)
                    if await _was_job_run_today(session, STAFF_MORNING_JOB, today):
                        done_staff_date = today
            elif now.hour == 23 and now.minute < 30 and done_owner_date != today:
                async with session_maker() as session:
                    await run_owner_2300_reminder(session, bot, settings, today)
                    if await _was_job_run_today(session, OWNER_REVENUE_JOB, today):
                        done_owner_date = today
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Помилка у планувальнику нагадувань: %s", exc)

        await asyncio.sleep(20)
