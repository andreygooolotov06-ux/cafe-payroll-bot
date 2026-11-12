from __future__ import annotations

import base64
import calendar
import gzip
import hashlib
import json
import logging
import re
import secrets
import string
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import raiseload

from app.config import Settings
from app.database.models import (
    Adjustment,
    DailyRevenue,
    Employee,
    IdempotencyRecord,
    Payout,
    PayoutRequest,
    Role,
    Shift,
)
from app.services.access import (
    ADMIN_PERM_LABELS,
    ActorContext,
    ActorRole,
    AdminPerms,
    bind_invite_code,
    get_admin_perms,
    is_admin_role_name,
    resolve_actor,
    toggle_admin_perm,
)
from app.services.analytics import compute_employee_dashboard, compute_owner_analytics
from app.services.excel_export import build_payroll_excel
from app.services.notifications import (
    notify_adjustment_created,
    notify_admin_perms_changed,
    notify_employee_payout_request_accepted,
    notify_employee_payout_request_rejected,
    notify_owners_admin_action,
    notify_owners_employee_connected,
    notify_owners_missing_banquet_rate,
    notify_owners_payout_request_created,
    notify_payout_created,
    notify_rate_changed,
    notify_revenue_saved,
    notify_role_changed,
    notify_shift_edited_by_manager,
)
from app.services.payout_requests import (
    accept_payout_request,
    cancel_payout_request,
    create_payout_request,
    format_card_display,
    luhn_is_valid,
    mark_payout_request_paid,
    mask_card,
    reject_payout_request,
    save_employee_card,
    serialize_payout_request,
)
from app.services.payroll import (
    compute_shift_breakdown,
    delete_employee_safely,
    get_all_employees_summary,
    get_employee_summary,
    get_recent_revenues,
    q2_float,
    set_employee_banquet_rate,
    to_dec,
)
from app.services.tunnel import miniapp_runtime
from app.utils.datetime_utils import (
    WEEKDAYS_SHORT_UK,
    format_date_short,
    format_date_with_day,
    get_period_bounds,
    parse_date_input,
    parse_time_range,
    today_local,
)
from app.webapp.auth import InitDataError, verify_telegram_init_data

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_INVITE_ALPHABET = string.ascii_letters + string.digits
_COMPRESSIBLE_CONTENT_TYPES = frozenset({
    "text/html",
    "text/css",
    "text/plain",
    "text/javascript",
    "application/javascript",
    "application/json",
    "image/svg+xml",
    "application/manifest+json",
})
_JS_REGEX_PREV_KEYWORDS = frozenset({
    "return",
    "throw",
    "case",
    "default",
    "in",
    "of",
    "typeof",
    "void",
    "delete",
    "new",
    "else",
    "await",
    "yield",
    "do",
    "instanceof",
})


def _minify_css(src: str) -> str:
    """Безпечна мініфікація CSS без порушення селекторів і виразів."""
    out: list[str] = []
    i = 0
    n = len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                out.append(src[i])
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    cleaned = re.sub(r"\s+", " ", "".join(out)).strip()
    cleaned = re.sub(r"\s*([{};,])\s*", r"\1", cleaned)
    cleaned = re.sub(r":\s+", ":", cleaned)
    cleaned = cleaned.replace(";}", "}")
    return cleaned


