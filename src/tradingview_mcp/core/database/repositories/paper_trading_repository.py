"""Persistence for `paper_orders` / `paper_fills` / `paper_positions` /
`paper_equity_snapshots`."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.database.models.paper_trading import (
    PaperEquitySnapshot,
    PaperFill,
    PaperOrder,
    PaperPosition,
)


def _d(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v if isinstance(v, Decimal) else Decimal(str(v)))


async def create_paper_order(
    session: AsyncSession,
    *,
    signal_id: Optional[UUID],
    symbol: str,
    direction: str,
    order_type: str,
    price: Optional[Decimal],
    quantity: Decimal,
    status: str = "NEW",
) -> UUID:
    row = PaperOrder(
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        order_type=order_type,
        price=_d(price),
        quantity=_d(quantity),
        status=status,
    )
    session.add(row)
    await session.flush()
    return row.id


async def record_fill(
    session: AsyncSession,
    *,
    order_id: UUID,
    price: Decimal,
    quantity: Decimal,
    fee: Decimal,
    slippage: Decimal,
) -> UUID:
    row = PaperFill(
        order_id=order_id,
        price=_d(price),
        quantity=_d(quantity),
        fee=_d(fee),
        slippage=_d(slippage),
    )
    session.add(row)
    await session.flush()
    return row.id


async def open_position(
    session: AsyncSession,
    *,
    signal_id: Optional[UUID],
    symbol: str,
    direction: str,
    entry_price: Decimal,
    quantity: Decimal,
    stop_loss: Optional[Decimal],
    take_profit_1: Optional[Decimal],
    take_profit_2: Optional[Decimal],
) -> UUID:
    row = PaperPosition(
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        entry_price=_d(entry_price),
        quantity=_d(quantity),
        stop_loss=_d(stop_loss),
        take_profit_1=_d(take_profit_1),
        take_profit_2=_d(take_profit_2),
        status="OPEN",
    )
    session.add(row)
    await session.flush()
    return row.id


async def close_position(
    session: AsyncSession,
    position: PaperPosition,
    *,
    realized_pnl: Decimal,
    fees_paid: Decimal,
    funding_paid: Decimal = Decimal("0"),
) -> None:
    position.status = "CLOSED"
    position.realized_pnl = _d(Decimal(position.realized_pnl or "0") + realized_pnl)
    position.fees_paid = _d(Decimal(position.fees_paid or "0") + fees_paid)
    position.funding_paid = _d(Decimal(position.funding_paid or "0") + funding_paid)
    position.closed_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()


async def record_equity_snapshot(
    session: AsyncSession,
    *,
    as_of: dt.datetime,
    balance: Decimal,
    equity: Decimal,
    open_positions: int,
    daily_pnl: Decimal,
) -> int:
    row = PaperEquitySnapshot(
        as_of=as_of,
        balance=_d(balance),
        equity=_d(equity),
        open_positions=open_positions,
        daily_pnl=_d(daily_pnl),
    )
    session.add(row)
    await session.flush()
    return row.id
