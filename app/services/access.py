from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import AdminPermissionSettings, Employee, OwnerBinding

ADMIN_ROLE_NAMES = frozenset({"адмін", "админ", "адміністратор", "администратор"})

ADMIN_PERM_LABELS: tuple[tuple[str, str], ...] = (
    ("can_enter_revenue", "💵 Каса дня (перегляд і внесення)"),
    ("can_view_staff_shifts", "📋 Персонал і перегляд їхніх змін"),
    ("can_edit_hours", "🕒 Зміна годин персоналу"),
    ("can_view_salaries_reports", "📊 Зарплати, ставки, звіти та Excel"),
    ("can_adjust_money", "🎁 Премії та штрафи"),
    ("can_payout", "💸 Виплати і баланси"),
    ("can_manage_staff", "⚙️ Додавання/видалення людей і зміна ставок"),
)


def is_admin_role_name(name: str | None) -> bool:
    if not name:
        return False
    return name.strip().lower() in ADMIN_ROLE_NAMES


class ActorRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    EMPLOYEE = "employee"
    UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True)
class AdminPerms:
    can_enter_revenue: bool = True
    can_view_staff_shifts: bool = True
    can_edit_hours: bool = False
    can_view_salaries_reports: bool = False
    can_adjust_money: bool = False
    can_payout: bool = False
    can_manage_staff: bool = False

    @property
    def can_open_staff_tab(self) -> bool:
        return any([
            self.can_view_staff_shifts,
            self.can_edit_hours,
            self.can_view_salaries_reports,
            self.can_adjust_money,
            self.can_payout,
            self.can_manage_staff,
        ])

    def has(self, perm: str) -> bool:
        return bool(getattr(self, perm, False))


@dataclass(frozen=True)
class ActorContext:
    role: ActorRole
    telegram_id: int
    employee: Employee | None = None
    admin_perms: AdminPerms = AdminPerms()

    @property
    def is_owner(self) -> bool:
        return self.role == ActorRole.OWNER

    @property
    def is_admin(self) -> bool:
        return self.role == ActorRole.ADMIN

    @property
    def is_staff_member(self) -> bool:
        return self.role in {ActorRole.EMPLOYEE, ActorRole.ADMIN} and self.employee is not None

    def has_perm(self, perm: str) -> bool:
        if self.is_owner:
            return True
        if self.is_admin:
            return self.admin_perms.has(perm)
        return False

    @property
    def actor_label(self) -> str:
        if self.is_owner:
            return "Аня"
        if self.is_admin and self.employee is not None:
            return f"Адмін {self.employee.full_name}"
        return "Аня"


async def get_admin_perms(session: AsyncSession) -> AdminPerms:
    row = await session.get(AdminPermissionSettings, 1)
    if row is None:
        row = AdminPermissionSettings(id=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return AdminPerms(
        can_enter_revenue=bool(row.can_enter_revenue),
        can_view_staff_shifts=bool(row.can_view_staff_shifts),
        can_edit_hours=bool(row.can_edit_hours),
        can_view_salaries_reports=bool(row.can_view_salaries_reports),
        can_adjust_money=bool(row.can_adjust_money),
        can_payout=bool(row.can_payout),
        can_manage_staff=bool(row.can_manage_staff),
    )


async def toggle_admin_perm(session: AsyncSession, field_name: str) -> AdminPerms:
    valid_fields = {key for key, _ in ADMIN_PERM_LABELS}
    if field_name not in valid_fields:
        raise ValueError(f"Unknown permission field: {field_name}")

    row = await session.get(AdminPermissionSettings, 1)
    if row is None:
        row = AdminPermissionSettings(id=1)
        session.add(row)
        await session.flush()

    current = bool(getattr(row, field_name))
    setattr(row, field_name, not current)
    await session.commit()
    await session.refresh(row)
    return await get_admin_perms(session)


async def get_connected_admin_employees(session: AsyncSession) -> list[Employee]:
    employees = (
        await session.scalars(
            select(Employee).where(Employee.is_active.is_(True), Employee.telegram_id.is_not(None))
        )
    ).all()
    return [emp for emp in employees if emp.role and is_admin_role_name(emp.role.name)]


async def resolve_actor(
    session: AsyncSession,
    telegram_id: int,
    owner_ids: frozenset[int],
    owner_usernames: frozenset[str] = frozenset(),
    telegram_username: str | None = None,
) -> ActorContext:
    if telegram_id in owner_ids:
        return ActorContext(role=ActorRole.OWNER, telegram_id=telegram_id, employee=None)

    bound_owner = await session.scalar(
        select(OwnerBinding).where(OwnerBinding.telegram_id == telegram_id)
    )
    if bound_owner is not None:
        return ActorContext(role=ActorRole.OWNER, telegram_id=telegram_id, employee=None)

    if telegram_username:
        uname = telegram_username.strip().lstrip("@").lower()
        if uname in owner_usernames:
            existing_binding = await session.scalar(
                select(OwnerBinding).where(OwnerBinding.username == uname)
            )
            if existing_binding is None:
                session.add(OwnerBinding(username=uname, telegram_id=telegram_id))
                await session.commit()
                return ActorContext(role=ActorRole.OWNER, telegram_id=telegram_id, employee=None)
            if existing_binding.telegram_id is None:
                existing_binding.telegram_id = telegram_id
                await session.commit()
                return ActorContext(role=ActorRole.OWNER, telegram_id=telegram_id, employee=None)
            if existing_binding.telegram_id == telegram_id:
                return ActorContext(role=ActorRole.OWNER, telegram_id=telegram_id, employee=None)

    stmt = select(Employee).where(
        Employee.telegram_id == telegram_id,
        Employee.is_active.is_(True),
    )
    employee = await session.scalar(stmt)
    if employee is not None:
        if employee.role and is_admin_role_name(employee.role.name):
            perms = await get_admin_perms(session)
            return ActorContext(
                role=ActorRole.ADMIN,
                telegram_id=telegram_id,
                employee=employee,
                admin_perms=perms,
            )
        return ActorContext(role=ActorRole.EMPLOYEE, telegram_id=telegram_id, employee=employee)

    return ActorContext(role=ActorRole.UNAUTHORIZED, telegram_id=telegram_id, employee=None)


async def get_all_owner_ids(session: AsyncSession, settings: Settings) -> set[int]:
    bound_ids = (
        await session.scalars(
            select(OwnerBinding.telegram_id).where(OwnerBinding.telegram_id.is_not(None))
        )
    ).all()
    result = set(settings.owner_ids)
    for tid in bound_ids:
        if tid is not None:
            result.add(int(tid))
    return result


async def bind_invite_code(
    session: AsyncSession,
    invite_code: str,
    telegram_id: int,
    telegram_username: str | None,
) -> tuple[bool, Employee | None, str]:
    """Прив'язує Telegram ID працівника по одноразовому інвайт-коду."""
    existing_self = await session.scalar(
        select(Employee).where(Employee.telegram_id == telegram_id, Employee.is_active.is_(True))
    )

    target = await session.scalar(
        select(Employee).where(Employee.invite_code == invite_code, Employee.is_active.is_(True))
    )
    if target is None:
        return False, None, "forbidden"

    if target.telegram_id is not None:
        if target.telegram_id == telegram_id:
            return True, target, "already_linked_self"
        return False, None, "forbidden"

    if existing_self is not None and existing_self.id != target.id:
        return False, None, "already_employee"

    target.telegram_id = telegram_id
    target.telegram_username = telegram_username
    target.invite_code = None
    await session.commit()
    await session.refresh(target)
    return True, target, "ok"
