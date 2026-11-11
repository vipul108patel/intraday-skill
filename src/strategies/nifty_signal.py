"""NIFTY Intraday Signal Strategy v1.0 — EMA/VWAP/OR breakout + retest + confirm."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import pandas as pd

from src.market.calendar import IST, opening_range_end, parse_hhmm, previous_trading_days
from src.strategies.exit_plan import atr
from src.strategies.orb import opening_range, session_slice


STRATEGY_NAME = "nifty_signal_v1"


def _ensure_tz(series: pd.Series) -> pd.Series:
    """Normalize mixed naive/aware timestamps to Asia/Kolkata."""
    # utc=True avoids "Tz-aware cannot be converted to datetime64" on mixed frames
    s = pd.to_datetime(series, utc=True, errors="coerce")
    return s.dt.tz_convert(IST)


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative session VWAP; if volume is all zero, use typical-price cumulative mean."""
    if df.empty:
        return pd.Series(dtype=float)
    typ = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    vol = df["volume"].astype(float).fillna(0.0) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    if float(vol.sum()) <= 0:
        return typ.expanding().mean()
    pv = typ * vol
    return pv.cumsum() / vol.cumsum().replace(0, pd.NA).ffill()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def resample_15m(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Build 15m OHLC from 5m session bars when native 15m is unavailable."""
    if df_5m is None or df_5m.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    out = df_5m.copy()
    out["datetime"] = _ensure_tz(out["datetime"])
    out = out.set_index("datetime").sort_index()
    agg = out.resample("15min", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    agg = agg.dropna(subset=["open", "close"]).reset_index()
    return agg


def prior_day_levels(prior_df: Optional[pd.DataFrame], day: dt.date, cfg: dict) -> dict[str, Optional[float]]:
    if prior_df is None or prior_df.empty:
        return {"pdh": None, "pdl": None, "pdc": None}
    out = prior_df.copy()
    out["datetime"] = _ensure_tz(out["datetime"])
    # Slice to the prior session day if timestamps span more than one day
    prior_day = out["datetime"].dt.date.max()
    if prior_day == day:
        # caller passed same-day frame by mistake
        return {"pdh": None, "pdl": None, "pdc": None}
    sess = session_slice(out, prior_day, cfg)
    use = sess if not sess.empty else out
    if use.empty:
        return {"pdh": None, "pdl": None, "pdc": None}
    return {
        "pdh": float(use["high"].max()),
        "pdl": float(use["low"].min()),
        "pdc": float(use["close"].iloc[-1]),
    }


def _ema_bias(bars_15m: pd.DataFrame, as_of: Optional[dt.datetime], period: int, flat_pct: float) -> dict[str, Any]:
    if bars_15m is None or bars_15m.empty or len(bars_15m) < max(5, period // 2):
        return {"status": "no_trade", "why": "Insufficient 15m bars for EMA20.", "bias": None}
    df = bars_15m.copy()
    df["datetime"] = _ensure_tz(df["datetime"])
    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=IST)
        df = df[df["datetime"] <= as_of]
    if len(df) < 3:
        return {"status": "no_trade", "why": "Insufficient 15m bars as-of now.", "bias": None}
    closes = df["close"].astype(float)
    e = ema(closes, period)
    last_c = float(closes.iloc[-1])
    last_e = float(e.iloc[-1])
    prev_e = float(e.iloc[-2]) if len(e) >= 2 else last_e
    slope_pct = abs(last_e - prev_e) / max(abs(last_e), 1e-9) * 100.0
    if slope_pct < flat_pct:
        return {
            "status": "no_trade",
            "why": f"EMA{period} flat (slope {slope_pct:.4f}% < {flat_pct}%).",
            "bias": None,
            "ema": last_e,
            "close": last_c,
        }
    if last_c > last_e and last_e >= prev_e:
        bias = "bullish"
    elif last_c < last_e and last_e <= prev_e:
        bias = "bearish"
    else:
        return {
            "status": "no_trade",
            "why": f"EMA{period} bias unclear (close={last_c:.2f}, ema={last_e:.2f}).",
            "bias": None,
            "ema": last_e,
            "close": last_c,
        }
    return {"status": "ok", "bias": bias, "ema": last_e, "close": last_c, "slope_pct": slope_pct}


def _vwap_chop(bars: pd.DataFrame, vwap: pd.Series, max_crosses: int) -> bool:
    if bars.empty or vwap.empty:
        return False
    side = (bars["close"].astype(float) > vwap.astype(float)).astype(int)
    crosses = int((side.diff().fillna(0).abs() > 0).sum())
    return crosses > max_crosses


def _vol_ok(row_vol: float, lookback: pd.Series, require_when_available: bool) -> tuple[bool, float]:
    avg = float(lookback.mean()) if len(lookback) else 0.0
    if avg <= 0:
        return True, 0.0  # index volume often zero — skip gate
    if not require_when_available:
        return True, row_vol / avg if avg else 0.0
    return row_vol > avg, (row_vol / avg) if avg else 0.0


def _next_structure_target(
    side: str,
    entry: float,
    stop: float,
    pdh: Optional[float],
    pdl: Optional[float],
    or_high: float,
    or_low: float,
    atr_val: Optional[float],
    min_rr: float,
) -> tuple[float, float, str]:
    risk = abs(entry - stop)
    if risk <= 0:
        return entry, 0.0, "invalid risk"
    min_target_dist = min_rr * risk
    candidates: list[tuple[float, str]] = []
    if side == "buy":
        if pdh is not None and pdh > entry:
            candidates.append((pdh, "PDH"))
        # session extension / OR mid stretch
        candidates.append((or_high + max(min_target_dist, (or_high - or_low)), "OR extension"))
        if atr_val:
            candidates.append((entry + 2.0 * atr_val, "2×ATR"))
        candidates.append((entry + min_target_dist, f"{min_rr}R floor"))
        # pick nearest reward >= min_rr
        valid = [(t, n) for t, n in candidates if (t - entry) >= min_target_dist - 1e-9]
        if not valid:
            t = entry + min_target_dist
            return t, min_rr, f"{min_rr}R floor"
        t, name = min(valid, key=lambda x: x[0])
        rr = (t - entry) / risk
        return t, rr, name
    else:
        if pdl is not None and pdl < entry:
            candidates.append((pdl, "PDL"))
        candidates.append((or_low - max(min_target_dist, (or_high - or_low)), "OR extension"))
        if atr_val:
            candidates.append((entry - 2.0 * atr_val, "2×ATR"))
        candidates.append((entry - min_target_dist, f"{min_rr}R floor"))
        valid = [(t, n) for t, n in candidates if (entry - t) >= min_target_dist - 1e-9]
        if not valid:
            t = entry - min_target_dist
            return t, min_rr, f"{min_rr}R floor"
        t, name = max(valid, key=lambda x: x[0])  # nearest below
        rr = (entry - t) / risk
        return t, rr, name


def find_nifty_signal_trigger(
    df: pd.DataFrame,
    day: dt.date,
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    bars_15m: Optional[pd.DataFrame] = None,
    prior_df: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    NIFTY Signal v1.0:
      prep OR + PDH/PDL/PDC → 15m EMA20 + VWAP bias → OR breakout (vol) →
      retest → confirmation → entry on next bar open.
    """
    rng = opening_range(df, day, cfg)
    if not rng:
        return {"status": "no-data", "why": "No opening-range bars.", "strategy": STRATEGY_NAME}

    levels = prior_day_levels(prior_df, day, cfg)
    pdh, pdl, pdc = levels["pdh"], levels["pdl"], levels["pdc"]

    bars_15 = bars_15m if bars_15m is not None and not bars_15m.empty else resample_15m(df)
    # Warm EMA with prior day 15m if provided
    if prior_df is not None and not prior_df.empty:
        prior_15 = resample_15m(prior_df)
        if not prior_15.empty:
            bars_15 = (
                pd.concat([prior_15, bars_15], ignore_index=True)
                .drop_duplicates(subset=["datetime"])
                .sort_values("datetime")
                .reset_index(drop=True)
            )

    ema_period = int(cfg.get("ema_period", 20))
    flat_pct = float(cfg.get("ema_flat_slope_pct", 0.015))
    bias_info = _ema_bias(bars_15, as_of, ema_period, flat_pct)
    if bias_info.get("status") != "ok":
        return {
            "status": "no_trade",
            "strategy": STRATEGY_NAME,
            "or_high": rng["or_high"],
            "or_low": rng["or_low"],
            "pdh": pdh,
            "pdl": pdl,
            "pdc": pdc,
            "why": bias_info.get("why", "EMA bias rejected."),
        }

    bias = bias_info["bias"]
    bars = session_slice(df, day, cfg)
    if bars.empty:
        return {"status": "no-data", "why": "No session bars.", "strategy": STRATEGY_NAME}

    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=IST)
        bars = bars[bars["datetime"] <= as_of].reset_index(drop=True)

    vwap = session_vwap(bars)
    max_crosses = int(cfg.get("vwap_chop_max_crosses", 8))
    # Only evaluate chop after OR ends
    or_end = dt.datetime.fromisoformat(rng["or_end"])
    after_mask = bars["datetime"] >= or_end
    if after_mask.any() and _vwap_chop(bars.loc[after_mask], vwap.loc[after_mask], max_crosses):
        return {
            "status": "no_trade",
            "strategy": STRATEGY_NAME,
            "or_high": rng["or_high"],
            "or_low": rng["or_low"],
            "why": f"VWAP chop: more than {max_crosses} crosses after OR.",
        }

    no_before = parse_hhmm(cfg.get("no_entries_before", "09:30"))
    no_after = parse_hhmm(cfg.get("no_entries_after", "15:00"))
    vol_ma = int(cfg.get("volume_ma", 20))
    require_vol = bool(cfg.get("require_volume_when_available", True))
    retest_bars = int(cfg.get("signal_retest_bars", 12))
    retest_tol_pct = float(cfg.get("signal_retest_tol_pct", 0.05))
    min_rr = float(cfg.get("min_reward_risk", 2.0))
    atr_n = int(cfg.get("atr_period", 14))
    entry_mode = str(cfg.get("signal_entry_mode", "retest")).lower()
    hold_closes = max(1, int(cfg.get("orb_confirm_closes", 2)))
    min_break = float(cfg.get("orb_min_breakout_pct", 0.02))

    or_high = float(rng["or_high"])
    or_low = float(rng["or_low"])
    direction = "long" if bias == "bullish" else "short"
    level = or_high if direction == "long" else or_low
    side = "buy" if direction == "long" else "sell"
    ema_period = int(cfg.get("ema_period", 20))

    ctx = {
        "day": day,
        "rng": rng,
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
        "bias": bias,
        "bias_info": bias_info,
        "ema_period": ema_period,
        "or_high": or_high,
        "or_low": or_low,
        "level": level,
        "side": side,
        "direction": direction,
        "min_rr": min_rr,
        "atr_n": atr_n,
        "no_before": no_before,
        "no_after": no_after,
        "bars": bars,
        "vwap": vwap,
        "or_end": or_end,
        "vol_ma": vol_ma,
        "require_vol": require_vol,
    }

    # Higher-frequency modes: skip full retest chain
    if entry_mode in ("break_hold", "hold"):
        return _trigger_break_hold(ctx, cfg, hold_closes=hold_closes, min_break=min_break)
    if entry_mode in ("break_confirm", "break"):
        return _trigger_break_confirm(ctx, cfg, min_break=min_break)

    kind = "nifty_ce_break_retest" if direction == "long" else "nifty_pe_break_retest"

    # State machine — full retest path (strict / lower frequency)
    break_idx: Optional[int] = None
    retest_idx: Optional[int] = None
    retest_swing_low: Optional[float] = None
    retest_swing_high: Optional[float] = None
    confirm_idx: Optional[int] = None
    break_vol_x = 0.0

    for i, row in bars.iterrows():
        bar_time = row["datetime"].to_pydatetime()
        if bar_time < or_end:
            continue
        if bar_time.time() > no_after:
            break

        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        v = float(row["volume"]) if "volume" in bars.columns else 0.0
        vw = float(vwap.iloc[i]) if i < len(vwap) and pd.notna(vwap.iloc[i]) else c

        if direction == "long" and c < vw:
            if break_idx is not None and retest_idx is None and c < level:
                break_idx = None
            continue
        if direction == "short" and c > vw:
            if break_idx is not None and retest_idx is None and c > level:
                break_idx = None
            continue

        if break_idx is None:
            broke = c > level if direction == "long" else c < level
            if not broke:
                continue
            lookback = (
                bars["volume"].astype(float).iloc[max(0, i - vol_ma) : i]
                if "volume" in bars.columns
                else pd.Series(dtype=float)
            )
            ok_vol, vx = _vol_ok(v, lookback, require_vol)
            if not ok_vol:
                continue
            break_idx = int(i)
            break_vol_x = vx
            retest_swing_low = l
            retest_swing_high = h
            continue

        if retest_idx is None:
            if direction == "long" and c < or_low:
                return {
                    "status": "no_trade",
                    "strategy": STRATEGY_NAME,
                    "or_high": or_high,
                    "or_low": or_low,
                    "why": "Breakout failed — price closed back through OR into range.",
                }
            if direction == "short" and c > or_high:
                return {
                    "status": "no_trade",
                    "strategy": STRATEGY_NAME,
                    "or_high": or_high,
                    "or_low": or_low,
                    "why": "Breakdown failed — price closed back through OR into range.",
                }
            if int(i) - int(break_idx) > retest_bars:
                return {
                    "status": "expired",
                    "strategy": STRATEGY_NAME,
                    "or_high": or_high,
                    "or_low": or_low,
                    "why": f"No retest within {retest_bars} bars after breakout.",
                }
            tol = level * retest_tol_pct / 100.0
            if direction == "long":
                touched = l <= level + tol
                held = c >= level - tol
            else:
                touched = h >= level - tol
                held = c <= level + tol
            retest_swing_low = min(retest_swing_low or l, l)
            retest_swing_high = max(retest_swing_high or h, h)
            if touched and held:
                retest_idx = int(i)
            continue

        if confirm_idx is None:
            if int(i) <= int(retest_idx):
                continue
            if direction == "long":
                if c < level:
                    return {
                        "status": "no_trade",
                        "strategy": STRATEGY_NAME,
                        "or_high": or_high,
                        "or_low": or_low,
                        "why": "Retest failed — close back below breakout level.",
                    }
                if c > o and c >= level:
                    confirm_idx = int(i)
            else:
                if c > level:
                    return {
                        "status": "no_trade",
                        "strategy": STRATEGY_NAME,
                        "or_high": or_high,
                        "or_low": or_low,
                        "why": "Retest failed — close back above breakdown level.",
                    }
                if c < o and c <= level:
                    confirm_idx = int(i)
            if confirm_idx is None:
                continue

        if confirm_idx is not None:
            hit = _finalize_entry(
                ctx,
                cfg,
                kind=kind,
                break_idx=int(break_idx),
                signal_idx=int(confirm_idx),
                swing_low=float(retest_swing_low or bars.iloc[retest_idx]["low"]),
                swing_high=float(retest_swing_high or bars.iloc[retest_idx]["high"]),
                break_vol_x=break_vol_x,
                mode_label="retest",
                retest_idx=int(retest_idx),
                confirm_idx=int(confirm_idx),
            )
            if hit is not None:
                return hit

    if break_idx is None:
        return {
            "status": "waiting",
            "strategy": STRATEGY_NAME,
            "or_high": or_high,
            "or_low": or_low,
            "direction": direction,
            "why": f"Waiting for OR {'breakout' if direction == 'long' else 'breakdown'} with EMA/VWAP bias={bias}.",
        }
    if retest_idx is None:
        return {
            "status": "waiting",
            "strategy": STRATEGY_NAME,
            "or_high": or_high,
            "or_low": or_low,
            "direction": direction,
            "why": "Breakout seen — waiting for retest of level.",
        }
    return {
        "status": "waiting",
        "strategy": STRATEGY_NAME,
        "or_high": or_high,
        "or_low": or_low,
        "direction": direction,
        "why": "Retest seen — waiting for confirmation candle.",
    }


