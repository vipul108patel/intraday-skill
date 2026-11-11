#!/usr/bin/env python3
"""
Offline demo: synthetic NSE session → nifty_bos_v1 → plan → risk.
No Angel One credentials required.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import load_trading_config
from src.market.calendar import IST
from src.models.trade import Mode
from src.strategies.nifty_bos import find_nifty_bos_trigger
from src.strategies.pipeline import evaluate_symbol_day
import pandas as pd


def _session_frame(day: dt.date, path: list) -> pd.DataFrame:
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


def synthetic_bos_ce(day: dt.date):
    prior_day = day - dt.timedelta(days=1)
    while prior_day.weekday() >= 5:
        prior_day -= dt.timedelta(days=1)

    prior_path = []
    px = 24000.0
    for i in range(75):
        o = px
        c = px + 2.0
        prior_path.append((o, max(o, c) + 5, min(o, c) - 5, c, 80000))
        px = c
    prior_df = _session_frame(prior_day, prior_path)

    path = [
        (24150, 24180, 24140, 24160, 100000),
        (24160, 24190, 24150, 24170, 105000),
        (24170, 24185, 24155, 24165, 102000),
    ]
    px = 24165.0
    for i in range(30):
        delta = -8 if i % 5 == 4 else 12
        o = px
        c = px + delta
        path.append((o, max(o, c) + 6, min(o, c) - 4, c, 120000 + i * 2000))
        px = c
    o = px
    c = px + 40
    path.append((o, c + 10, o - 5, c, 250000))
    px = c
    path.append((px + 2, px + 25, px - 3, px + 20, 180000))
    for i in range(10):
        o = px
        c = px + 8
        path.append((o, max(o, c) + 5, min(o, c) - 3, c, 100000))
        px = c
    df = _session_frame(day, path)

    rows_15 = []
    p_open = dt.datetime.combine(prior_day, dt.time(9, 15), tzinfo=IST)
    px = 23800.0
    for i in range(40):
        o = px
        c = px + 8
        rows_15.append(
            {
                "datetime": p_open + dt.timedelta(minutes=15 * i),
                "open": o,
                "high": max(o, c) + 10,
                "low": min(o, c) - 5,
                "close": c,
                "volume": 200000,
            }
        )
        px = c
    open_t = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
    px = 24150.0
    for i in range(25):
        o = px
        c = px + 10
        rows_15.append(
            {
                "datetime": open_t + dt.timedelta(minutes=15 * i),
                "open": o,
                "high": max(o, c) + 8,
                "low": min(o, c) - 4,
                "close": c,
                "volume": 220000,
            }
        )
        px = c
    return df, pd.DataFrame(rows_15), prior_df


def main():
    cfg = load_trading_config()
    cfg = {**cfg, "strategy": "nifty_bos_v1"}
    day = dt.date(2026, 3, 10)
    df, bars_15, prior_df = synthetic_bos_ce(day)

    trig = find_nifty_bos_trigger(df, day, cfg, bars_15m=bars_15, prior_df=prior_df)
    print("=== TRIGGER ===")
    print(
        json.dumps(
            {k: v for k, v in trig.items() if k != "strategy_steps"},
            indent=2,
            default=str,
        )
    )
    if trig.get("status") != "triggered":
        print("No BOS trigger — unexpected")
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
    assert trade is not None and trade.risk_verdict and trade.risk_verdict.passed
    print("\n=== TRADE ===")
    print(
        json.dumps(
            {
                "status": trade.status.value,
                "side": trade.side,
                "entry": trade.planned_entry,
                "stop": trade.planned_stop,
                "target": trade.planned_target,
                "how": trade.how,
            },
            indent=2,
            default=str,
        )
    )
    print("\nOffline nifty_bos_v1 demo OK")


if __name__ == "__main__":
    main()