def _minify_js(src: str) -> str:
    """Лексер-мініфікатор JS (зберігає рядки, template-літерали, regex-літерали і ASI)."""
    out: list[str] = []
    n = len(src)
    i = 0
    # Стек режимів: ("code", brace_depth) | ("template",)
    stack: list[tuple[str, ...]] = [("code", "0")]
    brace_depths = [0]
    last_tok = ""
    last_kind = "start"  # "start" | "op" | "val" | "word"

    def _can_start_regex() -> bool:
        if last_kind in ("start", "op"):
            return True
        if last_kind == "word" and last_tok in _JS_REGEX_PREV_KEYWORDS:
            return True
        return False

    while i < n:
        mode = stack[-1][0]
        if mode == "template":
            ch = src[i]
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                out.append(src[i])
            elif ch == "$" and i + 1 < n and src[i + 1] == "{":
                i += 1
                out.append("{")
                stack.append(("code",))
                brace_depths.append(1)
                last_kind = "op"
                last_tok = "${"
            elif ch == "`":
                stack.pop()
                last_kind = "val"
                last_tok = "`"
            i += 1
            continue

        ch = src[i]

        # Однорядковий коментар //
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            nl = src.find("\n", i + 2)
            if nl == -1:
                break
            i = nl
            continue

        # Багаторядковий коментар /* ... */
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            chunk = src[i:] if end == -1 else src[i : end + 2]
            out.append("\n" if "\n" in chunk else " ")
            i = n if end == -1 else end + 2
            continue

        # Рядкові літерали '...' / "..."
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = src[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    i += 1
                    out.append(src[i])
                elif c == quote:
                    break
                i += 1
            last_kind = "val"
            last_tok = quote
            i += 1
            continue

        # Шаблонний літерал `...`
        if ch == "`":
            out.append(ch)
            stack.append(("template",))
            i += 1
            continue

        # Regex літерал /.../flags
        if ch == "/" and _can_start_regex():
            out.append(ch)
            i += 1
            in_class = False
            while i < n:
                c = src[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    i += 1
                    out.append(src[i])
                elif c == "[":
                    in_class = True
                elif c == "]" and in_class:
                    in_class = False
                elif c == "/" and not in_class:
                    break
                elif c == "\n":
                    break
                i += 1
            i += 1
            while i < n and src[i].isalpha():
                out.append(src[i])
                i += 1
            last_kind = "val"
            last_tok = "/"
            continue

        # Пробіли та переноси рядків
        if ch in (" ", "\t", "\r", "\n"):
            has_nl = False
            while i < n and src[i] in (" ", "\t", "\r", "\n"):
                if src[i] == "\n":
                    has_nl = True
                i += 1
            if has_nl:
                if out and out[-1] in (" ", "\t"):
                    out.pop()
                if out and out[-1] != "\n":
                    out.append("\n")
            else:
                if out and out[-1] not in (" ", "\t", "\n"):
                    out.append(" ")
            continue

        # Відслідковуємо вкладеність ${ ... } у шаблонних літералах
        if ch == "{":
            brace_depths[-1] += 1
            out.append(ch)
            last_kind = "op"
            last_tok = "{"
            i += 1
            continue
        if ch == "}":
            brace_depths[-1] -= 1
            out.append(ch)
            if len(stack) > 1 and brace_depths[-1] == 0:
                brace_depths.pop()
                stack.pop()
            else:
                last_kind = "op"
                last_tok = "}"
            i += 1
            continue

        # Ідентифікатори / ключові слова
        if ch.isalpha() or ch in ("_", "$"):
            prev_tok = last_tok
            start = i
            i += 1
            while i < n and (src[i].isalnum() or src[i] in ("_", "$")):
                i += 1
            word = src[start:i]
            out.append(word)
            last_tok = word
            last_kind = "val" if prev_tok == "." else "word"
            continue

        # Числа
        if ch.isdigit():
            start = i
            while i < n and (src[i].isalnum() or src[i] in (".", "_")):
                i += 1
            out.append(src[start:i])
            last_tok = "0"
            last_kind = "val"
            continue

        # Подвійні оператори ++ / -- / =>
        if ch in ("+", "-") and i + 1 < n and src[i + 1] == ch:
            op = src[i : i + 2]
            out.append(op)
            last_tok = op
            last_kind = "val"
            i += 2
            continue

        out.append(ch)
        if ch in (")", "]"):
            last_kind = "val"
        else:
            last_kind = "op"
        last_tok = ch
        i += 1

    text = "".join(out)
    # Прибираємо порожні рядки та зайві пробіли на межах рядків
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    joined = "\n".join(lines)
    # Захист від закриття </script> усередині JS-літералів/рядків
    joined = re.sub(r"</script", "<\\/script", joined, flags=re.IGNORECASE)
    return joined


def _build_bundled_index_html(static_dir: Path) -> tuple[bytes, bytes, str]:
    """Збирає єдиний self-contained index.html з інлайновим CSS/JS/SVG та без блокуючих шрифтів."""
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    css_raw = (static_dir / "styles.css").read_text(encoding="utf-8")
    js_raw = (static_dir / "app.js").read_text(encoding="utf-8")
    svg_raw = (static_dir / "icon.svg").read_text(encoding="utf-8")

    css_min = _minify_css(css_raw)
    js_min = _minify_js(js_raw)
    svg_compact = re.sub(r"<!--.*?-->", "", svg_raw, flags=re.DOTALL)
    svg_compact = re.sub(r"\s+", " ", svg_compact).replace("> <", "><").strip()
    icon_b64 = base64.b64encode(svg_compact.encode("utf-8")).decode("ascii")
    icon_data_uri = f"data:image/svg+xml;base64,{icon_b64}"

    svg_boot = svg_compact.replace(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">',
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="72" height="72" aria-hidden="true">',
        1,
    )

    # Прибираємо зовнішні запити на шрифти, маніфест і розрізнені статичні файли
    html = re.sub(r'<link[^>]+fonts\.(?:google|gstatic)[^>]*>\s*', "", html, flags=re.IGNORECASE)
    html = re.sub(r'<link[^>]+rel=["\']manifest["\'][^>]*>\s*', "", html, flags=re.IGNORECASE)
    html = re.sub(
        r'<link[^>]+href=["\']/icon\.svg["\'][^>]*>',
        lambda m: m.group(0).replace("/icon.svg", icon_data_uri),
        html,
        flags=re.IGNORECASE,
    )
    html = html.replace('<img src="/icon.svg" alt="" width="72" height="72" />', svg_boot)
    html = re.sub(
        r'<script[^>]+src=["\']https://telegram\.org/js/telegram-web-app\.js["\'][^>]*></script>\s*',
        "",
        html,
        flags=re.IGNORECASE,
    )
    html = re.sub(
        r'<link[^>]+href=["\']/static/styles\.css["\'][^>]*>',
        lambda _: f"<style>{css_min}</style>",
        html,
        flags=re.IGNORECASE,
    )
    html = re.sub(
        r'<script[^>]+src=["\']/static/app\.js["\'][^>]*></script>',
        lambda _: (
            '<script src="https://telegram.org/js/telegram-web-app.js"></script>\n'
            f"  <script>{js_min}</script>"
        ),
        html,
        flags=re.IGNORECASE,
    )

    raw_bytes = html.encode("utf-8")
    gz_bytes = gzip.compress(raw_bytes, compresslevel=6)
    etag = f'"{hashlib.sha256(raw_bytes).hexdigest()[:16]}"'
    return raw_bytes, gz_bytes, etag


def _json_error(message: str, status: int = 400, code: str = "bad_request") -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _parse_money(value: Any, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("Введи коректне число")
    if isinstance(value, (int, float)):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip().replace(",", ".").replace(" ", "")
    else:
        raise ValueError("Введи коректне число")
    if not raw:
        raise ValueError("Введи коректне число")
    try:
        dec = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Введи коректне число, наприклад 120 або 120.5") from exc
    if not dec.is_finite():
        raise ValueError("Введи коректне число")
    if allow_zero:
        if dec < 0:
            raise ValueError("Значення не може бути від'ємним")
    else:
        if dec <= 0:
            raise ValueError("Значення має бути більшим за 0")
    return float(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _parse_any_date(raw: str | None) -> date:
    s = (raw or "").strip()
    if not s:
        raise ValueError("Дата не вказана")
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return date.fromisoformat(s)
    return parse_date_input(s)


def _parse_period_params(query: Any, today: date) -> tuple[date, date]:
    preset = (query.get("preset") or "").strip()
    raw_from = (query.get("from") or "").strip()
    raw_to = (query.get("to") or "").strip()
    if raw_from and raw_to:
        d1 = _parse_any_date(raw_from)
        d2 = _parse_any_date(raw_to)
        if d1 > d2:
            d1, d2 = d2, d1
        return d1, d2
    if preset and preset != "custom":
        d1, d2, _ = get_period_bounds(preset, today)
        return d1, d2
    d1, d2, _ = get_period_bounds("month", today)
    return d1, d2


def _perms_dict(perms: AdminPerms) -> dict[str, bool]:
    return {
        "can_enter_revenue": perms.can_enter_revenue,
        "can_view_staff_shifts": perms.can_view_staff_shifts,
        "can_edit_hours": perms.can_edit_hours,
        "can_view_salaries_reports": perms.can_view_salaries_reports,
        "can_adjust_money": perms.can_adjust_money,
        "can_payout": perms.can_payout,
        "can_manage_staff": perms.can_manage_staff,
        "can_open_staff_tab": perms.can_open_staff_tab,
    }


def _actor_permissions_payload(actor: ActorContext) -> dict[str, bool]:
    if actor.is_owner:
        return {
            "can_enter_revenue": True,
            "can_view_staff_shifts": True,
            "can_edit_hours": True,
            "can_view_salaries_reports": True,
            "can_adjust_money": True,
            "can_payout": True,
            "can_manage_staff": True,
            "can_open_staff_tab": True,
            "can_configure_admin": True,
            "has_personal_cabinet": False,
        }
    if actor.is_admin:
        base = _perms_dict(actor.admin_perms)
        base["can_configure_admin"] = False
        base["has_personal_cabinet"] = True
        return base
    return {
        "can_enter_revenue": False,
        "can_view_staff_shifts": False,
        "can_edit_hours": False,
        "can_view_salaries_reports": False,
        "can_adjust_money": False,
        "can_payout": False,
        "can_manage_staff": False,
        "can_open_staff_tab": False,
        "can_configure_admin": False,
        "has_personal_cabinet": True,
    }


async def _get_bot_username(app: web.Application, bot: Bot) -> str | None:
    cached = app.get("bot_username")
    if cached:
        return str(cached)
    try:
        me = await bot.me()
        if me.username:
            app["bot_username"] = me.username
            return me.username
    except Exception:
        pass
    return None


async def _ensure_invite_link(
    app: web.Application,
    session: AsyncSession,
    bot: Bot,
    emp: Employee,
) -> str | None:
    if emp.telegram_id is not None:
        return None
    if not emp.invite_code:
        emp.invite_code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(10))
        await session.commit()
        await session.refresh(emp)
    uname = await _get_bot_username(app, bot)
    if not uname:
        return None
    return f"https://t.me/{uname}?start={emp.invite_code}"


async def _get_or_create_role(session: AsyncSession, name: str) -> Role:
    cleaned = name.strip()
    existing = await session.scalar(select(Role).where(Role.name.ilike(cleaned)))
    if existing is not None:
        return existing
    role = Role(name=cleaned)
    session.add(role)
    await session.flush()
    return role


async def _authenticate_request(
    request: web.Request,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    allow_query_init_data: bool = False,
) -> tuple[ActorContext | None, web.Response | None]:
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not init_data and allow_query_init_data:
        init_data = request.query.get("initData", "").strip()

    try:
        tg_user = verify_telegram_init_data(init_data, settings.bot_token)
    except InitDataError as exc:
        return None, _json_error(str(exc), status=401, code="unauthorized_init_data")

    actor = await resolve_actor(
        session=session,
        telegram_id=tg_user.telegram_id,
        owner_ids=settings.owner_ids,
        owner_usernames=settings.owner_usernames,
        telegram_username=tg_user.username,
    )

    # Підтримка автоприв'язки по start_param (якщо хтось відкрив Mini App за інвайт-посиланням)
    start_param = tg_user.start_param or request.headers.get("X-Start-Param", "").strip() or None
    if actor.role == ActorRole.UNAUTHORIZED and start_param:
        ok, linked_emp, reason = await bind_invite_code(
            session=session,
            invite_code=start_param,
            telegram_id=tg_user.telegram_id,
            telegram_username=tg_user.username,
        )
        if ok and linked_emp is not None:
            if reason == "ok":
                await notify_owners_employee_connected(session, bot, settings, linked_emp)
            actor = await resolve_actor(
                session=session,
                telegram_id=tg_user.telegram_id,
                owner_ids=settings.owner_ids,
                owner_usernames=settings.owner_usernames,
                telegram_username=tg_user.username,
            )

    if actor.role == ActorRole.UNAUTHORIZED:
        return None, _json_error(
            "Недостатньо прав 🔒 Попроси в Ані персональне посилання.",
            status=403,
            code="forbidden",
        )

    return actor, None


async def _run_idempotent(
    request: web.Request,
    session: AsyncSession,
    actor: ActorContext,
    endpoint: str,
    action: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
) -> web.Response:
    idem_key = request.headers.get("X-Idempotency-Key", "").strip()
    if not idem_key:
        status_code, payload = await action()
        return web.json_response(payload, status=status_code)

    idem_key = idem_key[:120]
    existing = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.key == idem_key,
            IdempotencyRecord.actor_tg_id == actor.telegram_id,
            IdempotencyRecord.endpoint == endpoint,
        )
    )
    if existing is not None:
        return web.Response(
            status=existing.status_code,
            text=existing.response_json,
            content_type="application/json",
        )

    status_code, payload = await action()
    if 200 <= status_code < 300:
        body_str = json.dumps(payload, ensure_ascii=False)
        session.add(
            IdempotencyRecord(
                key=idem_key,
                actor_tg_id=actor.telegram_id,
                endpoint=endpoint,
                status_code=status_code,
                response_json=body_str,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
    return web.json_response(payload, status=status_code)


async def _parse_json_body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError("Некоректний JSON у запиті") from exc
    if not isinstance(data, dict):
        raise ValueError("Очікується JSON-об'єкт")
    return data


async def _parse_optional_json_body(request: web.Request) -> dict[str, Any]:
    raw = await request.read()
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Некоректний JSON у запиті") from exc
    if not isinstance(data, dict):
        raise ValueError("Очікується JSON-об'єкт")
    return data


def _serialize_shift_for_viewer(
    sh: Shift,
    rev_map: dict[date, float],
    can_view_money: bool,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": sh.id,
        "work_date": sh.work_date.isoformat(),
        "date_label": format_date_with_day(sh.work_date),
        "start_time": sh.start_time,
        "end_time": sh.end_time,
        "hours": sh.hours,
        "is_banquet": sh.is_banquet,
        "banquet_guests": sh.banquet_guests,
    }
    if can_view_money:
        bd = compute_shift_breakdown(sh, rev_map)
        item.update({
            "base_pay": bd.base_pay,
            "percent_pay": bd.percent_pay,
            "banquet_pay": bd.banquet_pay,
            "total_pay": bd.total_pay,
            "revenue_missing": bd.revenue_missing,
            "missing_banquet_rate": bd.missing_banquet_rate,
            "missing_banquet_guests": bd.missing_banquet_guests,
        })
    return item


_GZIP_ACCEPT_RE = re.compile(r"(?:^|,)\s*gzip\b(?!\s*;\s*q=0(?:\.0+)?\b)", re.IGNORECASE)


def _accepts_gzip(request: web.Request) -> bool:
    return bool(_GZIP_ACCEPT_RE.search(request.headers.get("Accept-Encoding", "")))


def _etag_matches(header_val: str | None, etag: str) -> bool:
    if not header_val:
        return False
    for raw_tag in header_val.split(","):
        tag = raw_tag.strip()
        if tag == "*" or tag == etag or tag.removeprefix("W/") == etag:
            return True
    return False


def _serve_precompressed(
    request: web.Request,
    *,
    raw_bytes: bytes,
    gz_bytes: bytes,
    etag: str,
    content_type: str,
    cache_control: str,
) -> web.Response:
    base_headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "Vary": "Accept-Encoding",
    }
    if _etag_matches(request.headers.get("If-None-Match"), etag):
        return web.Response(status=304, headers=base_headers)

    if _accepts_gzip(request):
        return web.Response(
            body=gz_bytes,
            headers={
                **base_headers,
                "Content-Type": content_type,
                "Content-Encoding": "gzip",
            },
        )
    return web.Response(
        body=raw_bytes,
        headers={
            **base_headers,
            "Content-Type": content_type,
        },
    )


async def _build_me_payload(
    session: AsyncSession,
    settings: Settings,
    actor: ActorContext,
    today: date,
    pending_requests_count: int | None = None,
) -> dict[str, Any]:
    roles = (await session.scalars(select(Role).order_by(Role.id.asc()))).all()
    emp_payload = None
    if actor.employee is not None:
        emp_payload = {
            "id": actor.employee.id,
            "full_name": actor.employee.full_name,
            "role_name": actor.employee.role.name if actor.employee.role else "—",
            "hourly_rate": actor.employee.hourly_rate,
            "revenue_percent": actor.employee.revenue_percent,
            "banquet_rate_per_person": actor.employee.banquet_rate_per_person,
            "last_start_time": actor.employee.last_start_time,
            "last_end_time": actor.employee.last_end_time,
            "card_digits": actor.employee.card_number,
            "card_display": format_card_display(actor.employee.card_number),
            "card_masked": mask_card(actor.employee.card_number),
        }

    pending_cnt = 0
    if actor.has_perm("can_payout"):
        if pending_requests_count is not None:
            pending_cnt = int(pending_requests_count)
        else:
            pending_cnt = int(
                await session.scalar(
                    select(func.count(PayoutRequest.id)).where(PayoutRequest.status == "pending")
                )
                or 0
            )

    return {
        "role": actor.role.value,
        "actor_label": actor.actor_label,
        "telegram_id": actor.telegram_id,
        "today": today.isoformat(),
        "today_label": format_date_with_day(today, full=True),
        "timezone": settings.timezone_name,
        "permissions": _actor_permissions_payload(actor),
        "employee": emp_payload,
        "roles": [{"id": r.id, "name": r.name} for r in roles],
        "pending_payout_requests": pending_cnt,
    }


async def _build_staff_list_payload(
    session: AsyncSession,
    d1: date,
    d2: date,
    can_money: bool,
) -> dict[str, Any]:
    summaries = await get_all_employees_summary(
        session,
        d1,
        d2,
        include_inactive_with_activity=False,
    )
    items: list[dict[str, Any]] = []
    for summary in summaries:
        emp = summary.employee
        role_name = emp.role.name if emp.role else "—"
        item: dict[str, Any] = {
            "id": emp.id,
            "full_name": emp.full_name,
            "role_id": emp.role_id,
            "role_name": role_name,
            "is_admin_target": is_admin_role_name(role_name),
            "connected": emp.telegram_id is not None,
            "telegram_username": emp.telegram_username,
            "shifts_count": summary.shifts_count,
            "total_hours": summary.total_hours,
            "has_card": bool(emp.card_number),
            "card_masked": mask_card(emp.card_number),
            "has_warnings": bool(
                summary.missing_banquet_rate_dates
                or summary.missing_banquet_guests_dates
            ),
        }
        if can_money:
            item.update({
                "hourly_rate": emp.hourly_rate,
                "revenue_percent": emp.revenue_percent,
                "banquet_rate_per_person": emp.banquet_rate_per_person,
                "earned_period": summary.earned_period,
                "balance_due": summary.balance_due,
            })
        else:
            item.update({
                "hourly_rate": None,
                "revenue_percent": None,
                "banquet_rate_per_person": None,
                "earned_period": None,
                "balance_due": None,
            })
        items.append(item)
    return {"staff": items, "can_view_money": can_money}


async def _build_payouts_overview_payload(
    session: AsyncSession,
    settings: Settings,
    today: date,
) -> dict[str, Any]:
    summaries = await get_all_employees_summary(session, today.replace(day=1), today)
    total_due = q2_float(sum((to_dec(s.balance_due) for s in summaries), Decimal("0.00")))
    bal_map = {s.employee.id: s.balance_due for s in summaries}
    emp_map = {s.employee.id: s.employee for s in summaries}

    open_reqs = (
        await session.scalars(
            select(PayoutRequest)
            .where(PayoutRequest.status.in_(("pending", "approved")))
            .order_by(PayoutRequest.created_at.desc(), PayoutRequest.id.desc())
            .limit(30)
        )
    ).all()
    recent_done_reqs = (
        await session.scalars(
            select(PayoutRequest)
            .where(PayoutRequest.status.in_(("paid", "rejected")))
            .order_by(PayoutRequest.created_at.desc(), PayoutRequest.id.desc())
            .limit(15)
        )
    ).all()

    recent_payouts = (
        await session.scalars(
            select(Payout)
            .options(raiseload(Payout.employee))
            .order_by(Payout.paid_date.desc(), Payout.id.desc())
            .limit(25)
        )
    ).all()

    serialized_requests = [
        serialize_payout_request(
            r,
            current_balance_due=bal_map.get(r.employee_id),
            reveal_card=(r.status in {"approved", "paid"}),
            tz=settings.tz,
        )
        for r in [*open_reqs, *recent_done_reqs]
    ]

    return {
        "total_balance_due": total_due,
        "payout_requests": serialized_requests,
        "employees": [
            {
                "id": s.employee.id,
                "full_name": s.employee.full_name,
                "role_name": s.employee.role.name if s.employee.role else "—",
                "is_active": s.employee.is_active,
                "all_time_earned": s.all_time_earned,
                "all_time_paid": s.all_time_paid,
                "balance_due": s.balance_due,
                "has_card": bool(s.employee.card_number),
                "card_masked": mask_card(s.employee.card_number),
                "card_digits": s.employee.card_number,
                "card_display": format_card_display(s.employee.card_number),
            }
            for s in summaries
        ],
        "recent_payouts": [
            {
                "id": p.id,
                "employee_id": p.employee_id,
                "full_name": emp_map[p.employee_id].full_name if p.employee_id in emp_map else "—",
                "date": p.paid_date.isoformat(),
                "date_label": format_date_with_day(p.paid_date),
                "amount": p.amount,
                "comment": p.comment,
            }
            for p in recent_payouts
        ],
    }


def _serialize_admin_perms(perms: AdminPerms) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "enabled": perms.has(key),
        }
        for key, label in ADMIN_PERM_LABELS
    ]


