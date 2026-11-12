from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None,
) -> None:
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            link_preview_options=link_preview_options,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def parse_positive_float(raw: str, allow_zero: bool = False) -> float:
    cleaned = raw.strip().replace(",", ".").replace(" ", "")
    try:
        dec = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Введи коректне число, наприклад 120 або 120.5") from exc
    if not dec.is_finite():
        raise ValueError("Введи коректне число, наприклад 120 або 120.5")
    if allow_zero:
        if dec < 0:
            raise ValueError("Значення не може бути від'ємним")
    else:
        if dec <= 0:
            raise ValueError("Значення має бути більшим за 0")
    return float(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_positive_int(raw: str) -> int:
    cleaned = raw.strip()
    if not cleaned.isdigit():
        raise ValueError("Введи ціле додатне число, наприклад 15")
    value = int(cleaned)
    if value <= 0:
        raise ValueError("Кількість має бути більшою за 0")
    return value
