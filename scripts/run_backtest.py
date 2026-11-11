#!/usr/bin/env python3
"""CLI backtest against Angel One historical candles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import run_backtest
from src.core.config import load_desk_config, normalize_desk


def main():
    p = argparse.ArgumentParser(description="Backtest BOS skill on NSE via Angel One")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--symbols", type=str, default=None, help="Comma-separated, else watchlist")
    p.add_argument("--desk", default="nifty", choices=["nifty", "stocks"])
    args = p.parse_args()
    desk = normalize_desk(args.desk)
    cfg = load_desk_config(desk)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [s.upper() for s in (cfg.get("watchlist") or [])]
        limit = int(cfg.get("backtest_symbol_limit") or 0)
        if limit > 0:
            symbols = symbols[:limit]
    summary = run_backtest(
        symbols=symbols,
        days=args.days or cfg.get("backtest_default_days", 30),
        cfg=cfg,
    )
    print(json.dumps(summary.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    main()