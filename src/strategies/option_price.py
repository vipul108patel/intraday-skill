"""Resolve CE/PE premium and lot invested amount (premium × qty).

Market mode uses Angel live LTP or historical NFO candles only — no % estimates.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from src.market.calendar import IST

logger = logging.getLogger(__name__)


def estimate_atm_premium(spot: float, cfg: dict) -> float:
    """Legacy fallback — only used when option_premium_mode == estimate."""
    pct = float(cfg.get("option_premium_pct_estimate", 0.008))
    floor = float(cfg.get("option_premium_floor", 50.0))
    return max(floor, float(spot) * pct)


def invested_from_premium(premium: float, qty: int) -> float:
    return round(float(premium) * int(qty), 2)


def _client_or_none(client: Any = None) -> Any:
    if client is not None:
        return client
    try:
        from src.angelone.client import get_client

        return get_client()
    except Exception:
        return None


def market_option_premium(
    option: dict[str, Any],
    as_of: Optional[dt.datetime] = None,
    client: Any = None,
) -> tuple[Optional[float], str]:
    """
    Real premium only: live NFO LTP and/or historical option candle close.
    Returns (None, reason) when market data is unavailable — never invents a price.
    """
    token = option.get("token")
    tsym = option.get("tradingsymbol") or ""
    exchange = str(option.get("exchange") or "NFO")
    client = _client_or_none(client)

    if not token or not tsym:
        return None, "no_option_token"

    if client is None:
        return None, "no_broker_client"

    if as_of is not None and as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=IST)

    now = dt.datetime.now(IST)
    use_live = as_of is None or abs((now - as_of).total_seconds()) < 3600

    if use_live:
        try:
            px = float(client.get_ltp_by_token(exchange, tsym, str(token)))
            if px > 0:
                return px, "live_ltp"
        except Exception as e:
            logger.debug("Option live LTP failed %s: %s", tsym, e)

    if as_of is None:
        return None, "no_as_of_for_hist"

    try:
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
        if df is None or df.empty:
            return None, "empty_option_candles"

        bars = df[df["datetime"] <= as_of]
        if bars.empty:
            # No bar yet at as_of — take first bar of the day only if as_of is before open
            first = df.iloc[0]
            first_t = first["datetime"]
            if getattr(first_t, "tzinfo", None) is None:
                first_t = first_t.replace(tzinfo=IST)
            if as_of < first_t:
                px = float(first["open"])
            else:
                return None, "no_candle_at_as_of"
        else:
            px = float(bars.iloc[-1]["close"])

        if px > 0:
            return px, "hist_candle"
        return None, "non_positive_premium"
    except Exception as e:
        logger.debug("Option hist premium failed %s: %s", tsym, e)
        return None, f"hist_error:{e}"


def resolve_option_premium(
    option: dict[str, Any],
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    client: Any = None,
) -> tuple[Optional[float], str]:
    """
    Return (premium_per_unit, source).

    Default / market mode: live LTP or hist candle only.
    estimate mode: legacy spot×pct fallback when market data missing.
    """
    px, src = market_option_premium(option, as_of=as_of, client=client)
    if px is not None:
        return px, src

    mode = str(cfg.get("option_premium_mode", "market")).lower()
    if mode == "estimate":
        spot = float(option.get("spot_ref") or 0) or 23000.0
        return estimate_atm_premium(spot, cfg), "estimate"

    return None, src
