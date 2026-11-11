#!/usr/bin/env python3
"""One paper-trading tick (scan + manage opens) using live Angel One data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import load_desk_config, normalize_desk
from src.paper.engine import PaperEngine


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true", help="Keep ticking until Ctrl+C")
    p.add_argument("--every", type=int, default=None, help="Seconds between ticks")
    p.add_argument(
        "--desk",
        default="nifty",
        choices=["nifty", "stocks"],
        help="Trading desk: nifty (index options) or stocks (Nifty 100 equity)",
    )
    args = p.parse_args()
    desk = normalize_desk(args.desk)
    cfg = load_desk_config(desk)
    engine = PaperEngine(cfg=cfg, desk=desk)
    every = args.every or int(cfg.get("scan_poll_seconds", 60))

    def once():
        result = engine.tick()
        print(json.dumps(result, indent=2, default=str))

    if not args.loop:
        once()
        return

    print(
        f"Paper loop desk={desk} every {every}s · "
        f"watchlist={len(cfg.get('watchlist') or [])} · Ctrl+C to stop"
    )
    while True:
        once()
        time.sleep(every)


if __name__ == "__main__":
    main()