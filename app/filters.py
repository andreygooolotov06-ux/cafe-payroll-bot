from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from app.services.access import ActorContext, ActorRole


class IsOwnerFilter(BaseFilter):
    async def __call__(self, event: TelegramObject, actor: ActorContext | None = None) -> bool:
        return actor is not None and actor.role == ActorRole.OWNER


class IsOwnerOrAdminFilter(BaseFilter):
    async def __call__(self, event: TelegramObject, actor: ActorContext | None = None) -> bool:
        return actor is not None and actor.role in {ActorRole.OWNER, ActorRole.ADMIN}


class IsEmployeeFilter(BaseFilter):
    async def __call__(self, event: TelegramObject, actor: ActorContext | None = None) -> bool:
        return actor is not None and actor.is_staff_member
