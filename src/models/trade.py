"""Trade / setup models with full audit trail for backtest, paper, and live."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class TradeStatus(str, Enum):
    SETUP = "setup"
    WAITING = "waiting"
    TRIGGERED = "triggered"
    RISK_BLOCKED = "risk_blocked"
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ExitReason(str, Enum):
    TARGET = "target"
    STOP = "stop"
    TRAIL = "trail"
    FLAT_BY = "flat_by"
    MANUAL = "manual"
    EOD = "eod"
    NONE = "none"


class SetupHit(BaseModel):
    symbol: str
    strategy: str
    as_of: datetime
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    trigger_price: Optional[float] = None
    trigger_time: Optional[datetime] = None
    pct_over: Optional[float] = None
    details: dict[str, Any] = Field(default_factory=dict)
    why: str = ""


class TradePlan(BaseModel):
    symbol: str
    side: str = "buy"
    strategy: str
    setup: str
    entry: float
    stop: float
    target: float
    stop_rule: str
    target_r: float
    risk_per_share: float
    reward_risk: float
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    entry_time: Optional[datetime] = None
    flat_by: str = "15:15"
    why: str = ""
    strategy_steps: list[str] = Field(default_factory=list)


class RiskVerdict(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    max_loss: float = 0.0
    reward_risk: float = 0.0
    qty: int = 0
    notional: float = 0.0


class TradeRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    mode: Mode
    status: TradeStatus = TradeStatus.SETUP
    symbol: str
    strategy: str
    setup: str
    side: str = "buy"
    desk: str = "nifty"  # nifty | stocks — shared capital, separate books

    # Strategy narrative
    why: str = ""
    strategy_steps: list[str] = Field(default_factory=list)
    how: str = ""

    # Levels
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    planned_entry: Optional[float] = None
    planned_stop: Optional[float] = None
    planned_target: Optional[float] = None
    stop_rule: Optional[str] = None
    target_r: Optional[float] = None
    risk_per_share: Optional[float] = None
    reward_risk: Optional[float] = None

    # Sizing / risk
    qty: int = 0
    risk_verdict: Optional[RiskVerdict] = None
    max_loss_planned: Optional[float] = None

    # Options contract (NIFTY CE/PE)
    option_type: Optional[str] = None  # CE / PE
    strike: Optional[int] = None
    expiry: Optional[str] = None
    option_symbol: Optional[str] = None
    moneyness: Optional[str] = None
    spot_ref: Optional[float] = None
    option_premium: Optional[float] = None  # CE/PE LTP per unit
    invested_amount: Optional[float] = None  # premium × qty (lot cost, e.g. ₹13450)

    # Fills
    entry_time: Optional[datetime] = None
    entry_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: ExitReason = ExitReason.NONE

    # P&L
    pnl: Optional[float] = None
    pnl_r: Optional[float] = None
    fees: float = 0.0

    # Meta
    session_date: Optional[str] = None
    flat_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)

    def mark_open(self, price: float, when: datetime, qty: int) -> None:
        self.status = TradeStatus.OPEN
        self.entry_price = price
        self.entry_time = when
        self.qty = qty
        self.updated_at = datetime.utcnow()

    def mark_closed(
        self,
        price: float,
        when: datetime,
        reason: ExitReason,
        fees: float = 0.0,
    ) -> None:
        self.status = TradeStatus.CLOSED
        self.exit_price = price
        self.exit_time = when
        self.exit_reason = reason
        self.fees = fees
        if self.entry_price is not None and self.qty:
            direction = 1 if self.side == "buy" else -1
            self.pnl = direction * (price - self.entry_price) * self.qty - fees
            if self.risk_per_share and self.risk_per_share > 0:
                self.pnl_r = (direction * (price - self.entry_price)) / self.risk_per_share
        self.updated_at = datetime.utcnow()


class BacktestSummary(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime
    ended_at: datetime
    symbols: list[str]
    strategy: str
    days: int
    trades: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_r: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    notes: str = ""