async def _build_admin_perms_payload(session: AsyncSession) -> dict[str, Any]:
    perms = await get_admin_perms(session)
    return {"permissions": _serialize_admin_perms(perms)}


def _resolve_bootstrap_target(actor: ActorContext, requested_tab: str | None) -> tuple[str, str]:
    req = (requested_tab or "").strip().lower()
    if actor.is_owner:
        allowed = ("pulse", "staff", "payouts", "perms")
        return ("admin", req if req in allowed else "pulse")

    if actor.is_admin:
        admin_tabs: list[str] = []
        if actor.has_perm("can_view_salaries_reports"):
            admin_tabs.append("pulse")
        if actor.admin_perms.can_open_staff_tab:
            admin_tabs.append("staff")
        if actor.has_perm("can_payout"):
            admin_tabs.append("payouts")
        personal_tabs = ("shift", "calendar", "salary")
        if req in admin_tabs:
            return ("admin", req)
        if req in personal_tabs:
            return ("personal", req)
        if admin_tabs:
            return ("admin", admin_tabs[0])
        return ("personal", "shift")

    personal_tabs = ("shift", "calendar", "salary")
    return ("personal", req if req in personal_tabs else "shift")


def create_webapp(
    session_maker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
) -> web.Application:
    app = web.Application()

    raw_index, gz_index, index_etag = _build_bundled_index_html(STATIC_DIR)
    raw_manifest = (STATIC_DIR / "manifest.webmanifest").read_bytes()
    gz_manifest = gzip.compress(raw_manifest, compresslevel=6)
    manifest_etag = f'"{hashlib.sha256(raw_manifest).hexdigest()[:16]}"'
    raw_icon = (STATIC_DIR / "icon.svg").read_bytes()
    gz_icon = gzip.compress(raw_icon, compresslevel=6)
    icon_etag = f'"{hashlib.sha256(raw_icon).hexdigest()[:16]}"'

    @web.middleware
    async def compression_and_cache_mw(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        resp = await handler(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path == "/":
            resp.headers.setdefault("Cache-Control", "no-cache")
        elif request.path.startswith("/static/") or request.path in ("/icon.svg", "/manifest.webmanifest"):
            resp.headers.setdefault("Cache-Control", "public, max-age=86400")

        if (
            resp.status in (204, 304)
            or not isinstance(resp, web.Response)
            or isinstance(resp, web.FileResponse)
            or "Content-Encoding" in resp.headers
            or not _accepts_gzip(request)
        ):
            return resp

        ctype = (resp.content_type or "").split(";")[0].strip().lower()
        if ctype not in _COMPRESSIBLE_CONTENT_TYPES:
            return resp

        body = resp.body
        if isinstance(body, (bytes, bytearray)) and len(body) >= 256:
            compressed = gzip.compress(bytes(body), compresslevel=5)
            if len(compressed) < len(body):
                resp.body = compressed
                resp.headers["Content-Encoding"] = "gzip"
                vary_cur = resp.headers.get("Vary", "")
                if "accept-encoding" not in vary_cur.lower():
                    resp.headers["Vary"] = f"{vary_cur}, Accept-Encoding".strip(", ")
        return resp

    app.middlewares.append(compression_and_cache_mw)

    # ----------------- ПУБЛІЧНА СТАТИКА / HEALTH / PWA -----------------

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "status": miniapp_runtime.status,
            "miniapp_url": miniapp_runtime.url,
        })

    async def handle_index(request: web.Request) -> web.Response:
        return _serve_precompressed(
            request,
            raw_bytes=raw_index,
            gz_bytes=gz_index,
            etag=index_etag,
            content_type="text/html; charset=utf-8",
            cache_control="no-cache",
        )

    async def handle_manifest(request: web.Request) -> web.Response:
        return _serve_precompressed(
            request,
            raw_bytes=raw_manifest,
            gz_bytes=gz_manifest,
            etag=manifest_etag,
            content_type="application/manifest+json; charset=utf-8",
            cache_control="public, max-age=86400",
        )

    async def handle_icon(request: web.Request) -> web.Response:
        return _serve_precompressed(
            request,
            raw_bytes=raw_icon,
            gz_bytes=gz_icon,
            etag=icon_etag,
            content_type="image/svg+xml; charset=utf-8",
            cache_control="public, max-age=86400",
        )

    # ----------------- ПРОФІЛЬ, ПРАВА І BOOTSTRAP -----------------

    async def handle_me(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            return web.json_response(await _build_me_payload(session, settings, actor, today))

    async def handle_bootstrap(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            try:
                d1, d2 = _parse_period_params(request.query, today)
                cal_year = int(request.query.get("year") or today.year)
                cal_month = int(request.query.get("month") or today.month)
                if not (1 <= cal_month <= 12) or not (2020 <= cal_year <= 2100):
                    raise ValueError("Некоректний рік або місяць")
            except ValueError as exc:
                return _json_error(str(exc), 400)

            workspace, initial_tab = _resolve_bootstrap_target(
                actor,
                request.query.get("tab"),
            )

            result: dict[str, Any] = {
                "workspace": workspace,
                "initial_tab": initial_tab,
            }
            pending_cnt_override: int | None = None

            if initial_tab == "pulse":
                analytics = await compute_owner_analytics(session, d1, d2, ref_date=today)
                result["analytics"] = analytics
                pending_cnt_override = analytics["kpis"].get("pending_payout_requests")
            elif initial_tab == "staff":
                can_money = actor.has_perm("can_view_salaries_reports")
                result["staff_list"] = await _build_staff_list_payload(session, d1, d2, can_money)
            elif initial_tab == "payouts":
                result["payouts"] = await _build_payouts_overview_payload(session, settings, today)
            elif initial_tab == "perms":
                result["admin_perms"] = await _build_admin_perms_payload(session)
            elif initial_tab in {"shift", "calendar", "salary"} and actor.employee is not None:
                result["my_dash"] = await compute_employee_dashboard(
                    session=session,
                    emp=actor.employee,
                    date_from=d1,
                    date_to=d2,
                    cal_year=cal_year,
                    cal_month=cal_month,
                    today=today,
                )

            result["me"] = await _build_me_payload(
                session,
                settings,
                actor,
                today,
                pending_requests_count=pending_cnt_override,
            )
            return web.json_response(result)

    # ----------------- ОСОБИСТИЙ КАБІНЕТ ПРАЦІВНИКА / АДМІНА -----------------

    async def handle_my_dashboard(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_staff_member or actor.employee is None:
                return _json_error("Особистий кабінет доступний лише працівникам.", 403)

            today = today_local(settings.tz)
            try:
                d1, d2 = _parse_period_params(request.query, today)
                cal_year = int(request.query.get("year") or today.year)
                cal_month = int(request.query.get("month") or today.month)
                if not (1 <= cal_month <= 12) or not (2020 <= cal_year <= 2100):
                    raise ValueError("Некоректний рік або місяць")
            except ValueError as exc:
                return _json_error(str(exc), 400)

            payload = await compute_employee_dashboard(
                session=session,
                emp=actor.employee,
                date_from=d1,
                date_to=d2,
                cal_year=cal_year,
                cal_month=cal_month,
                today=today,
            )
            return web.json_response(payload)

    async def handle_my_create_shift(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_staff_member or actor.employee is None:
                return _json_error("Особистий кабінет доступний лише працівникам.", 403)

            try:
                body = await _parse_json_body(request)
                work_date = _parse_any_date(body.get("work_date"))
                if body.get("time_range"):
                    start_time, end_time, hours = parse_time_range(str(body["time_range"]))
                else:
                    start_time, end_time, hours = parse_time_range(
                        f"{body.get('start_time', '')}-{body.get('end_time', '')}"
                    )
                is_banquet = bool(body.get("is_banquet", False))
                raw_guests = body.get("banquet_guests")
                guests: int | None = None
                if is_banquet and raw_guests not in (None, "", 0, "0"):
                    guests = int(raw_guests)
                    if guests <= 0 or guests > 2000:
                        raise ValueError("Кількість гостей має бути від 1 до 2000")
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            emp = actor.employee

            async def _do() -> tuple[int, dict[str, Any]]:
                existing = await session.scalar(
                    select(Shift)
                    .options(raiseload(Shift.employee))
                    .where(Shift.employee_id == emp.id, Shift.work_date == work_date)
                )
                if existing is not None:
                    return 409, {
                        "error": f"За {format_date_short(work_date)} зміна вже внесена. Можеш оновити її години.",
                        "code": "duplicate_shift",
                        "shift_id": existing.id,
                    }

                shift = Shift(
                    employee_id=emp.id,
                    work_date=work_date,
                    start_time=start_time,
                    end_time=end_time,
                    hours=hours,
                    hourly_rate_snapshot=emp.hourly_rate,
                    percent_snapshot=emp.revenue_percent,
                    is_banquet=is_banquet,
                    banquet_guests=guests if is_banquet else None,
                    banquet_rate_snapshot=emp.banquet_rate_per_person if is_banquet else None,
                )
                emp.last_start_time = start_time
                emp.last_end_time = end_time
                session.add(shift)
                await session.commit()
                await session.refresh(shift)

                if is_banquet and emp.banquet_rate_per_person is None:
                    await notify_owners_missing_banquet_rate(
                        session, bot, settings, emp, work_date, guests
                    )

                rev = await session.scalar(
                    select(DailyRevenue.amount).where(DailyRevenue.work_date == work_date)
                )
                rev_map = {work_date: rev} if rev is not None else {}
                return 201, {
                    "ok": True,
                    "shift": _serialize_shift_for_viewer(shift, rev_map, can_view_money=True),
                    "needs_owner_banquet_rate": bool(is_banquet and emp.banquet_rate_per_person is None),
                }

            return await _run_idempotent(request, session, actor, "POST:/api/my/shifts", _do)

    async def handle_my_patch_shift(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_staff_member or actor.employee is None:
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                shift_id = int(request.match_info["shift_id"])
            except ValueError:
                return _json_error("Некоректний ID зміни", 400)

            shift = await session.get(Shift, shift_id)
            if shift is None or shift.employee_id != actor.employee.id:
                return _json_error("Зміну не знайдено або недостатньо прав 🔒", 403)

            try:
                body = await _parse_json_body(request)
                if body.get("time_range") or (body.get("start_time") and body.get("end_time")):
                    raw_tr = (
                        str(body["time_range"])
                        if body.get("time_range")
                        else f"{body.get('start_time')}-{body.get('end_time')}"
                    )
                    start_time, end_time, hours = parse_time_range(raw_tr)
                    shift.start_time = start_time
                    shift.end_time = end_time
                    shift.hours = hours
                    actor.employee.last_start_time = start_time
                    actor.employee.last_end_time = end_time

                notify_banquet_rate = False
                if "is_banquet" in body:
                    shift.is_banquet = bool(body["is_banquet"])
                    if not shift.is_banquet:
                        shift.banquet_guests = None
                        shift.banquet_rate_snapshot = None
                    elif shift.banquet_rate_snapshot is None:
                        shift.banquet_rate_snapshot = actor.employee.banquet_rate_per_person
                        if shift.banquet_rate_snapshot is None:
                            notify_banquet_rate = True

                if "banquet_guests" in body and shift.is_banquet:
                    raw_g = body["banquet_guests"]
                    if raw_g in (None, "", 0, "0"):
                        shift.banquet_guests = None
                    else:
                        g = int(raw_g)
                        if g <= 0 or g > 2000:
                            raise ValueError("Кількість гостей має бути від 1 до 2000")
                        shift.banquet_guests = g
                        if shift.banquet_rate_snapshot is None:
                            notify_banquet_rate = True
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            await session.commit()
            await session.refresh(shift)
            if notify_banquet_rate:
                await notify_owners_missing_banquet_rate(
                    session, bot, settings, actor.employee, shift.work_date, shift.banquet_guests
                )

            rev = await session.scalar(
                select(DailyRevenue.amount).where(DailyRevenue.work_date == shift.work_date)
            )
            rev_map = {shift.work_date: rev} if rev is not None else {}
            return web.json_response({
                "ok": True,
                "shift": _serialize_shift_for_viewer(shift, rev_map, can_view_money=True),
            })

    async def handle_my_put_card(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_staff_member or actor.employee is None:
                return _json_error("Особистий кабінет доступний лише працівникам.", 403)

            try:
                body = await _parse_json_body(request)
                clean = await save_employee_card(session, actor.employee, body.get("card_number"))
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            return web.json_response({
                "ok": True,
                "card_digits": clean,
                "card_display": format_card_display(clean),
                "card_masked": mask_card(clean),
                "card_luhn_ok": luhn_is_valid(clean),
            })

    async def handle_my_create_payout_request(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_staff_member or actor.employee is None:
                return _json_error("Особистий кабінет доступний лише працівникам.", 403)

            emp = actor.employee
            today = today_local(settings.tz)
            try:
                body = await _parse_json_body(request)
                amount = _parse_money(body.get("amount"), allow_zero=False)
                raw_comment = body.get("comment")
                comment = None if raw_comment is None else str(raw_comment)
                raw_card = body.get("card_number")
                card_str = None if raw_card is None else str(raw_card)
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            async def _do() -> tuple[int, dict[str, Any]]:
                try:
                    req = await create_payout_request(
                        session=session,
                        emp=emp,
                        amount=amount,
                        comment=comment,
                        raw_card=card_str,
                    )
                except ValueError as exc:
                    return 400, {"error": str(exc), "code": "bad_request"}

                summary = await get_employee_summary(session, emp, today.replace(day=1), today)
                await notify_owners_payout_request_created(
                    session, bot, settings, emp, req, summary.balance_due
                )
                return 201, {
                    "ok": True,
                    "request": serialize_payout_request(
                        req,
                        current_balance_due=summary.balance_due,
                        reveal_card=True,
                        tz=settings.tz,
                    ),
                }

            return await _run_idempotent(request, session, actor, "POST:/api/my/payout-requests", _do)

    async def handle_my_cancel_payout_request(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_staff_member or actor.employee is None:
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                req_id = int(request.match_info["req_id"])
                req = await cancel_payout_request(session, req_id, actor.employee.id)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            return web.json_response({
                "ok": True,
                "request": serialize_payout_request(req, reveal_card=True, tz=settings.tz),
            })

    # ----------------- ВЛАСНИЦЯ / АДМІН: АНАЛІТИКА ТА EXCEL -----------------

    async def handle_owner_analytics(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_view_salaries_reports"):
                return _json_error("Недостатньо прав для перегляду фінансової аналітики 🔒", 403)

            today = today_local(settings.tz)
            try:
                d1, d2 = _parse_period_params(request.query, today)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            analytics = await compute_owner_analytics(session, d1, d2, ref_date=today)
            return web.json_response(analytics)

    async def handle_owner_excel_download(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(
                request, session, settings, bot, allow_query_init_data=True
            )
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_view_salaries_reports"):
                return _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            try:
                d1, d2 = _parse_period_params(request.query, today)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            summaries = await get_all_employees_summary(session, d1, d2)
            xlsx_bytes = build_payroll_excel(summaries, d1, d2)
            filename = f"zvit_{d1.isoformat()}_{d2.isoformat()}.xlsx"
            return web.Response(
                body=xlsx_bytes,
                headers={
                    "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )

    async def handle_owner_excel_send_chat(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_view_salaries_reports"):
                return _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            try:
                body = await _parse_json_body(request)
                d1 = _parse_any_date(body.get("from")) if body.get("from") else today.replace(day=1)
                d2 = _parse_any_date(body.get("to")) if body.get("to") else today
                if d1 > d2:
                    d1, d2 = d2, d1
            except ValueError as exc:
                return _json_error(str(exc), 400)

            summaries = await get_all_employees_summary(session, d1, d2)
            xlsx_bytes = build_payroll_excel(summaries, d1, d2)
            filename = f"zvit_{d1.isoformat()}_{d2.isoformat()}.xlsx"
            try:
                await bot.send_document(
                    actor.telegram_id,
                    BufferedInputFile(xlsx_bytes, filename=filename),
                    caption=f"📥 Excel-звіт за <b>{format_date_short(d1)}–{format_date_short(d2)}</b> (з Mini App)",
                )
            except Exception as exc:
                return _json_error(f"Не вдалося надіслати у Telegram: {exc}", 500)

            return web.json_response({"ok": True, "filename": filename})

    # ----------------- КАСА ДНЯ -----------------

    async def handle_owner_revenues_list(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_enter_revenue"):
                return _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            try:
                days_count = min(60, max(7, int(request.query.get("days") or 21)))
            except ValueError:
                days_count = 21
            dates = [today - timedelta(days=i) for i in range(days_count)]
            rev_map = await get_recent_revenues(session, dates)

            shifts = (
                await session.scalars(
                    select(Shift)
                    .options(raiseload(Shift.employee))
                    .where(Shift.work_date.in_(set(dates)))
                )
            ).all()
            shifts_per_day: dict[date, int] = {}
            pct_staff_per_day: dict[date, int] = {}
            for sh in shifts:
                shifts_per_day[sh.work_date] = shifts_per_day.get(sh.work_date, 0) + 1
                if sh.percent_snapshot > 0:
                    pct_staff_per_day[sh.work_date] = pct_staff_per_day.get(sh.work_date, 0) + 1

            rows = [
                {
                    "date": d.isoformat(),
                    "date_label": format_date_with_day(d),
                    "weekday_short": WEEKDAYS_SHORT_UK[d.weekday()],
                    "is_today": d == today,
                    "amount": rev_map.get(d),
                    "shifts_count": shifts_per_day.get(d, 0),
                    "pct_eligible_count": pct_staff_per_day.get(d, 0),
                }
                for d in dates
            ]
            return web.json_response({"days": rows})

    async def handle_owner_put_revenue(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_enter_revenue"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                work_date = _parse_any_date(request.match_info["work_date"])
                body = await _parse_json_body(request)
                amount = _parse_money(body.get("amount"), allow_zero=True)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            async def _do() -> tuple[int, dict[str, Any]]:
                existing = await session.scalar(
                    select(DailyRevenue).where(DailyRevenue.work_date == work_date)
                )
                if existing is None:
                    session.add(DailyRevenue(work_date=work_date, amount=amount))
                else:
                    existing.amount = amount
                await session.commit()

                shifts_that_day = list(
                    (await session.scalars(select(Shift).where(Shift.work_date == work_date))).all()
                )
                pct_items = await notify_revenue_saved(
                    session, bot, settings, actor, work_date, amount, shifts_that_day
                )
                resp_payload: dict[str, Any] = {
                    "ok": True,
                    "work_date": work_date.isoformat(),
                    "date_label": format_date_with_day(work_date),
                    "amount": amount,
                }
                if actor.has_perm("can_view_salaries_reports"):
                    resp_payload["percent_bonuses"] = pct_items
                return 200, resp_payload

            return await _run_idempotent(
                request, session, actor, f"PUT:/api/owner/revenues/{work_date.isoformat()}", _do
            )

    # ----------------- ПРАЦІВНИКИ -----------------

    async def handle_owner_staff_list(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not (actor.is_owner or actor.admin_perms.can_open_staff_tab):
                return _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            try:
                d1, d2 = _parse_period_params(request.query, today)
            except ValueError as exc:
                return _json_error(str(exc), 400)
            can_money = actor.has_perm("can_view_salaries_reports")
            return web.json_response(
                await _build_staff_list_payload(session, d1, d2, can_money)
            )

    async def handle_owner_staff_detail(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not (actor.is_owner or actor.admin_perms.can_open_staff_tab):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)

            emp = await session.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                return _json_error("Працівника не знайдено", 404)

            today = today_local(settings.tz)
            d1, d2 = _parse_period_params(request.query, today)
            can_money = actor.has_perm("can_view_salaries_reports")
            can_manage = actor.has_perm("can_manage_staff")
            role_name = emp.role.name if emp.role else "—"
            target_is_admin = is_admin_role_name(role_name)
            can_manage_target = can_manage and (actor.is_owner or not target_is_admin)

            invite_link = (
                await _ensure_invite_link(app, session, bot, emp)
                if (emp.telegram_id is None and can_manage_target)
                else None
            )

            recent_shifts = (
                await session.scalars(
                    select(Shift)
                    .options(raiseload(Shift.employee))
                    .where(Shift.employee_id == emp.id)
                    .order_by(Shift.work_date.desc())
                    .limit(25)
                )
            ).all()
            rev_map = (
                await get_recent_revenues(session, [s.work_date for s in recent_shifts])
                if can_money and recent_shifts
                else {}
            )

            payload: dict[str, Any] = {
                "employee": {
                    "id": emp.id,
                    "full_name": emp.full_name,
                    "role_id": emp.role_id,
                    "role_name": role_name,
                    "is_admin_target": target_is_admin,
                    "connected": emp.telegram_id is not None,
                    "telegram_username": emp.telegram_username,
                    "invite_link": invite_link,
                    "can_manage_target": can_manage_target,
                },
                "shifts": [
                    _serialize_shift_for_viewer(sh, rev_map, can_view_money=can_money)
                    for sh in recent_shifts
                ],
                "can_view_money": can_money,
            }

            if can_money:
                summary = await get_employee_summary(session, emp, d1, d2)
                payload["employee"].update({
                    "hourly_rate": emp.hourly_rate,
                    "revenue_percent": emp.revenue_percent,
                    "banquet_rate_per_person": emp.banquet_rate_per_person,
                    "has_card": bool(emp.card_number),
                    "card_masked": mask_card(emp.card_number),
                    "card_display": format_card_display(emp.card_number),
                    "card_digits": emp.card_number,
                })
                payload["summary"] = {
                    "from": d1.isoformat(),
                    "to": d2.isoformat(),
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
                }
            else:
                # Тільки кількість змін та годин без жодних сум
                period_shifts = [s for s in recent_shifts if d1 <= s.work_date <= d2]
                payload["summary"] = {
                    "from": d1.isoformat(),
                    "to": d2.isoformat(),
                    "shifts_count": len(period_shifts),
                    "total_hours": q2_float(sum((to_dec(s.hours) for s in period_shifts), Decimal("0.00"))),
                }
                if actor.has_perm("can_payout"):
                    payload["employee"].update({
                        "has_card": bool(emp.card_number),
                        "card_masked": mask_card(emp.card_number),
                        "card_display": format_card_display(emp.card_number),
                        "card_digits": emp.card_number,
                    })

            if can_money or actor.has_perm("can_adjust_money"):
                adjs = (
                    await session.scalars(
                        select(Adjustment)
                        .options(raiseload(Adjustment.employee))
                        .where(Adjustment.employee_id == emp.id)
                        .order_by(Adjustment.adj_date.desc(), Adjustment.id.desc())
                        .limit(12)
                    )
                ).all()
                payload["adjustments"] = [
                    {
                        "id": a.id,
                        "date": a.adj_date.isoformat(),
                        "date_label": format_date_with_day(a.adj_date),
                        "kind": a.kind,
                        "amount": a.amount,
                        "comment": a.comment,
                    }
                    for a in adjs
                ]
            if can_money or actor.has_perm("can_payout"):
                pays = (
                    await session.scalars(
                        select(Payout)
                        .options(raiseload(Payout.employee))
                        .where(Payout.employee_id == emp.id)
                        .order_by(Payout.paid_date.desc(), Payout.id.desc())
                        .limit(12)
                    )
                ).all()
                payload["payouts"] = [
                    {
                        "id": p.id,
                        "date": p.paid_date.isoformat(),
                        "date_label": format_date_with_day(p.paid_date),
                        "amount": p.amount,
                        "comment": p.comment,
                    }
                    for p in pays
                ]

            return web.json_response(payload)

    async def handle_owner_staff_create(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_manage_staff"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                body = await _parse_json_body(request)
                full_name = str(body.get("full_name") or "").strip()
                if len(full_name) < 2 or len(full_name) > 100:
                    raise ValueError("Ім'я має бути від 2 до 100 символів")
                hourly_rate = _parse_money(body.get("hourly_rate", 0), allow_zero=True)
                revenue_percent = _parse_money(body.get("revenue_percent", 0), allow_zero=True)
                raw_banquet = body.get("banquet_rate_per_person")
                banquet_rate = (
                    _parse_money(raw_banquet, allow_zero=False)
                    if raw_banquet not in (None, "")
                    else None
                )

                role: Role | None = None
                if body.get("role_id") is not None:
                    role = await session.get(Role, int(body["role_id"]))
                elif body.get("role_name"):
                    rname = str(body["role_name"]).strip()
                    if len(rname) < 2 or len(rname) > 60:
                        raise ValueError("Назва посади має бути від 2 до 60 символів")
                    if not actor.is_owner and is_admin_role_name(rname):
                        return _json_error("Тільки Аня може призначати роль Адміна 🔒", 403)
                    role = await _get_or_create_role(session, rname)

                if role is None:
                    raise ValueError("Обери або введи посаду")
                if not actor.is_owner and is_admin_role_name(role.name):
                    return _json_error("Тільки Аня може призначати роль Адміна 🔒", 403)
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            async def _do() -> tuple[int, dict[str, Any]]:
                invite_code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(10))
                emp = Employee(
                    full_name=full_name,
                    role_id=role.id,
                    hourly_rate=hourly_rate,
                    revenue_percent=revenue_percent,
                    banquet_rate_per_person=banquet_rate,
                    invite_code=invite_code,
                    is_active=True,
                )
                session.add(emp)
                await session.commit()
                await session.refresh(emp)

                invite_link = await _ensure_invite_link(app, session, bot, emp)
                await notify_owners_admin_action(
                    session,
                    bot,
                    settings,
                    actor,
                    f"створив(ла) працівника <b>{emp.full_name}</b> ({role.name})",
                )
                return 201, {
                    "ok": True,
                    "employee": {
                        "id": emp.id,
                        "full_name": emp.full_name,
                        "role_id": emp.role_id,
                        "role_name": role.name,
                        "hourly_rate": emp.hourly_rate,
                        "revenue_percent": emp.revenue_percent,
                        "banquet_rate_per_person": emp.banquet_rate_per_person,
                        "invite_link": invite_link,
                    },
                }

            return await _run_idempotent(request, session, actor, "POST:/api/owner/staff", _do)

    async def handle_owner_staff_patch(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_manage_staff"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)

            emp = await session.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                return _json_error("Працівника не знайдено", 404)
            if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
                return _json_error("Тільки Аня може змінювати профіль Адміна 🔒", 403)

            try:
                body = await _parse_json_body(request)
                events: list[tuple[str, Any]] = []

                if "full_name" in body and body["full_name"] is not None:
                    nm = str(body["full_name"]).strip()
                    if len(nm) < 2 or len(nm) > 100:
                        raise ValueError("Ім'я має бути від 2 до 100 символів")
                    emp.full_name = nm

                if "hourly_rate" in body and body["hourly_rate"] is not None:
                    val = _parse_money(body["hourly_rate"], allow_zero=True)
                    if val != emp.hourly_rate:
                        emp.hourly_rate = val
                        events.append(("hourly", val))

                if "revenue_percent" in body and body["revenue_percent"] is not None:
                    val = _parse_money(body["revenue_percent"], allow_zero=True)
                    if val != emp.revenue_percent:
                        emp.revenue_percent = val
                        events.append(("percent", val))

                banquet_new: float | None = None
                if "banquet_rate_per_person" in body and body["banquet_rate_per_person"] not in (None, ""):
                    banquet_new = _parse_money(body["banquet_rate_per_person"], allow_zero=False)

                role_new: Role | None = None
                if body.get("role_id") is not None:
                    role_new = await session.get(Role, int(body["role_id"]))
                    if role_new is None:
                        raise ValueError("Посаду не знайдено")
                elif body.get("role_name"):
                    rname = str(body["role_name"]).strip()
                    if len(rname) < 2 or len(rname) > 60:
                        raise ValueError("Назва посади має бути від 2 до 60 символів")
                    if not actor.is_owner and is_admin_role_name(rname):
                        return _json_error("Тільки Аня може призначати роль Адміна 🔒", 403)
                    role_new = await _get_or_create_role(session, rname)

                if role_new is not None and not actor.is_owner and is_admin_role_name(role_new.name):
                    return _json_error("Тільки Аня може призначати роль Адміна 🔒", 403)

                await session.commit()
                await session.refresh(emp)

                for kind, val in events:
                    await notify_rate_changed(session, bot, settings, actor, emp, kind, val)

                recalculated_dates: list[str] = []
                if banquet_new is not None:
                    upd_dates = await set_employee_banquet_rate(session, emp, banquet_new)
                    recalculated_dates = [d.isoformat() for d in upd_dates]
                    await notify_rate_changed(
                        session, bot, settings, actor, emp, "banquet", banquet_new, upd_dates
                    )

                if role_new is not None and role_new.id != emp.role_id:
                    was_admin = bool(emp.role and is_admin_role_name(emp.role.name))
                    is_now_admin = is_admin_role_name(role_new.name)
                    emp.role_id = role_new.id
                    emp.role = role_new
                    await session.commit()
                    await session.refresh(emp, attribute_names=["role"])
                    perms = await get_admin_perms(session)
                    await notify_role_changed(
                        session, bot, settings, actor, emp, role_new, was_admin, is_now_admin, perms
                    )
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            return web.json_response({
                "ok": True,
                "recalculated_banquet_dates": recalculated_dates,
            })

    async def handle_owner_staff_relink(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_manage_staff"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)
            emp = await session.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                return _json_error("Працівника не знайдено", 404)
            if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
                return _json_error("Тільки Аня може керувати Адміном 🔒", 403)

            emp.telegram_id = None
            emp.telegram_username = None
            emp.invite_code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(10))
            await session.commit()
            await session.refresh(emp)
            invite_link = await _ensure_invite_link(app, session, bot, emp)

            await notify_owners_admin_action(
                session,
                bot,
                settings,
                actor,
                f"скинув(ла) Telegram-прив'язку працівнику <b>{emp.full_name}</b>",
            )
            return web.json_response({"ok": True, "invite_link": invite_link})

    async def handle_owner_staff_delete(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_manage_staff"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)
            emp = await session.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                return _json_error("Працівника не знайдено", 404)
            if not actor.is_owner and emp.role and is_admin_role_name(emp.role.name):
                return _json_error("Недостатньо прав 🔒", 403)

            name = emp.full_name
            mode = await delete_employee_safely(session, emp)
            await notify_owners_admin_action(
                session,
                bot,
                settings,
                actor,
                f"видалив(ла) працівника <b>{name}</b>",
            )
            return web.json_response({"ok": True, "mode": mode})

    # ----------------- РЕДАГУВАННЯ ГОДИН / ПРЕМІЇ / ШТРАФИ -----------------

    async def handle_owner_put_emp_shift(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_edit_hours"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)
            emp = await session.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                return _json_error("Працівника не знайдено", 404)

            try:
                body = await _parse_json_body(request)
                work_date = _parse_any_date(body.get("work_date"))
                if body.get("time_range"):
                    start_time, end_time, hours = parse_time_range(str(body["time_range"]))
                else:
                    start_time, end_time, hours = parse_time_range(
                        f"{body.get('start_time', '')}-{body.get('end_time', '')}"
                    )
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            is_banq = bool(body.get("is_banquet", False))
            shift = await session.scalar(
                select(Shift).where(Shift.employee_id == emp.id, Shift.work_date == work_date)
            )
            old_range: str | None = None
            created = False
            if shift is None:
                created = True
                shift = Shift(
                    employee_id=emp.id,
                    work_date=work_date,
                    start_time=start_time,
                    end_time=end_time,
                    hours=hours,
                    hourly_rate_snapshot=emp.hourly_rate,
                    percent_snapshot=emp.revenue_percent,
                    is_banquet=is_banq,
                    banquet_rate_snapshot=emp.banquet_rate_per_person if is_banq else None,
                )
                session.add(shift)
            else:
                old_range = f"{shift.start_time}-{shift.end_time}"
                shift.start_time = start_time
                shift.end_time = end_time
                shift.hours = hours

            await session.commit()
            await notify_shift_edited_by_manager(
                session, bot, settings, actor, emp, work_date, start_time, end_time, hours, created, old_range
            )
            return web.json_response({"ok": True, "created": created, "hours": hours})

    async def handle_owner_post_adjustment(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_adjust_money"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)
            emp = await session.get(Employee, emp_id)
            if emp is None or not emp.is_active:
                return _json_error("Працівника не знайдено", 404)

            try:
                body = await _parse_json_body(request)
                kind = str(body.get("kind") or "").strip().lower()
                if kind not in {"bonus", "fine"}:
                    raise ValueError("Тип має бути 'bonus' або 'fine'")
                amount = _parse_money(body.get("amount"), allow_zero=False)
                raw_comment = str(body.get("comment") or "").strip()
                comment = None if raw_comment in {"", "-", "—"} else raw_comment[:240]
                today = today_local(settings.tz)
                adj_date = _parse_any_date(body.get("adj_date")) if body.get("adj_date") else today
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            async def _do() -> tuple[int, dict[str, Any]]:
                adj = Adjustment(
                    employee_id=emp.id,
                    adj_date=adj_date,
                    kind=kind,
                    amount=amount,
                    comment=comment,
                )
                session.add(adj)
                await session.commit()

                new_summary = await get_employee_summary(session, emp, today.replace(day=1), today)
                await notify_adjustment_created(
                    session, bot, settings, actor, emp, kind, amount, comment, new_summary.balance_due
                )
                return 201, {
                    "ok": True,
                    "adjustment_id": adj.id,
                    "balance_due": new_summary.balance_due,
                }

            return await _run_idempotent(
                request, session, actor, f"POST:/api/owner/staff/{emp.id}/adjustments", _do
            )

    # ----------------- ВИПЛАТИ І БАЛАНСИ -----------------

    async def handle_owner_payouts_overview(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            return web.json_response(
                await _build_payouts_overview_payload(session, settings, today)
            )

    async def handle_owner_payout_requests_list(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            today = today_local(settings.tz)
            summaries = await get_all_employees_summary(session, today.replace(day=1), today)
            bal_map = {s.employee.id: s.balance_due for s in summaries}
            rows = (
                await session.scalars(
                    select(PayoutRequest)
                    .order_by(PayoutRequest.created_at.desc(), PayoutRequest.id.desc())
                    .limit(40)
                )
            ).all()
            return web.json_response({
                "payout_requests": [
                    serialize_payout_request(
                        r,
                        current_balance_due=bal_map.get(r.employee_id),
                        reveal_card=(r.status in {"approved", "paid"}),
                        tz=settings.tz,
                    )
                    for r in rows
                ]
            })

    async def handle_owner_payout_request_accept(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                req_id = int(request.match_info["req_id"])
            except ValueError:
                return _json_error("Некоректний ID запиту", 400)

            existing = await session.get(PayoutRequest, req_id)
            was_pending = existing is not None and existing.status == "pending"
            try:
                req = await accept_payout_request(session, req_id, actor.telegram_id)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            today = today_local(settings.tz)
            emp = await session.get(Employee, req.employee_id)
            cur_bal: float | None = None
            if emp is not None:
                summary = await get_employee_summary(session, emp, today.replace(day=1), today)
                cur_bal = summary.balance_due
                if was_pending:
                    await notify_employee_payout_request_accepted(bot, emp, req)
                    await notify_owners_admin_action(
                        session,
                        bot,
                        settings,
                        actor,
                        f"прийняв(ла) запит на виплату для <b>{emp.full_name}</b>: <b>{req.amount} грн</b>",
                    )

            return web.json_response({
                "ok": True,
                "request": serialize_payout_request(
                    req,
                    current_balance_due=cur_bal,
                    reveal_card=True,
                    tz=settings.tz,
                ),
            })

    async def handle_owner_payout_request_reject(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                req_id = int(request.match_info["req_id"])
                body = await _parse_optional_json_body(request)
                reason = body.get("reason")
                reason_str = None if reason is None else str(reason)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            existing = await session.get(PayoutRequest, req_id)
            was_open = existing is not None and existing.status in {"pending", "approved"}
            try:
                req = await reject_payout_request(session, req_id, actor.telegram_id, reason=reason_str)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            emp = await session.get(Employee, req.employee_id)
            if emp is not None and was_open:
                await notify_employee_payout_request_rejected(bot, emp, req)
                await notify_owners_admin_action(
                    session,
                    bot,
                    settings,
                    actor,
                    f"відхилив(ла) запит на виплату <b>{emp.full_name}</b>: <b>{req.amount} грн</b>",
                )

            return web.json_response({
                "ok": True,
                "request": serialize_payout_request(req, reveal_card=False, tz=settings.tz),
            })

    async def handle_owner_payout_request_pay(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                req_id = int(request.match_info["req_id"])
                body = await _parse_optional_json_body(request)
                actual_amount = (
                    _parse_money(body.get("amount"), allow_zero=False)
                    if body.get("amount") not in (None, "")
                    else None
                )
                raw_comment = body.get("comment")
                comment_str = None if raw_comment is None else str(raw_comment)
                today = today_local(settings.tz)
                paid_date = _parse_any_date(body.get("paid_date")) if body.get("paid_date") else today
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            async def _do() -> tuple[int, dict[str, Any]]:
                try:
                    req, payout, new_balance = await mark_payout_request_paid(
                        session=session,
                        req_id=req_id,
                        actor_tg_id=actor.telegram_id,
                        paid_date=paid_date,
                        actual_amount=actual_amount,
                        comment=comment_str,
                    )
                except ValueError as exc:
                    return 400, {"error": str(exc), "code": "bad_request"}

                emp = await session.get(Employee, req.employee_id)
                if emp is not None:
                    await notify_payout_created(
                        session, bot, settings, actor, emp, payout.amount, payout.comment, new_balance
                    )
                return 201, {
                    "ok": True,
                    "payout_id": payout.id,
                    "amount": payout.amount,
                    "balance_due": new_balance,
                    "request": serialize_payout_request(
                        req,
                        current_balance_due=new_balance,
                        reveal_card=True,
                        tz=settings.tz,
                    ),
                }

            return await _run_idempotent(
                request, session, actor, f"POST:/api/owner/payout-requests/{req_id}/pay", _do
            )

    async def handle_owner_payout_preview(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                emp_id = int(request.match_info["emp_id"])
            except ValueError:
                return _json_error("Некоректний ID", 400)
            emp = await session.get(Employee, emp_id)
            if emp is None:
                return _json_error("Працівника не знайдено", 404)

            today = today_local(settings.tz)
            try:
                d1, d2 = _parse_period_params(request.query, today)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            summary = await get_employee_summary(session, emp, d1, d2)
            comment = (
                f"за {format_date_short(d1)}"
                if d1 == d2
                else f"за {format_date_short(d1)}–{format_date_short(d2)}"
            )
            return web.json_response({
                "employee_id": emp.id,
                "full_name": emp.full_name,
                "from": d1.isoformat(),
                "to": d2.isoformat(),
                "shifts_count": summary.shifts_count,
                "total_hours": summary.total_hours,
                "base_pay": summary.base_pay,
                "percent_pay": summary.percent_pay,
                "banquet_pay": summary.banquet_pay,
                "earned_period": summary.earned_period,
                "balance_due": summary.balance_due,
                "suggested_comment": comment,
                "has_card": bool(emp.card_number),
                "card_masked": mask_card(emp.card_number),
                "card_display": format_card_display(emp.card_number),
                "card_digits": emp.card_number,
            })

    async def handle_owner_post_payout(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.has_perm("can_payout"):
                return _json_error("Недостатньо прав 🔒", 403)

            try:
                body = await _parse_json_body(request)
                emp_id = int(body.get("employee_id"))
                amount = _parse_money(body.get("amount"), allow_zero=False)
                raw_comment = str(body.get("comment") or "").strip()
                comment = None if raw_comment in {"", "-", "—"} else raw_comment[:240]
                today = today_local(settings.tz)
                paid_date = _parse_any_date(body.get("paid_date")) if body.get("paid_date") else today
            except (ValueError, TypeError) as exc:
                return _json_error(str(exc), 400)

            emp = await session.get(Employee, emp_id)
            if emp is None:
                return _json_error("Працівника не знайдено", 404)

            async def _do() -> tuple[int, dict[str, Any]]:
                payout = Payout(
                    employee_id=emp.id,
                    paid_date=paid_date,
                    amount=amount,
                    comment=comment,
                )
                session.add(payout)
                await session.commit()

                new_summary = await get_employee_summary(session, emp, today.replace(day=1), today)
                await notify_payout_created(
                    session, bot, settings, actor, emp, amount, comment, new_summary.balance_due
                )
                return 201, {
                    "ok": True,
                    "payout_id": payout.id,
                    "amount": amount,
                    "balance_due": new_summary.balance_due,
                }

            return await _run_idempotent(request, session, actor, "POST:/api/owner/payouts", _do)

    # ----------------- ПРАВА АДМІНА (ТІЛЬКИ ВЛАСНИЦЯ) -----------------

    async def handle_owner_admin_perms(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_owner:
                return _json_error("Тільки Аня може керувати правами Адміна 🔒", 403)

            return web.json_response(await _build_admin_perms_payload(session))

    async def handle_owner_admin_perm_toggle(request: web.Request) -> web.Response:
        async with session_maker() as session:
            actor, err = await _authenticate_request(request, session, settings, bot)
            if err is not None or actor is None:
                return err or _json_error("Недостатньо прав 🔒", 403)
            if not actor.is_owner:
                return _json_error("Тільки Аня може керувати правами Адміна 🔒", 403)

            try:
                body = await _parse_json_body(request)
                key = str(body.get("key") or "").strip()
                perms = await toggle_admin_perm(session, key)
            except ValueError as exc:
                return _json_error(str(exc), 400)

            await notify_admin_perms_changed(session, bot, perms)
            return web.json_response({"ok": True, "permissions": _serialize_admin_perms(perms)})

    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/manifest.webmanifest", handle_manifest)
    app.router.add_get("/icon.svg", handle_icon)
    app.router.add_static("/static", STATIC_DIR)

    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/bootstrap", handle_bootstrap)
    app.router.add_get("/api/my/dashboard", handle_my_dashboard)
    app.router.add_post("/api/my/shifts", handle_my_create_shift)
    app.router.add_patch("/api/my/shifts/{shift_id}", handle_my_patch_shift)
    app.router.add_put("/api/my/card", handle_my_put_card)
    app.router.add_post("/api/my/payout-requests", handle_my_create_payout_request)
    app.router.add_post("/api/my/payout-requests/{req_id}/cancel", handle_my_cancel_payout_request)

    app.router.add_get("/api/owner/analytics", handle_owner_analytics)
    app.router.add_get("/api/owner/reports/excel", handle_owner_excel_download)
    app.router.add_post("/api/owner/reports/send_excel", handle_owner_excel_send_chat)

    app.router.add_get("/api/owner/revenues", handle_owner_revenues_list)
    app.router.add_put("/api/owner/revenues/{work_date}", handle_owner_put_revenue)

    app.router.add_get("/api/owner/staff", handle_owner_staff_list)
    app.router.add_post("/api/owner/staff", handle_owner_staff_create)
    app.router.add_get("/api/owner/staff/{emp_id}", handle_owner_staff_detail)
    app.router.add_patch("/api/owner/staff/{emp_id}", handle_owner_staff_patch)
    app.router.add_post("/api/owner/staff/{emp_id}/relink", handle_owner_staff_relink)
    app.router.add_delete("/api/owner/staff/{emp_id}", handle_owner_staff_delete)
    app.router.add_put("/api/owner/staff/{emp_id}/shifts", handle_owner_put_emp_shift)
    app.router.add_post("/api/owner/staff/{emp_id}/adjustments", handle_owner_post_adjustment)

    app.router.add_get("/api/owner/payouts", handle_owner_payouts_overview)
    app.router.add_get("/api/owner/payouts/{emp_id}/preview", handle_owner_payout_preview)
    app.router.add_post("/api/owner/payouts", handle_owner_post_payout)
    app.router.add_get("/api/owner/payout-requests", handle_owner_payout_requests_list)
    app.router.add_post("/api/owner/payout-requests/{req_id}/accept", handle_owner_payout_request_accept)
    app.router.add_post("/api/owner/payout-requests/{req_id}/reject", handle_owner_payout_request_reject)
    app.router.add_post("/api/owner/payout-requests/{req_id}/pay", handle_owner_payout_request_pay)

    app.router.add_get("/api/owner/admin-perms", handle_owner_admin_perms)
    app.router.add_post("/api/owner/admin-perms/toggle", handle_owner_admin_perm_toggle)

    return app
