from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Employee, Payout, PayoutRequest
from app.services.payroll import get_employee_summary, q2_float, to_dec

_NON_DIGIT_CARD_RE = re.compile(r"[\s\-_]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_card_number(raw: str | None) -> str:
    """Очищує та перевіряє номер банківської картки (13–19 цифр, стандарт 16)."""
    cleaned = _NON_DIGIT_CARD_RE.sub("", (raw or "").strip())
    if not cleaned or not cleaned.isdigit():
        raise ValueError("Введи номер картки лише цифрами (16 цифр), наприклад 4149 4991 1234 5678")
    if not (13 <= len(cleaned) <= 19):
        raise ValueError("Номер картки має містити від 13 до 19 цифр (стандарт — 16 цифр)")
    return cleaned


def luhn_is_valid(digits: str) -> bool:
    if not digits or not digits.isdigit():
        return False
    total = 0
    reverse = digits[::-1]
    for idx, ch in enumerate(reverse):
        d = int(ch)
        if idx % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def format_card_display(digits: str | None) -> str | None:
    if not digits:
        return None
    d = _NON_DIGIT_CARD_RE.sub("", digits)
    return " ".join(d[i : i + 4] for i in range(0, len(d), 4))


def mask_card(digits: str | None) -> str | None:
    if not digits:
        return None
    d = _NON_DIGIT_CARD_RE.sub("", digits)
    if len(d) <= 4:
        return d
    if len(d) >= 12:
        return f"{d[:4]} •••• •••• {d[-4:]}"
    return f"•••• {d[-4:]}"


def _fmt_dt(dt: datetime | None, tz: ZoneInfo | None = None) -> str | None:
    if dt is None:
        return None
    val = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    if tz is not None:
        val = val.astimezone(tz)
    return val.strftime("%d.%m.%Y %H:%M")


async def save_employee_card(
    session: AsyncSession,
    emp: Employee,
    raw_card: str | None,
) -> str:
    clean = normalize_card_number(raw_card)
    emp.card_number = clean
    await session.commit()
    await session.refresh(emp)
    return clean


async def create_payout_request(
    session: AsyncSession,
    emp: Employee,
    amount: float,
    comment: str | None = None,
    raw_card: str | None = None,
) -> PayoutRequest:
    if raw_card is not None and raw_card.strip():
        emp.card_number = normalize_card_number(raw_card)

    card = emp.card_number
    if not card:
        raise ValueError("Спочатку вкажи свій номер картки 💳, щоб Аня могла швидко зробити переказ")

    existing_pending = await session.scalar(
        select(PayoutRequest).where(
            PayoutRequest.employee_id == emp.id,
            PayoutRequest.status == "pending",
        )
    )
    if existing_pending is not None:
        raise ValueError("У тебе вже є активний запит на виплату ⏳ Дочекайся рішення Ані або скасуй попередній.")

    amt_dec = to_dec(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amt_dec <= 0:
        raise ValueError("Сума запиту має бути більшою за 0")
    if amt_dec > Decimal("1000000.00"):
        raise ValueError("Занадто велика сума запиту")

    cleaned_comment = (comment or "").strip()
    if cleaned_comment in {"", "-", "—"}:
        cleaned_comment = None
    elif len(cleaned_comment) > 240:
        cleaned_comment = cleaned_comment[:240]

    req = PayoutRequest(
        employee_id=emp.id,
        amount=float(amt_dec),
        comment=cleaned_comment,
        card_snapshot=card,
        status="pending",
    )
    session.add(req)
    await session.commit()
    await session.refresh(req)
    return req


async def accept_payout_request(
    session: AsyncSession,
    req_id: int,
    reviewer_tg_id: int,
) -> PayoutRequest:
    req = await session.get(PayoutRequest, req_id)
    if req is None:
        raise ValueError("Запит на виплату не знайдено")
    if req.status == "approved":
        return req
    if req.status != "pending":
        raise ValueError("Цей запит вже оброблено або скасовано")

    emp = await session.get(Employee, req.employee_id)
    latest_card = (emp.card_number if emp and emp.card_number else req.card_snapshot)
    req.card_snapshot = latest_card
    req.status = "approved"
    req.reviewed_by_tg_id = reviewer_tg_id
    req.reviewed_at = utcnow()
    await session.commit()
    await session.refresh(req)
    return req


async def reject_payout_request(
    session: AsyncSession,
    req_id: int,
    reviewer_tg_id: int,
    reason: str | None = None,
) -> PayoutRequest:
    req = await session.get(PayoutRequest, req_id)
    if req is None:
        raise ValueError("Запит на виплату не знайдено")
    if req.status in {"paid", "cancelled"}:
        raise ValueError("Цей запит вже закрито")
    if req.status == "rejected":
        return req

    note = (reason or "").strip()[:240] or None
    req.status = "rejected"
    req.reviewed_by_tg_id = reviewer_tg_id
    req.review_note = note
    req.reviewed_at = utcnow()
    await session.commit()
    await session.refresh(req)
    return req


async def cancel_payout_request(
    session: AsyncSession,
    req_id: int,
    employee_id: int,
) -> PayoutRequest:
    req = await session.get(PayoutRequest, req_id)
    if req is None or req.employee_id != employee_id:
        raise ValueError("Запит не знайдено")
    if req.status != "pending":
        raise ValueError("Можна скасувати лише запит, який ще на розгляді")
    req.status = "cancelled"
    await session.commit()
    await session.refresh(req)
    return req


async def mark_payout_request_paid(
    session: AsyncSession,
    req_id: int,
    actor_tg_id: int,
    paid_date: date,
    actual_amount: float | None = None,
    comment: str | None = None,
) -> tuple[PayoutRequest, Payout, float]:
    req = await session.get(PayoutRequest, req_id)
    if req is None:
        raise ValueError("Запит на виплату не знайдено")
    if req.status == "paid" or req.payout_id is not None:
        raise ValueError("Цей запит вже оплачено")
    if req.status not in {"pending", "approved"}:
        raise ValueError("Неможливо оплатити відхилений чи скасований запит")

    emp = await session.get(Employee, req.employee_id)
    if emp is None:
        raise ValueError("Працівника не знайдено")

    amt_val = req.amount if actual_amount is None else actual_amount
    amt_dec = to_dec(amt_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amt_dec <= 0:
        raise ValueError("Сума виплати має бути більшою за 0")

    final_comment = (comment or "").strip() if comment is not None else (req.comment or "за запитом на виплату")
    if final_comment in {"", "-", "—"}:
        final_comment = "за запитом на виплату"
    final_comment = final_comment[:240]

    payout = Payout(
        employee_id=emp.id,
        paid_date=paid_date,
        amount=float(amt_dec),
        comment=final_comment,
    )
    session.add(payout)
    await session.flush()

    now = utcnow()
    if req.reviewed_at is None:
        req.reviewed_at = now
        req.reviewed_by_tg_id = actor_tg_id
    if not req.card_snapshot and emp.card_number:
        req.card_snapshot = emp.card_number
    req.status = "paid"
    req.payout_id = payout.id
    req.paid_at = now
    await session.commit()
    await session.refresh(req)

    summary = await get_employee_summary(session, emp, paid_date.replace(day=1), paid_date)
    return req, payout, summary.balance_due


def serialize_payout_request(
    req: PayoutRequest,
    current_balance_due: float | None = None,
    reveal_card: bool = False,
    tz: ZoneInfo | None = None,
    employee_override: Employee | None = None,
) -> dict[str, Any]:
    emp = employee_override if employee_override is not None else req.employee
    card_digits = (emp.card_number if emp and emp.card_number else None) or req.card_snapshot
    item: dict[str, Any] = {
        "id": req.id,
        "employee_id": req.employee_id,
        "employee_name": emp.full_name if emp else f"#{req.employee_id}",
        "role_name": emp.role.name if emp and emp.role else "—",
        "amount": q2_float(to_dec(req.amount)),
        "comment": req.comment,
        "status": req.status,
        "review_note": req.review_note,
        "payout_id": req.payout_id,
        "created_at_label": _fmt_dt(req.created_at, tz),
        "reviewed_at_label": _fmt_dt(req.reviewed_at, tz),
        "paid_at_label": _fmt_dt(req.paid_at, tz),
        "has_card": bool(card_digits),
        "card_masked": mask_card(card_digits),
        "card_luhn_ok": luhn_is_valid(card_digits) if card_digits else None,
    }
    if current_balance_due is not None:
        item["current_balance_due"] = q2_float(to_dec(current_balance_due))
    if reveal_card and card_digits:
        item["card_digits"] = card_digits
        item["card_display"] = format_card_display(card_digits)
    return item
