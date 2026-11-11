#!/usr/bin/env python3
"""
Offline demo: synthetic NSE session → NIFTY Signal v1 → plan → risk → exit.
No Angel One credentials required.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import simulate_exit
from src.core.config import load_trading_config
from src.db.store import get_store
from src.market.calendar import IST
from src.models.trade import Mode
from src.strategies.nifty_signal import find_nifty_signal_trigger
from src.strategies.pipeline import evaluate_symbol_day
from src.strategies.risk import check_entry
from src.strategies.exit_plan import build_plan


def _session_frame(day: dt.date, path: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """path entries: open, high, low, close, volume for each 5m bar from 09:15."""
    open_t = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
    rows = []
    for i, (o, h, l, c, v) in enumerate(path):
        rows.append(
            {
                "datetime": open_t + dt.timedelta(minutes=5 * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            }
        )
    return pd.DataFrame(rows)


def synthetic_ce_day(day: dt.date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Build a CE path:
      OR 1000–1010 (09:15–09:30)
      breakout above 1010 with volume
      retest hold above 1010
      bullish confirmation
      entry next open
    """
    prior_day = day - dt.timedelta(days=1)
    while prior_day.weekday() >= 5:
        prior_day -= dt.timedelta(days=1)

    # Prior session for PDH/PDL/PDC
    prior_path = []
    px = 1005.0
    for i in range(75):
        o = px
        c = px + 0.2
        prior_path.append((o, max(o, c) + 1, min(o, c) - 1, c, 40000 + i * 50))
        px = c
    # Force prior high/low
    prior_path[10] = (1008, 1025, 1006, 1020, 80000)  # PDH ~1025
    prior_df = _session_frame(prior_day, prior_path)

    # Today: OR bars 0–2 (09:15, 09:20, 09:25)
    path = []
    # OR: high 1010, low 1000
    path.append((1002, 1008, 1000, 1005, 50000))  # 09:15
    path.append((1005, 1010, 1003, 1007, 52000))  # 09:20
    path.append((1007, 1009, 1004, 1006, 51000))  # 09:25
    # After OR — grind above VWAP toward breakout
    path.append((1006, 1009, 1005, 1008, 55000))  # 09:30
    path.append((1008, 1011, 1007, 1010.5, 90000))  # 09:35 breakout close > 1010
    # Retest: touch ~1010, close above
    path.append((1010.2, 1011, 1009.8, 1010.4, 60000))  # 09:40 retest
    # Confirmation bullish
    path.append((1010.5, 1014, 1010.2, 1013.5, 70000))  # 09:45 confirm
    # Entry bar open
    path.append((1013.8, 1018, 1013, 1017, 65000))  # 09:50 entry
    # Follow-through toward target
    for i in range(8, 75):
        o = 1017 + (i - 8) * 1.5
        c = o + 1.2
        path.append((o, max(o, c) + 1, min(o, c) - 0.5, c, 45000 + i * 10))

    df = _session_frame(day, path)
    # 15m: rising closes so EMA20 bullish
    open_t = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
    rows_15 = []
    # Include prior day 15m warmup
    p_open = dt.datetime.combine(prior_day, dt.time(9, 15), tzinfo=IST)
    px = 990.0
    for i in range(25):
        o = px
        c = px + 1.0
        rows_15.append(
            {
                "datetime": p_open + dt.timedelta(minutes=15 * i),
                "open": o,
                "high": max(o, c) + 2,
                "low": min(o, c) - 1,
                "close": c,
                "volume": 100000,
            }
        )
        px = c
    px = 1005.0
    for i in range(20):
        o = px
        c = px + 1.5
        rows_15.append(
            {
                "datetime": open_t + dt.timedelta(minutes=15 * i),
                "open": o,
                "high": max(o, c) + 2,
                "low": min(o, c) - 1,
                "close": c,
                "volume": 120000,
            }
        )
        px = c
    bars_15 = pd.DataFrame(rows_15)
    return df, bars_15, prior_df


def main():
    cfg = load_trading_config()
    day = dt.date(2026, 3, 10)  # Tuesday
    df, bars_15, prior_df = synthetic_ce_day(day)

    trig = find_nifty_signal_trigger(df, day, cfg, bars_15m=bars_15, prior_df=prior_df)
    print("=== TRIGGER ===")
    print(json.dumps({k: v for k, v in trig.items() if k != "strategy_steps"}, indent=2, default=str))
    if trig.get("status") != "triggered":
        print("No trigger on synthetic CE day — unexpected")
        sys.exit(1)

    trade = evaluate_symbol_day(
        "NIFTY",
        df,
        day,
        cfg=cfg,
        mode=Mode.BACKTEST,
        bars_15m=bars_15,
        prior_df=prior_df,
    )
    if trade is None:
        print("evaluate_symbol_day returned None — unexpected")
        sys.exit(1)

    print("\n=== TRADE / RISK ===")
    print(json.dumps(trade.model_dump(mode="json"), indent=2, default=str))

    # Consecutive-loss block check
    plan = build_plan(
        symbol="NIFTY",
        entry=float(trig["entry"]),
        or_high=float(trig["or_high"]),
        or_low=float(trig["or_low"]),
        cfg=cfg,
        signal_low=trig.get("signal_low"),
        signal_high=trig.get("signal_high"),
        side=trig.get("side", "buy"),
        planned_target=trig.get("computed_target"),
        planned_stop=trig.get("computed_stop"),
        why=trig.get("why", ""),
    )
    blocked = check_entry(plan, cfg, consecutive_losses=2, skip_session_checks=True)
    assert not blocked.passed, "expected streak block"
    print("\nStreak block OK:", blocked.reasons)

    if trade.risk_verdict and trade.risk_verdict.passed:
        trade.mark_open(trade.planned_entry, trade.entry_time, trade.qty)
        exit_px, exit_tm, reason, trail_meta = simulate_exit(
            df,
            trade.entry_time,
            trade.entry_price,
            trade.planned_stop,
            trade.planned_target,
            trade.flat_by or cfg.get("flat_by", "15:10"),
            day,
            side=trade.side,
            cfg=cfg,
            qty=trade.qty,
        )
        trade.mark_closed(exit_px, exit_tm, reason)
        store = get_store()
        store.upsert_trade(trade)
        print("\n=== EXIT ===")
        print(
            json.dumps(
                {
                    "exit": exit_px,
                    "time": exit_tm,
                    "reason": reason.value,
                    "pnl": trade.pnl,
                    "pnl_r": trade.pnl_r,
                },
                indent=2,
                default=str,
            )
        )
    print("\nOffline NIFTY Signal v1 demo OK")


if __name__ == "__main__":
    main()
