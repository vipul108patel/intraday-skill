#!/usr/bin/env python3
"""CLI live trading against Angel One — Nifty and/or Stocks desks.

Examples:
  python scripts/run_live.py --status
  python scripts/run_live.py --arm
  python scripts/run_live.py --disarm
  python scripts/run_live.py --desk nifty
  python scripts/run_live.py --desk all --loop --every 60
  # Nifty entry scan ~60s, Stocks entry scan ~300s (5m) — exits still every loop tick
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import live_armed, load_desk_config, normalize_desk, set_live_arm
from src.live.engine import LiveEngine, tick_all_desks


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str), flush=True)


def _status_payload(desk: str) -> dict:
    desks = ["nifty", "stocks"] if desk == "all" else [normalize_desk(desk)]
    return {
        "global_armed": live_armed(),
        "desks": {d: LiveEngine(desk=d).status() for d in desks},
    }


def main():
    p = argparse.ArgumentParser(description="Live Angel One auto-trading (NFO)")
    p.add_argument("--desk", default="all", choices=["nifty", "stocks", "all"])
    p.add_argument("--loop", action="store_true")
    p.add_argument(
        "--every",
        type=int,
        default=None,
        help="Loop sleep seconds (default 60). Stocks entry scan still respects scan_poll_seconds=300.",
    )
    p.add_argument("--arm", action="store_true", help="Enable live (paper=false)")
    p.add_argument("--disarm", action="store_true", help="Kill switch")
    p.add_argument("--status", action="store_true", help="RMS / arm status only")
    p.add_argument(
        "--tick",
        action="store_true",
        help="Run one live scan/manage cycle (default if not --arm/--disarm/--status only)",
    )
    args = p.parse_args()

    if args.arm and args.disarm:
        print("Use only one of --arm / --disarm", file=sys.stderr)
        sys.exit(2)

    if args.arm:
        _print({"action": "armed", **set_live_arm(True)})
    if args.disarm:
        _print({"action": "disarmed", **set_live_arm(False)})

    if args.status or (args.arm or args.disarm) and not args.tick and not args.loop:
        _print(_status_payload(args.desk))
        return

    if not live_armed():
        _print(
            {
                "error": "Live not armed",
                "hint": "python scripts/run_live.py --arm",
                "status": _status_payload(args.desk),
            }
        )
        sys.exit(1)

    def once():
        if args.desk == "all":
            return tick_all_desks()
        return LiveEngine(desk=normalize_desk(args.desk)).tick()

    if args.loop:
        every = max(15, int(args.every if args.every is not None else 60))
        nifty_poll = int(load_desk_config("nifty").get("scan_poll_seconds") or 60)
        stocks_poll = int(load_desk_config("stocks").get("scan_poll_seconds") or 300)
        print(
            f"Live loop desk={args.desk} tick every {every}s · "
            f"Nifty entry scan ~{nifty_poll}s · Stocks entry scan ~{stocks_poll}s · "
            f"exits every tick · Ctrl+C to stop",
            flush=True,
        )
        while True:
            _print(once())
            time.sleep(every)
    else:
        _print(once())


if __name__ == "__main__":
    main()
