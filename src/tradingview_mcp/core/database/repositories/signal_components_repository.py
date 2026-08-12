"""Read-only helper: fetch a signal's persisted scoring components."""
from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.database.models.signals import SignalComponent


async def get_components_for_signal(session: AsyncSession, signal_id: UUID) -> Sequence[SignalComponent]:
    return (
        await session.execute(
            select(SignalComponent).where(SignalComponent.signal_id == signal_id)
        )
    ).scalars().all()
