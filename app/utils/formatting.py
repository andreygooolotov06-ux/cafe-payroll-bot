from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _q2_dec(value: float | int | Decimal) -> Decimal:
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")
    return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def fmt_money(value: float | int | Decimal) -> str:
    rounded = _q2_dec(value)
    if abs(rounded) < Decimal("0.005"):
        rounded = Decimal("0.00")
    if rounded == rounded.to_integral_value():
        formatted = f"{int(rounded):,}".replace(",", " ")
    else:
        formatted = f"{rounded:,.2f}".replace(",", " ")
    return f"{formatted} грн"


def fmt_hours(value: float | int | Decimal) -> str:
    rounded = _q2_dec(value)
    if rounded == rounded.to_integral_value():
        return f"{int(rounded)} год"
    return f"{rounded:.2f}".rstrip("0").rstrip(".") + " год"


def fmt_percent(value: float | int | Decimal) -> str:
    rounded = _q2_dec(value)
    if rounded == rounded.to_integral_value():
        return f"{int(rounded)}%"
    return f"{rounded:.2f}".rstrip("0").rstrip(".") + "%"
