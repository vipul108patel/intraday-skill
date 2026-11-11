"""Load 5m + 15m + prior-session context for NIFTY / stocks strategies."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Optional

import pandas as pd

from src.market.calendar import IST, now_ist, previous_trading_days
from src.strategies.nifty_signal import previous_session_date, resample_15m

logger = logging.getLogger(__name__)


def _day_mask(df: pd.DataFrame, day: dt.date) -> pd.Series:
    return pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("Asia/Kolkata").dt.date == day


def load_symbol_context(
    client: Any,
    symbol: str,
    day: dt.date,
    cfg: dict,
) -> dict[str, Optional[pd.DataFrame]]:
    """
    Fetch today's + prior 5m sessions; build 15m by resampling.

    Fast path (scan_combine_range / stocks desk):
      - Past warmup days from disk cache when possible
      - Else ONE range API call covering warmup→today, then cache past days
      - Today always refreshed (no stale full-day cache)
    """
    interval = cfg.get("bar_interval", "FIVE_MINUTE")
    warmup_n = int(cfg.get("scan_warmup_days", 2) or 2)
    warmup_n = max(0, min(warmup_n, 5))
    warmup = list(previous_trading_days(day, warmup_n)) if warmup_n else []
    combine = bool(
        cfg.get(
            "scan_combine_range",
            str(cfg.get("desk", "")).lower() == "stocks",
        )
    )

    frames_5: list[pd.DataFrame] = []

    if combine:
        cached_priors: list[pd.DataFrame] = []
        missing = False
        for d in warmup:
            try:
                # Only hits API if that past day is not cached yet
                part = client.get_session_candles(symbol, d, interval=interval, use_cache=True)
                if part is not None and not part.empty:
                    cached_priors.append(part)
                else:
                    missing = True
                    break
            except Exception:
                missing = True
                break

        now = now_ist()
        today_end = min(
            now,
            dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST),
        )
        if missing and warmup:
            # One Angel call for warmup start → now (cuts ~2x API volume)
            start = dt.datetime.combine(warmup[0], dt.time(9, 15), tzinfo=IST)
            try:
                all_df = client.get_candles(
                    symbol, start, today_end, interval=interval, use_cache=False
                )
            except Exception as e:
                logger.debug("range fetch %s: %s", symbol, e)
                all_df = pd.DataFrame()
            if all_df is not None and not all_df.empty:
                frames_5.append(all_df)
                for d in warmup:
                    slice_d = all_df.loc[_day_mask(all_df, d)].reset_index(drop=True)
                    if not slice_d.empty and hasattr(client, "write_session_cache"):
                        client.write_session_cache(symbol, d, slice_d, interval=interval)
        else:
            frames_5.extend(cached_priors)
            try:
                today_df = client.get_session_candles(
                    symbol, day, interval=interval, use_cache=False
                )
                if today_df is not None and not today_df.empty:
                    # Clip to "now" if client returned full session template
                    frames_5.append(today_df)
            except Exception as e:
                logger.debug("today fetch %s: %s", symbol, e)
    else:
        for d in list(warmup) + [day]:
            try:
                use_cache = d < day
                part = client.get_session_candles(
                    symbol, d, interval=interval, use_cache=use_cache
                )
                if part is not None and not part.empty:
                    frames_5.append(part)
            except Exception as e:
                logger.debug("5m %s %s: %s", symbol, d, e)

    if not frames_5:
        return {"df_5m": pd.DataFrame(), "df_15m": pd.DataFrame(), "prior_df": None}

    all_5m = (
        pd.concat(frames_5, ignore_index=True)
        .drop_duplicates(subset=["datetime"])
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    df_5m = all_5m.loc[_day_mask(all_5m, day)].reset_index(drop=True)
    if df_5m.empty:
        try:
            df_5m = client.get_session_candles(symbol, day, interval=interval, use_cache=False)
        except Exception:
            df_5m = pd.DataFrame()

    df_15m = resample_15m(all_5m)

    prior_day = previous_session_date(day)
    prior_df = None
    if prior_day is not None:
        prior_df = all_5m.loc[_day_mask(all_5m, prior_day)].reset_index(drop=True)
        # Do NOT issue a second Angel call here — that was doubling rate-limit pressure.

    return {"df_5m": df_5m, "df_15m": df_15m, "prior_df": prior_df}


def load_universe_context(
    client: Any,
    symbols: list[str],
    day: dt.date,
    cfg: dict,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    candle_map: dict[str, pd.DataFrame] = {}
    bars_15m_map: dict[str, pd.DataFrame] = {}
    prior_map: dict[str, pd.DataFrame] = {}
    pause = max(0, int(cfg.get("scan_symbol_pause_ms", 0) or 0)) / 1000.0
    ok = 0
    for i, symbol in enumerate(symbols):
        if i and pause:
            time.sleep(pause)
        try:
            ctx = load_symbol_context(client, symbol, day, cfg)
        except Exception as e:
            logger.warning("Context %s: %s", symbol, e)
            continue
        if ctx["df_5m"] is None or ctx["df_5m"].empty:
            continue
        candle_map[symbol] = ctx["df_5m"]
        ok += 1
        if ctx["df_15m"] is not None and not ctx["df_15m"].empty:
            bars_15m_map[symbol] = ctx["df_15m"]
        if ctx["prior_df"] is not None and not ctx["prior_df"].empty:
            prior_map[symbol] = ctx["prior_df"]
    if symbols:
        logger.info(
            "Universe context %s: %s/%s symbols with bars",
            cfg.get("desk", "?"),
            ok,
            len(symbols),
        )
    return candle_map, bars_15m_map, prior_map
