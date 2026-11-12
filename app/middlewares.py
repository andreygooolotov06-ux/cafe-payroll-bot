from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.services.access import ActorContext, resolve_actor


class AppContextMiddleware(BaseMiddleware):
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.session_maker = session_maker
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        async with self.session_maker() as session:
            data["session"] = session
            data["settings"] = self.settings
            if user is not None:
                actor: ActorContext = await resolve_actor(
                    session,
                    user.id,
                    self.settings.owner_ids,
                    self.settings.owner_usernames,
                    user.username,
                )
                data["actor"] = actor
            return await handler(event, data)
