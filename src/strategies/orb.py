"""ORB Hold Continuation — 5m: wait for OR level to hold, then trade with level stop."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import pandas as pd

from src.market.calendar import IST, opening_range_end, parse_hhmm


def _ensure_tz(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series, utc=True, errors="coerce")
    return s.dt.tz_convert(IST)


def session_slice(df: pd.DataFrame, day: dt.date, cfg: dict) -> pd.DataFrame:
    if df.empty:
        return df
    open_t = parse_hhmm(cfg.get("session_open", "09:15"))
    close_t = parse_hhmm(cfg.get("session_close", "15:30"))
    start = dt.datetime.combine(day, open_t, tzinfo=IST)
    end = dt.datetime.combine(day, close_t, tzinfo=IST)
    out = df.copy()
    out["datetime"] = _ensure_tz(pd.to_datetime(out["datetime"]))
    mask = (out["datetime"] >= start) & (out["datetime"] <= end)
    return out.loc[mask].sort_values("datetime").reset_index(drop=True)


def opening_range(df: pd.DataFrame, day: dt.date, cfg: dict) -> Optional[dict[str, Any]]:
    bars = session_slice(df, day, cfg)
    if bars.empty:
        return None
    open_t = parse_hhmm(cfg.get("session_open", "09:15"))
    or_end = opening_range_end(cfg)
    start = dt.datetime.combine(day, open_t, tzinfo=IST)
    end = dt.datetime.combine(day, or_end, tzinfo=IST)
    rng = bars[(bars["datetime"] >= start) & (bars["datetime"] < end)]
    if rng.empty:
        return None
    or_high = float(rng["high"].max())
    or_low = float(rng["low"].min())
    mid = (or_high + or_low) / 2.0
    width_pct = ((or_high - or_low) / mid) * 100.0 if mid else 0.0
    session_open_px = float(rng.iloc[0]["open"])
    return {
        "or_high": or_high,
        "or_low": or_low,
        "or_mid": mid,
        "or_width_pct": round(width_pct, 4),
        "session_open_px": session_open_px,
        "bullish_or": mid >= session_open_px,
        "bearish_or": mid <= session_open_px,
        "or_start": start.isoformat(),
        "or_end": end.isoformat(),
        "bars": int(len(rng)),
        "avg_volume": float(rng["volume"].mean()) if "volume" in rng else 0.0,
    }


def _body_frac(o, h, l, c) -> float:
    return abs(c - o) / max(h - l, 1e-9)


def find_orb_trigger(
    df: pd.DataFrame,
    day: dt.date,
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    min_volume_x: float = 0.0,
) -> dict[str, Any]:
    """
    ORB Hold Continuation (5m) — planned skill flow:

    1. Build 15m OR; trade only quality width days
    2. Bias: bullish OR → long/CE; bearish OR → short/PE
    3. Require N consecutive closes beyond OR high/low (hold confirmation)
    4. Entry on Nth confirming close (chase capped)
    5. Stop = OR level (clear invalidation), target ~2.5R, trail BE after 1R
    6. Skip if a close returns inside OR before confirmation completes
    """
    rng = opening_range(df, day, cfg)
    if not rng:
        return {"status": "no-data", "why": "No opening-range bars."}

    min_w = float(cfg.get("orb_min_range_pct", 0.18))
    max_w = float(cfg.get("orb_max_range_pct", 0.70))
    if rng["or_width_pct"] < min_w or rng["or_width_pct"] > max_w:
        return {
            "status": "expired",
            "or_high": rng["or_high"],
            "or_low": rng["or_low"],
            "why": f"OR width {rng['or_width_pct']:.3f}% outside {min_w}-{max_w}% quality band.",
        }

    allow_short = bool(cfg.get("orb_allow_short_side", True))
    if rng["bullish_or"]:
        direction = "long"
    elif allow_short and rng["bearish_or"]:
        direction = "short"
    else:
        return {
            "status": "expired",
            "or_high": rng["or_high"],
            "or_low": rng["or_low"],
            "why": "No directional OR bias.",
        }

    bars = session_slice(df, day, cfg)
    or_end = dt.datetime.fromisoformat(rng["or_end"])
    after = bars[bars["datetime"] >= or_end].reset_index(drop=True)
    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=IST)
        after = after[after["datetime"] <= as_of].reset_index(drop=True)

    no_before = parse_hhmm(cfg.get("no_entries_before", "09:30"))
    no_after = parse_hhmm(cfg.get("no_entries_after", "15:00"))
    min_break = float(cfg.get("orb_min_breakout_pct", 0.03))
    max_chase = float(cfg.get("orb_max_chase_pct", 0.22))
    min_ext = float(cfg.get("orb_min_entry_extension_pct", 0.05))
    need = max(2, int(cfg.get("orb_confirm_closes", 3)))
    body_min = float(cfg.get("orb_body_min_pct", 0.30))

    if direction == "long":
        level = float(rng["or_high"])
        side = "buy"
        kind = "orb_hold_long_5m"
    else:
        level = float(rng["or_low"])
        side = "sell"
        kind = "orb_hold_short_5m"

    streak = 0
    streak_low = None
    streak_high = None

    for _, row in after.iterrows():
        bar_time = row["datetime"].to_pydatetime()
        if bar_time.time() > no_after:
            break
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        # Still build streak before window; only block the fill itself
        in_window = bar_time.time() >= no_before

        if direction == "long":
            beyond = c > level * (1 + min_break / 100.0)
            ok_body = (c >= o) or _body_frac(o, h, l, c) >= body_min
            failed = c < level
        else:
            beyond = c < level * (1 - min_break / 100.0)
            ok_body = (c <= o) or _body_frac(o, h, l, c) >= body_min
            failed = c > level

        if failed:
            streak = 0
            streak_low = None
            streak_high = None
            continue

        if beyond and ok_body:
            streak += 1
            streak_low = l if streak_low is None else min(streak_low, l)
            streak_high = h if streak_high is None else max(streak_high, h)
        elif beyond:
            # weak beyond — don't add, don't fully reset if still outside
            continue
        else:
            streak = 0
            streak_low = None
            streak_high = None
            continue

        if streak < need:
            continue

        if direction == "long":
            pct = (c - level) / level * 100.0
        else:
            pct = (level - c) / level * 100.0
        if pct > max_chase:
            # too extended — wait; keep streak but don't enter chase
            continue
        if pct < min_ext:
            continue
        if not in_window:
            continue
        # Close strength: finish near the favorable extreme of the bar
        if cfg.get("orb_require_close_strength", True):
            rng_bar = max(h - l, 1e-9)
            if direction == "long" and (c - l) / rng_bar < 0.55:
                continue
            if direction == "short" and (h - c) / rng_bar < 0.55:
                continue

        return {
            "status": "triggered",
            "symbol_day": day.isoformat(),
            "strategy": "orb_retest_pro",
            "setup": kind,
            "side": side,
            "or_high": rng["or_high"],
            "or_low": rng["or_low"],
            "or_mid": rng["or_mid"],
            "or_width_pct": rng["or_width_pct"],
            "entry": c,
            "entry_time": bar_time,
            "bar_open": o,
            "bar_high": h,
            "bar_low": l,
            "signal_low": float(level if direction == "long" else (streak_low or l)),
            "signal_high": float(level if direction == "short" else (streak_high or h)),
            "volume": 0.0,
            "pct_over": round(pct, 3),
            "why": (
                f"ORB Hold ({kind}): {need} closes beyond {level:.2f}; "
                f"entry {c:.2f} @ {bar_time.strftime('%H:%M')} ({pct:+.2f}%). "
                f"Width {rng['or_width_pct']:.2f}%."
            ),
            "strategy_steps": [
                f"5m OR H={rng['or_high']:.2f} L={rng['or_low']:.2f} width={rng['or_width_pct']:.2f}%.",
                "Directional bias from OR vs session open.",
                f"Hold filter: {need} consecutive closes beyond OR level.",
                "Skip / reset if price closes back inside OR (false break).",
                "Chase filter: skip extended entries.",
                "Stop = OR level (invalidation). Target ~2.5R. Trail BE after 1R.",
                "Risk-managed size · max 1 trade/day · entries 09:30–15:00 IST.",
            ],
        }

    return {
        "status": "waiting",
        "or_high": rng["or_high"],
        "or_low": rng["or_low"],
        "direction": direction,
        "why": f"ORB Hold Continuation dir={direction}; need {need} holds beyond level.",
    }


def scan_orb_universe(
    candle_map: dict[str, pd.DataFrame],
    day: dt.date,
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
) -> list[dict[str, Any]]:
    hits = []
    for symbol, df in candle_map.items():
        result = find_orb_trigger(df, day, cfg, as_of=as_of)
        if result.get("status") == "triggered":
            hits.append(
                {
                    "symbol": symbol,
                    "strategy": result.get("strategy", "orb_retest_pro"),
                    "setup": result.get("setup", "orb_hold_5m"),
                    **result,
                }
            )
    hits.sort(key=lambda h: h.get("pct_over", 0), reverse=True)
    return hits


def daily_setups(closes: pd.Series) -> list[str]:
    if len(closes) < 21:
        return []
    tags: list[str] = []
    last = float(closes.iloc[-1])
    prev20 = closes.iloc[-21:-1]
    if last > float(prev20.max()):
        tags.append("breakout_20d")
    return tags
