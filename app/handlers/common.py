from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Employee
from app.keyboards.inline import HOME_BTN
from app.keyboards.reply import (
    OWNER_MENU_TEXTS,
    admin_main_menu,
    employee_main_menu,
    owner_main_menu,
)
from app.services.access import (
    ActorContext,
    ActorRole,
    bind_invite_code,
    get_admin_perms,
    get_all_owner_ids,
    is_admin_role_name,
)
from app.services.tunnel import miniapp_runtime
from app.utils.formatting import esc

start_router = Router(name="common_start")
fallback_router = Router(name="common_fallback")


def _menu_for_actor(actor: ActorContext) -> ReplyKeyboardMarkup | None:
    if actor.role == ActorRole.OWNER:
        return owner_main_menu()
    if actor.role == ActorRole.ADMIN:
        return admin_main_menu(actor.admin_perms)
    if actor.role == ActorRole.EMPLOYEE:
        return employee_main_menu()
    return None


async def _reply_menu_for_employee(session: AsyncSession, emp: Employee) -> ReplyKeyboardMarkup:
    if emp.role and is_admin_role_name(emp.role.name):
        perms = await get_admin_perms(session)
        return admin_main_menu(perms)
    return employee_main_menu()


async def _maybe_send_miniapp_card(message: Message) -> None:
    if not miniapp_runtime.url:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Відкрити Mini App",
                    web_app=WebAppInfo(url=miniapp_runtime.url),
                )
            ]
        ]
    )
    await message.answer(
        "📱 <b>Доступний візуальний Mini App кабінет</b>\n"
        "Можеш працювати у звичайному чаті кнопками знизу або відкрити повноекранний кабінет "
        "і перенести його іконкою на головний екран телефону ✨",
        reply_markup=kb,
    )


@start_router.message(StateFilter("*"), Command("menu"))
async def cmd_menu(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    await state.clear()
    menu = _menu_for_actor(actor)
    if menu is not None:
        await message.answer(
            "🏠 <b>Головне меню</b>\nОбери потрібну дію внизу (або /app для Mini App) ✨",
            reply_markup=menu,
        )
    else:
        await message.answer("Недостатньо прав 🔒")


@start_router.message(StateFilter("*"), Command("app"))
async def cmd_app(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    await state.clear()
    if actor.role == ActorRole.UNAUTHORIZED:
        await message.answer("Недостатньо прав 🔒")
        return
    if not miniapp_runtime.url:
        await message.answer(
            "Mini App зараз запускає HTTPS-канал, спробуй за кілька секунд або скористайся кнопками внизу 😊"
        )
        return
    await _maybe_send_miniapp_card(message)


@start_router.message(StateFilter("*"), CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    actor: ActorContext,
) -> None:
    await state.clear()
    args = (command.args or "").strip()

    if args:
        if actor.role == ActorRole.OWNER:
            await message.answer(
                "Привіт, Аню! 🎀\n"
                "Ти є власницею кафе, а це персональне посилання призначене для працівника. Передай його потрібному співробітнику ✨",
                reply_markup=owner_main_menu(),
            )
            return

        if message.from_user is None:
            return

        ok, linked_emp, reason = await bind_invite_code(
            session=session,
            invite_code=args,
            telegram_id=message.from_user.id,
            telegram_username=message.from_user.username,
        )
        if not ok or linked_emp is None:
            await message.answer("Недостатньо прав 🔒")
            return

        role_name = linked_emp.role.name if linked_emp.role else "працівник"
        emp_menu = await _reply_menu_for_employee(session, linked_emp)
        if reason == "already_linked_self":
            await message.answer(
                f"З поверненням, <b>{esc(linked_emp.full_name)}</b>! 🌸\n"
                f"Твій профіль ({esc(role_name)}) вже підключено — користуйся кнопками внизу.\n"
                "<i>(Візуальний кабінет: /app)</i>",
                reply_markup=emp_menu,
            )
            return

        extra_admin = (
            "\nТакож тобі відкриті інструменти адміністратора у меню внизу 👑"
            if is_admin_role_name(role_name)
            else ""
        )
        await message.answer(
            f"Привіт, <b>{esc(linked_emp.full_name)}</b>! 🌸\n"
            f"Тебе успішно підключено до бота на посаду <b>{esc(role_name)}</b>.{extra_admin}\n"
            "Бот запам'ятав твій Telegram, тож тепер усе відкриватиметься автоматично. Гарної зміни ✨\n"
            "<i>(Візуальний кабінет: /app)</i>",
            reply_markup=emp_menu,
        )

        owner_ids = await get_all_owner_ids(session, settings)
        uname_str = f" (@{esc(linked_emp.telegram_username)})" if linked_emp.telegram_username else ""
        alert_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="👤 Відкрити картку", callback_data=f"own:emp:{linked_emp.id}")]
            ]
        )
        for owner_id in owner_ids:
            try:
                await bot.send_message(
                    owner_id,
                    f"🟢 <b>{esc(linked_emp.full_name)}</b> ({esc(role_name)}){uname_str} "
                    "перейшов за посиланням та успішно підключився до бота! ✨\n"
                    "Статус змінено з ⏳ очікування на 🟢 <b>Підключено</b>.",
                    reply_markup=alert_kb,
                )
            except Exception:
                pass
        return

    # Звичайний /start (у тому числі після повного перезапуску бота — все визначається по Telegram ID)
    if actor.role == ActorRole.OWNER:
        await message.answer(
            "Привіт, Аню! 🎀\n"
            "Рада тебе бачити. Усі дані збережені — обирай потрібну вкладку внизу ✨\n"
            "<i>(Візуальний Mini App кабінет: /app)</i>",
            reply_markup=owner_main_menu(),
        )
        return

    if actor.role == ActorRole.ADMIN and actor.employee is not None:
        role_name = actor.employee.role.name if actor.employee.role else "Адмін"
        await message.answer(
            f"Привіт, <b>{esc(actor.employee.full_name)}</b>! 👑\n"
            f"Твоя роль: <b>{esc(role_name)}</b>.\n"
            "Обирай потрібну дію або панель адміна в меню внизу ✨\n"
            "<i>(Візуальний Mini App кабінет: /app)</i>",
            reply_markup=admin_main_menu(actor.admin_perms),
        )
        return

    if actor.role == ActorRole.EMPLOYEE and actor.employee is not None:
        role_name = actor.employee.role.name if actor.employee.role else "працівник"
        await message.answer(
            f"Привіт, <b>{esc(actor.employee.full_name)}</b>! 🌸\n"
            f"Твоя роль: <b>{esc(role_name)}</b>.\n"
            "Обирай потрібну дію в меню внизу 😊\n"
            "<i>(Візуальний Mini App кабінет: /app)</i>",
            reply_markup=employee_main_menu(),
        )
        return

    await message.answer(
        "Недостатньо прав 🔒\n"
        "Попроси в Ані персональне посилання для підключення до бота."
    )


