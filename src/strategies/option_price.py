"""Resolve CE/PE premium and lot invested amount (premium × qty)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from src.market.calendar import IST

logger = logging.getLogger(__name__)


def estimate_atm_premium(spot: float, cfg: dict) -> float:
    """Fallback when live/historical option LTP unavailable."""
    pct = float(cfg.get("option_premium_pct_estimate", 0.008))  # ~0.8% of spot
    floor = float(cfg.get("option_premium_floor", 50.0))
    return max(floor, float(spot) * pct)


def invested_from_premium(premium: float, qty: int) -> float:
    return round(float(premium) * int(qty), 2)


def resolve_option_premium(
    option: dict[str, Any],
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    client: Any = None,
) -> tuple[float, str]:
    """
    Return (premium_per_unit, source).
    Prefers live NFO LTP, then historical option candle at as_of, then estimate.
    """
    spot = float(option.get("spot_ref") or 0)
    token = option.get("token")
    tsym = option.get("tradingsymbol") or ""
    exchange = str(option.get("exchange") or "NFO")

    if client is None:
        try:
            from src.angelone.client import get_client

            client = get_client()
        except Exception:
            client = None

    # 1) Live LTP when as_of is "now-ish" or missing
    if client is not None and token and tsym:
        try:
            now = dt.datetime.now(IST)
            use_live = as_of is None or abs((now - as_of).total_seconds()) < 3600
            if use_live:
                px = float(client.get_ltp_by_token(exchange, tsym, str(token)))
                if px > 0:
                    return px, "live_ltp"
        except Exception as e:
            logger.debug("Option live LTP failed %s: %s", tsym, e)

    # 2) Historical candle around as_of
    if client is not None and token and as_of is not None:
        try:
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=IST)
            day = as_of.date()
            start = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
            end = dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST)
            df = client.get_candles_by_token(
                exchange,
                str(token),
                start,
                end,
                interval="FIVE_MINUTE",
                cache_key=f"opt_{tsym}",
            )
            if df is not None and not df.empty:
                bars = df[df["datetime"] <= as_of]
                if bars.empty:
                    bars = df
                px = float(bars.iloc[-1]["close"])
                if px > 0:
                    return px, "hist_candle"
        except Exception as e:
            logger.debug("Option hist premium failed %s: %s", tsym, e)

    # 3) Estimate
    est = estimate_atm_premium(spot or 23000.0, cfg)
    return est, "estimate"