def _finalize_entry(
    ctx: dict,
    cfg: dict,
    *,
    kind: str,
    break_idx: int,
    signal_idx: int,
    swing_low: float,
    swing_high: float,
    break_vol_x: float,
    mode_label: str,
    retest_idx: Optional[int] = None,
    confirm_idx: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    bars = ctx["bars"]
    direction = ctx["direction"]
    side = ctx["side"]
    level = ctx["level"]
    or_high = ctx["or_high"]
    or_low = ctx["or_low"]
    min_rr = ctx["min_rr"]
    no_before = ctx["no_before"]
    no_after = ctx["no_after"]
    entry_i = signal_idx + 1
    if entry_i >= len(bars):
        return {
            "status": "waiting",
            "strategy": STRATEGY_NAME,
            "or_high": or_high,
            "or_low": or_low,
            "direction": direction,
            "why": "Signal ready — awaiting next 5m bar for entry.",
        }
    entry_row = bars.iloc[entry_i]
    entry_time = entry_row["datetime"].to_pydatetime()
    if entry_time.time() < no_before or entry_time.time() > no_after:
        return {
            "status": "expired",
            "strategy": STRATEGY_NAME,
            "or_high": or_high,
            "or_low": or_low,
            "why": "Entry bar outside entry window.",
        }
    entry = float(entry_row["open"])
    if direction == "long":
        stop = float(swing_low) - max(0.5, swing_low * 0.0001)
        if stop >= entry:
            stop = entry - max(5.0, (or_high - or_low) * 0.25)
    else:
        stop = float(swing_high) + max(0.5, swing_high * 0.0001)
        if stop <= entry:
            stop = entry + max(5.0, (or_high - or_low) * 0.25)

    atr_val = atr(bars.iloc[: entry_i + 1], ctx["atr_n"])
    target, rr, tgt_name = _next_structure_target(
        side, entry, stop, ctx["pdh"], ctx["pdl"], or_high, or_low, atr_val, min_rr
    )
    if rr + 1e-9 < min_rr:
        return {
            "status": "no_trade",
            "strategy": STRATEGY_NAME,
            "or_high": or_high,
            "or_low": or_low,
            "why": f"Insufficient RR to next S/R ({rr:.2f} < {min_rr}).",
        }

    pct = abs(entry - level) / level * 100.0
    why_extra = ""
    if mode_label == "retest" and retest_idx is not None and confirm_idx is not None:
        why_extra = (
            f"break@{bars.iloc[break_idx]['datetime'].strftime('%H:%M')} "
            f"retest@{bars.iloc[retest_idx]['datetime'].strftime('%H:%M')} "
            f"confirm@{bars.iloc[confirm_idx]['datetime'].strftime('%H:%M')} "
        )
    elif mode_label == "break_hold":
        why_extra = f"hold@{bars.iloc[signal_idx]['datetime'].strftime('%H:%M')} "
    else:
        why_extra = f"confirm@{bars.iloc[signal_idx]['datetime'].strftime('%H:%M')} "

    return {
        "status": "triggered",
        "symbol_day": ctx["day"].isoformat(),
        "strategy": STRATEGY_NAME,
        "setup": kind,
        "side": side,
        "or_high": or_high,
        "or_low": or_low,
        "or_mid": ctx["rng"]["or_mid"],
        "or_width_pct": ctx["rng"]["or_width_pct"],
        "pdh": ctx["pdh"],
        "pdl": ctx["pdl"],
        "pdc": ctx["pdc"],
        "entry": entry,
        "entry_time": entry_time,
        "planned_target": target,
        "target_name": tgt_name,
        "bar_open": float(entry_row["open"]),
        "bar_high": float(entry_row["high"]),
        "bar_low": float(entry_row["low"]),
        "signal_low": float(swing_low),
        "signal_high": float(swing_high),
        "volume": float(bars.iloc[break_idx]["volume"]) if "volume" in bars.columns else 0.0,
        "volume_x": round(break_vol_x, 3),
        "pct_over": round(pct, 3),
        "reward_risk": round(rr, 3),
        "ema": ctx["bias_info"].get("ema"),
        "bias": ctx["bias"],
        "entry_mode": mode_label,
        "why": (
            f"NIFTY Signal v1 {kind} [{mode_label}]: {ctx['bias']} EMA{ctx['ema_period']}+VWAP; "
            f"{why_extra}"
            f"→ entry {entry:.2f} @ {entry_time.strftime('%H:%M')}; "
            f"stop {stop:.2f}; target {target:.2f} ({tgt_name}, {rr:.2f}R)."
        ),
        "strategy_steps": [
            f"Prep: ORH={or_high:.2f} ORL={or_low:.2f}; PDH={ctx['pdh']} PDL={ctx['pdl']}.",
            f"15m EMA{ctx['ema_period']} {ctx['bias']}; VWAP aligned.",
            f"Entry mode={mode_label}.",
            f"SL = signal swing; target ≥ {min_rr}R via {tgt_name}.",
        ],
        "computed_stop": stop,
        "computed_target": target,
    }


def _trigger_break_hold(ctx: dict, cfg: dict, hold_closes: int, min_break: float) -> dict[str, Any]:
    """OR break + N consecutive closes beyond level (higher frequency)."""
    bars = ctx["bars"]
    direction = ctx["direction"]
    level = ctx["level"]
    or_high = ctx["or_high"]
    or_low = ctx["or_low"]
    or_end = ctx["or_end"]
    vwap = ctx["vwap"]
    no_after = ctx["no_after"]
    kind = "nifty_ce_break_hold" if direction == "long" else "nifty_pe_break_hold"
    streak = 0
    streak_low = None
    streak_high = None
    break_idx = None
    break_vol_x = 0.0

    for i, row in bars.iterrows():
        bar_time = row["datetime"].to_pydatetime()
        if bar_time < or_end or bar_time.time() > no_after:
            if bar_time.time() > no_after:
                break
            continue
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        v = float(row["volume"]) if "volume" in bars.columns else 0.0
        vw = float(vwap.iloc[i]) if i < len(vwap) and pd.notna(vwap.iloc[i]) else c
        if direction == "long" and c < vw:
            streak = 0
            continue
        if direction == "short" and c > vw:
            streak = 0
            continue

        if direction == "long":
            beyond = c > level * (1 + min_break / 100.0)
            failed = c < level
        else:
            beyond = c < level * (1 - min_break / 100.0)
            failed = c > level

        if failed:
            streak = 0
            streak_low = None
            streak_high = None
            continue
        if not beyond:
            streak = 0
            continue

        lookback = (
            bars["volume"].astype(float).iloc[max(0, i - ctx["vol_ma"]) : i]
            if "volume" in bars.columns
            else pd.Series(dtype=float)
        )
        ok_vol, vx = _vol_ok(v, lookback, ctx["require_vol"])
        if not ok_vol and streak == 0:
            continue

        streak += 1
        streak_low = l if streak_low is None else min(streak_low, l)
        streak_high = h if streak_high is None else max(streak_high, h)
        if break_idx is None:
            break_idx = int(i)
            break_vol_x = vx
        if streak < hold_closes:
            continue

        hit = _finalize_entry(
            ctx,
            cfg,
            kind=kind,
            break_idx=int(break_idx),
            signal_idx=int(i),
            swing_low=float(streak_low if direction == "long" else level),
            swing_high=float(streak_high if direction == "short" else level),
            break_vol_x=break_vol_x,
            mode_label="break_hold",
        )
        if hit is not None:
            return hit

    return {
        "status": "waiting",
        "strategy": STRATEGY_NAME,
        "or_high": or_high,
        "or_low": or_low,
        "direction": direction,
        "why": f"break_hold: need {hold_closes} closes beyond OR (bias={ctx['bias']}).",
    }


def _trigger_break_confirm(ctx: dict, cfg: dict, min_break: float) -> dict[str, Any]:
    """OR break then one confirmation candle — no retest (medium frequency)."""
    bars = ctx["bars"]
    direction = ctx["direction"]
    level = ctx["level"]
    or_high = ctx["or_high"]
    or_low = ctx["or_low"]
    or_end = ctx["or_end"]
    vwap = ctx["vwap"]
    no_after = ctx["no_after"]
    kind = "nifty_ce_break_confirm" if direction == "long" else "nifty_pe_break_confirm"
    break_idx = None
    swing_low = None
    swing_high = None
    break_vol_x = 0.0

    for i, row in bars.iterrows():
        bar_time = row["datetime"].to_pydatetime()
        if bar_time < or_end or bar_time.time() > no_after:
            if bar_time.time() > no_after:
                break
            continue
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        v = float(row["volume"]) if "volume" in bars.columns else 0.0
        vw = float(vwap.iloc[i]) if i < len(vwap) and pd.notna(vwap.iloc[i]) else c
        if direction == "long" and c < vw:
            continue
        if direction == "short" and c > vw:
            continue

        if break_idx is None:
            beyond = c > level * (1 + min_break / 100.0) if direction == "long" else c < level * (1 - min_break / 100.0)
            if not beyond:
                continue
            lookback = (
                bars["volume"].astype(float).iloc[max(0, i - ctx["vol_ma"]) : i]
                if "volume" in bars.columns
                else pd.Series(dtype=float)
            )
            ok_vol, vx = _vol_ok(v, lookback, ctx["require_vol"])
            if not ok_vol:
                continue
            break_idx = int(i)
            swing_low, swing_high, break_vol_x = l, h, vx
            continue

        # failed break
        if direction == "long" and c < or_low:
            return {"status": "no_trade", "strategy": STRATEGY_NAME, "or_high": or_high, "or_low": or_low,
                    "why": "Breakout failed after break_confirm start."}
        if direction == "short" and c > or_high:
            return {"status": "no_trade", "strategy": STRATEGY_NAME, "or_high": or_high, "or_low": or_low,
                    "why": "Breakdown failed after break_confirm start."}

        swing_low = min(swing_low or l, l)
        swing_high = max(swing_high or h, h)
        if int(i) <= int(break_idx):
            continue
        ok = (c > o and c >= level) if direction == "long" else (c < o and c <= level)
        if not ok:
            continue
        hit = _finalize_entry(
            ctx,
            cfg,
            kind=kind,
            break_idx=int(break_idx),
            signal_idx=int(i),
            swing_low=float(swing_low if direction == "long" else level),
            swing_high=float(swing_high if direction == "short" else level),
            break_vol_x=break_vol_x,
            mode_label="break_confirm",
        )
        if hit is not None:
            return hit

    return {
        "status": "waiting",
        "strategy": STRATEGY_NAME,
        "or_high": or_high,
        "or_low": or_low,
        "direction": direction,
        "why": f"break_confirm: waiting OR break + confirm (bias={ctx['bias']}).",
    }


def scan_nifty_signal_universe(
    candle_map: dict[str, pd.DataFrame],
    day: dt.date,
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    bars_15m_map: Optional[dict[str, pd.DataFrame]] = None,
    prior_map: Optional[dict[str, pd.DataFrame]] = None,
) -> list[dict[str, Any]]:
    hits = []
    bars_15m_map = bars_15m_map or {}
    prior_map = prior_map or {}
    for symbol, df in candle_map.items():
        result = find_nifty_signal_trigger(
            df,
            day,
            cfg,
            as_of=as_of,
            bars_15m=bars_15m_map.get(symbol),
            prior_df=prior_map.get(symbol),
        )
        if result.get("status") == "triggered":
            hits.append(
                {
                    "symbol": symbol,
                    "strategy": result.get("strategy", STRATEGY_NAME),
                    "setup": result.get("setup", "nifty_signal"),
                    **result,
                }
            )
    hits.sort(key=lambda h: h.get("reward_risk", h.get("pct_over", 0)), reverse=True)
    return hits


def previous_session_date(day: dt.date) -> Optional[dt.date]:
    days = previous_trading_days(day, 1)
    return days[0] if days else None