@start_router.message(StateFilter("*"), Command("cancel"))
async def cmd_cancel(
    message: Message,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    await state.clear()
    menu = _menu_for_actor(actor)
    if menu is not None:
        await message.answer("Дію скасовано 👌", reply_markup=menu)
    else:
        await message.answer("Недостатньо прав 🔒")


@start_router.callback_query(StateFilter("*"), F.data == "nav:home")
async def cb_nav_home(
    callback: CallbackQuery,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if actor.role == ActorRole.UNAUTHORIZED:
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        menu = _menu_for_actor(actor)
        await callback.message.answer(
            "🏠 <b>Головне меню</b>\nОбери потрібну дію внизу (або /app для Mini App) ✨",
            reply_markup=menu,
        )
    await callback.answer()


@start_router.callback_query(StateFilter("*"), F.data == "fsm:cancel")
async def cb_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    actor: ActorContext,
) -> None:
    if actor.role == ActorRole.UNAUTHORIZED:
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "Дію скасовано 👌",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[HOME_BTN]]),
        )
    await callback.answer()


@start_router.callback_query(StateFilter("*"), F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ----------------- ЗАХИСТ ВІД НЕСАНКЦІОНОВАНОГО ДОСТУПУ -----------------

@fallback_router.callback_query(StateFilter("*"))
async def cb_unauthorized_or_unknown(
    callback: CallbackQuery,
    actor: ActorContext,
) -> None:
    if actor.role == ActorRole.UNAUTHORIZED:
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return
    if actor.role == ActorRole.EMPLOYEE and (callback.data or "").startswith("own:"):
        await callback.answer("Недостатньо прав 🔒", show_alert=True)
        return
    await callback.answer("Ця кнопка вже застаріла, відкрий вкладку з меню внизу 😊", show_alert=True)


@fallback_router.message(StateFilter("*"))
async def msg_unauthorized_or_unknown(
    message: Message,
    actor: ActorContext,
) -> None:
    if actor.role == ActorRole.UNAUTHORIZED:
        await message.answer("Недостатньо прав 🔒")
        return

    if actor.role == ActorRole.EMPLOYEE:
        if message.text in OWNER_MENU_TEXTS:
            await message.answer("Недостатньо прав 🔒", reply_markup=employee_main_menu())
            return
        await message.answer("Обери потрібну дію в меню внизу 😊", reply_markup=employee_main_menu())
        return

    if actor.role == ActorRole.ADMIN:
        admin_kb = admin_main_menu(actor.admin_perms)
        if message.text in OWNER_MENU_TEXTS:
            await message.answer("Недостатньо прав 🔒", reply_markup=admin_kb)
            return
        await message.answer("Обери потрібну дію в меню внизу 😊", reply_markup=admin_kb)
        return

    await message.answer("Обери потрібну вкладку в меню внизу 😊", reply_markup=owner_main_menu())
