from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class OwnerBinding(Base):
    """Зберігає прив'язку Telegram ID власниці за @username після першого /start."""

    __tablename__ = "owner_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    employees: Mapped[list[Employee]] = relationship(back_populates="role")


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)

    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    hourly_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    revenue_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # None означає, що Аня ще не внесла тариф за 1 людину на банкеті
    banquet_rate_per_person: Mapped[float | None] = mapped_column(Float, nullable=True)

    invite_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    card_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    last_end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    role: Mapped[Role] = relationship(back_populates="employees", lazy="selectin")
    shifts: Mapped[list[Shift]] = relationship(back_populates="employee", cascade="all, delete-orphan")
    adjustments: Mapped[list[Adjustment]] = relationship(back_populates="employee", cascade="all, delete-orphan")
    payouts: Mapped[list[Payout]] = relationship(back_populates="employee", cascade="all, delete-orphan")


class DailyRevenue(Base):
    __tablename__ = "daily_revenues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_shift_employee_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)
    hours: Mapped[float] = mapped_column(Float, nullable=False)

    # Фіксація ставок на момент внесення зміни
    hourly_rate_snapshot: Mapped[float] = mapped_column(Float, nullable=False)
    percent_snapshot: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    is_banquet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    banquet_guests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    banquet_rate_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    employee: Mapped[Employee] = relationship(back_populates="shifts", lazy="selectin")


class Adjustment(Base):
    """Премії (bonus) та штрафи (fine)."""

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    adj_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "bonus" | "fine"
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    employee: Mapped[Employee] = relationship(back_populates="adjustments", lazy="selectin")


class Payout(Base):
    """Фактично видані виплати / аванси."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paid_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    employee: Mapped[Employee] = relationship(back_populates="payouts", lazy="selectin")


class AdminPermissionSettings(Base):
    """Права доступу, які власниця (Аня) налаштовує для працівників з роллю Адмін."""

    __tablename__ = "admin_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    can_enter_revenue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_view_staff_shifts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_edit_hours: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_view_salaries_reports: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_adjust_money: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_payout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_manage_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ScheduledRunLog(Base):
    """Журнал останніх запусків щоденних авто-нагадувань, щоб не дублювати повідомлення."""

    __tablename__ = "scheduled_runs"

    job_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_run_date: Mapped[date] = mapped_column(Date, nullable=False)


class IdempotencyRecord(Base):
    """Захист від повторного виконання фінансових операцій при мережевих ретраях Mini App."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("key", "actor_tg_id", "endpoint", name="uq_idempotency_key_actor_endpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PayoutRequest(Base):
    """Запити працівників на виплату зарплати чи авансу з прив'язкою до картки."""

    __tablename__ = "payout_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    card_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    payout_id: Mapped[int | None] = mapped_column(
        ForeignKey("payouts.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    employee: Mapped[Employee] = relationship(lazy="selectin")
