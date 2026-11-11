"""
NIFTY BOS Strategy v1 — Market Structure + VWAP + EMA20/50 + RSI/ADX/Supertrend/ATR.

Priority (algo):
  1. Market Structure (BOS/CHOCH)
  2. VWAP
  3. EMA 20/50 (15m)
  4. Volume (futures proxy when available)
  5. ATR
  6. OI/PCR/VIX soft context
  7. RSI / ADX / Supertrend confirmation

CE: 15m EMA20>EMA50, price>VWAP, 5m bullish BOS, RSI>55, ADX>20, volume, soft put-side OK
PE: reverse with RSI<45
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import pandas as pd

from src.market.calendar import IST, parse_hhmm
from src.strategies.indicators import (
    adx,
    atr,
    daily_cpr,
    detect_bos_choch,
    ema,
    rsi,
    supertrend,
)
from src.strategies.nifty_signal import session_vwap, prior_day_levels, resample_15m
from src.strategies.option_flow import option_flow_context
from src.strategies.orb import opening_range, session_slice

STRATEGY_NAME = "nifty_bos_v1"


def _ensure_tz(series: pd.Series) -> pd.Series:
    s = pd.to_datetime(series, utc=True, errors="coerce")
    return s.dt.tz_convert(IST)


def _enrich_5m(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = df.copy()
    out["datetime"] = _ensure_tz(out["datetime"])
    out = out.sort_values("datetime").reset_index(drop=True)
    out["ema20"] = ema(out["close"], int(cfg.get("bos_ema_fast", 20)))
    out["ema50"] = ema(out["close"], int(cfg.get("bos_ema_slow", 50)))
    out["rsi"] = rsi(out["close"], int(cfg.get("bos_rsi_period", 14)))
    out["atr"] = atr(out, int(cfg.get("bos_atr_period", 14)))
    adx_df = adx(out, int(cfg.get("bos_adx_period", 14)))
    out["adx"] = adx_df["adx"]
    out["plus_di"] = adx_df["+di"]
    out["minus_di"] = adx_df["-di"]
    st = supertrend(
        out,
        period=int(cfg.get("bos_supertrend_period", 10)),
        multiplier=float(cfg.get("bos_supertrend_mult", 3.0)),
    )
    out["supertrend"] = st["supertrend"]
    out["st_dir"] = st["st_dir"]
    out["vwap"] = session_vwap(out)
    struct = detect_bos_choch(
        out,
        left=int(cfg.get("bos_swing_left", 2)),
        right=int(cfg.get("bos_swing_right", 2)),
    )
    for col in struct.columns:
        out[col] = struct[col]
    if "volume" not in out.columns:
        out["volume"] = 0.0
    vol_ma = int(cfg.get("volume_ma", 20))
    out["vol_ma"] = out["volume"].astype(float).rolling(vol_ma, min_periods=1).mean()
    return out


def _trend_15m_bullish(bars_15m: pd.DataFrame, cfg: dict, as_of: Optional[dt.datetime]) -> dict[str, Any]:
    if bars_15m is None or bars_15m.empty:
        return {"ok": False, "bias": None, "why": "No 15m bars for EMA trend."}
    df = bars_15m.copy()
    df["datetime"] = _ensure_tz(df["datetime"])
    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=IST)
        df = df[df["datetime"] <= as_of]
    if len(df) < 5:
        return {"ok": False, "bias": None, "why": "Insufficient 15m history."}
    fast = int(cfg.get("bos_ema_fast", 20))
    slow = int(cfg.get("bos_ema_slow", 50))
    e20 = ema(df["close"], fast)
    e50 = ema(df["close"], slow)
    last_e20 = float(e20.iloc[-1])
    last_e50 = float(e50.iloc[-1])
    px = float(df["close"].iloc[-1])
    if last_e20 > last_e50 and px >= last_e50:
        return {"ok": True, "bias": "bullish", "ema20": last_e20, "ema50": last_e50, "close": px}
    if last_e20 < last_e50 and px <= last_e50:
        return {"ok": True, "bias": "bearish", "ema20": last_e20, "ema50": last_e50, "close": px}
    return {
        "ok": False,
        "bias": None,
        "why": f"15m EMA not aligned (EMA{fast}={last_e20:.1f}, EMA{slow}={last_e50:.1f}, px={px:.1f}).",
        "ema20": last_e20,
        "ema50": last_e50,
        "close": px,
    }


def _vol_ok(row_vol: float, vol_ma: float, require: bool) -> bool:
    if vol_ma <= 0:
        return True  # index volume often 0 — skip
    if not require:
        return True
    return row_vol > vol_ma


def find_nifty_bos_trigger(
    df: pd.DataFrame,
    day: dt.date,
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    bars_15m: Optional[pd.DataFrame] = None,
    prior_df: Optional[pd.DataFrame] = None,
    volume_df: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """
    Scan today's 5m bars for CE/PE BOS setup after 15m trend + VWAP filters.
    volume_df: optional NIFTY futures session bars for volume confirmation.
    """
    bars = session_slice(df, day, cfg)
    if bars.empty:
        return {"status": "no-data", "why": "No session bars.", "strategy": STRATEGY_NAME}

    if as_of is not None:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=IST)
        bars = bars[bars["datetime"] <= as_of].reset_index(drop=True)

    # Merge futures volume if provided (index has no real volume)
    if volume_df is not None and not volume_df.empty and "volume" in volume_df.columns:
        vdf = volume_df.copy()
        vdf["datetime"] = _ensure_tz(vdf["datetime"])
        vmap = dict(zip(vdf["datetime"], vdf["volume"].astype(float)))
        bars = bars.copy()
        bars["volume"] = bars["datetime"].map(lambda t: vmap.get(t, 0.0))

    levels = prior_day_levels(prior_df, day, cfg)
    pdh, pdl, pdc = levels["pdh"], levels["pdl"], levels["pdc"]
    cpr = None
    if pdh is not None and pdl is not None and pdc is not None:
        cpr = daily_cpr(pdh, pdl, pdc)

    bars_15 = bars_15m if bars_15m is not None and not bars_15m.empty else resample_15m(
        pd.concat([prior_df, df], ignore_index=True) if prior_df is not None else df
    )
    trend = _trend_15m_bullish(bars_15, cfg, as_of)
    if not trend.get("ok"):
        return {
            "status": "no_trade",
            "strategy": STRATEGY_NAME,
            "why": trend.get("why", "15m trend filter failed."),
            "pdh": pdh,
            "pdl": pdl,
            "pdc": pdc,
        }

    bias = trend["bias"]
    enriched = _enrich_5m(bars, cfg)
    rng = opening_range(df, day, cfg)
    or_high = float(rng["or_high"]) if rng else None
    or_low = float(rng["or_low"]) if rng else None

    no_before = parse_hhmm(cfg.get("no_entries_before", "09:30"))
    no_after = parse_hhmm(cfg.get("no_entries_after", "15:00"))
    rsi_bull = float(cfg.get("bos_rsi_bull", 55))
    rsi_bear = float(cfg.get("bos_rsi_bear", 45))
    adx_min = float(cfg.get("bos_adx_min", 20))
    atr_min_pct = float(cfg.get("bos_atr_min_pct", 0.02))  # ATR vs price
    require_vol = bool(cfg.get("require_volume_when_available", True))
    require_st = bool(cfg.get("bos_require_supertrend", True))
    use_choch = bool(cfg.get("bos_allow_choch", True))
    min_rr = float(cfg.get("min_reward_risk", 1.5))

    for i in range(len(enriched)):
        row = enriched.iloc[i]
        bar_time = row["datetime"].to_pydatetime()
        if bar_time.time() < no_before or bar_time.time() > no_after:
            continue

        c = float(row["close"])
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        vw = float(row["vwap"]) if pd.notna(row["vwap"]) else c
        r = float(row["rsi"])
        adx_v = float(row["adx"])
        atr_v = float(row["atr"]) if pd.notna(row["atr"]) else 0.0
        st_dir = int(row["st_dir"]) if pd.notna(row["st_dir"]) else 0
        vol = float(row["volume"])
        vma = float(row["vol_ma"]) if pd.notna(row["vol_ma"]) else 0.0

        # ATR sufficiency
        if atr_v > 0 and (atr_v / max(c, 1e-9) * 100.0) < atr_min_pct:
            continue

        if bias == "bullish":
            struct_hit = bool(row["bos_bull"]) or (use_choch and bool(row["choch_bull"]))
            if not struct_hit:
                continue
            if c < vw:
                continue
            if r < rsi_bull:
                continue
            if adx_v < adx_min:
                continue
            if require_st and st_dir != 1:
                continue
            if not _vol_ok(vol, vma, require_vol):
                continue
            # Optional CPR: prefer price above TC/pivot for CE
            if cpr and bool(cfg.get("bos_require_above_cpr", False)) and c < cpr["pivot"]:
                continue

            side = "buy"
            kind = "nifty_ce_bos"
            swing_low = float(row["last_swing_low"]) if pd.notna(row["last_swing_low"]) else l
            swing_high = float(row["last_swing_high"]) if pd.notna(row["last_swing_high"]) else h
            stop = min(swing_low, l) - max(0.5, atr_v * 0.1)
            # Entry next bar open if available
            entry_i = i + 1
            if entry_i >= len(enriched):
                return {
                    "status": "waiting",
                    "strategy": STRATEGY_NAME,
                    "why": "BOS bullish confirmed — awaiting next 5m for entry.",
                    "bias": bias,
                }
            entry_row = enriched.iloc[entry_i]
            entry_time = entry_row["datetime"].to_pydatetime()
            if entry_time.time() > no_after:
                continue
            entry = float(entry_row["open"])
            if stop >= entry:
                stop = entry - max(5.0, atr_v)
            risk = entry - stop
            target = entry + max(min_rr * risk, atr_v * 1.5)
            if or_high is None:
                or_high = swing_high
            if or_low is None:
                or_low = swing_low

            flow = option_flow_context(entry, side, cfg)
            if bool(cfg.get("require_option_flow", False)) and not flow.get("supportive", True):
                continue

            return _triggered(
                day=day,
                kind=kind,
                side=side,
                bias=bias,
                entry=entry,
                entry_time=entry_time,
                stop=stop,
                target=target,
                or_high=float(or_high),
                or_low=float(or_low),
                swing_low=swing_low,
                swing_high=swing_high,
                pdh=pdh,
                pdl=pdl,
                pdc=pdc,
                cpr=cpr,
                trend=trend,
                row=row,
                flow=flow,
                min_rr=min_rr,
                cfg=cfg,
                event="BOS/CHOCH bullish",
            )

        else:  # bearish
            struct_hit = bool(row["bos_bear"]) or (use_choch and bool(row["choch_bear"]))
            if not struct_hit:
                continue
            if c > vw:
                continue
            if r > rsi_bear:
                continue
            if adx_v < adx_min:
                continue
            if require_st and st_dir != -1:
                continue
            if not _vol_ok(vol, vma, require_vol):
                continue
            if cpr and bool(cfg.get("bos_require_below_cpr", False)) and c > cpr["pivot"]:
                continue

            side = "sell"
            kind = "nifty_pe_bos"
            swing_low = float(row["last_swing_low"]) if pd.notna(row["last_swing_low"]) else l
            swing_high = float(row["last_swing_high"]) if pd.notna(row["last_swing_high"]) else h
            stop = max(swing_high, h) + max(0.5, atr_v * 0.1)
            entry_i = i + 1
            if entry_i >= len(enriched):
                return {
                    "status": "waiting",
                    "strategy": STRATEGY_NAME,
                    "why": "BOS bearish confirmed — awaiting next 5m for entry.",
                    "bias": bias,
                }
            entry_row = enriched.iloc[entry_i]
            entry_time = entry_row["datetime"].to_pydatetime()
            if entry_time.time() > no_after:
                continue
            entry = float(entry_row["open"])
            if stop <= entry:
                stop = entry + max(5.0, atr_v)
            risk = stop - entry
            target = entry - max(min_rr * risk, atr_v * 1.5)
            if or_high is None:
                or_high = swing_high
            if or_low is None:
                or_low = swing_low

            flow = option_flow_context(entry, side, cfg)
            if bool(cfg.get("require_option_flow", False)) and not flow.get("supportive", True):
                continue

            return _triggered(
                day=day,
                kind=kind,
                side=side,
                bias=bias,
                entry=entry,
                entry_time=entry_time,
                stop=stop,
                target=target,
                or_high=float(or_high),
                or_low=float(or_low),
                swing_low=swing_low,
                swing_high=swing_high,
                pdh=pdh,
                pdl=pdl,
                pdc=pdc,
                cpr=cpr,
                trend=trend,
                row=row,
                flow=flow,
                min_rr=min_rr,
                cfg=cfg,
                event="BOS/CHOCH bearish",
            )

    return {
        "status": "waiting",
        "strategy": STRATEGY_NAME,
        "bias": bias,
        "why": f"nifty_bos_v1: 15m {bias} OK — waiting 5m BOS + RSI/ADX/ST/VWAP confluence.",
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
    }


def _triggered(**kw) -> dict[str, Any]:
    day = kw["day"]
    kind = kw["kind"]
    side = kw["side"]
    bias = kw["bias"]
    entry = kw["entry"]
    entry_time = kw["entry_time"]
    stop = kw["stop"]
    target = kw["target"]
    risk = abs(entry - stop)
    rr = abs(target - entry) / risk if risk else 0.0
    row = kw["row"]
    trend = kw["trend"]
    flow = kw["flow"]
    cpr = kw["cpr"]
    min_rr = kw["min_rr"]
    cfg = kw["cfg"]

    return {
        "status": "triggered",
        "symbol_day": day.isoformat(),
        "strategy": STRATEGY_NAME,
        "setup": kind,
        "side": side,
        "or_high": kw["or_high"],
        "or_low": kw["or_low"],
        "or_mid": (kw["or_high"] + kw["or_low"]) / 2.0,
        "pdh": kw["pdh"],
        "pdl": kw["pdl"],
        "pdc": kw["pdc"],
        "cpr": cpr,
        "entry": entry,
        "entry_time": entry_time,
        "planned_target": target,
        "signal_low": kw["swing_low"],
        "signal_high": kw["swing_high"],
        "computed_stop": stop,
        "computed_target": target,
        "reward_risk": round(rr, 3),
        "pct_over": round(
            abs(entry - (kw["or_high"] if side == "buy" else kw["or_low"])) / max(entry, 1e-9) * 100,
            3,
        ),
        "bias": bias,
        "rsi": float(row["rsi"]),
        "adx": float(row["adx"]),
        "atr": float(row["atr"]) if pd.notna(row["atr"]) else None,
        "st_dir": int(row["st_dir"]),
        "option_flow": flow,
        "why": (
            f"NIFTY BOS v1 {kind}: 15m {bias} (EMA20/50); VWAP OK; {kw['event']}; "
            f"RSI={float(row['rsi']):.0f} ADX={float(row['adx']):.0f} ST={'green' if int(row['st_dir'])==1 else 'red'}; "
            f"entry {entry:.2f} @ {entry_time.strftime('%H:%M')} stop {stop:.2f} target {target:.2f} ({rr:.2f}R). "
            f"Flow: {flow.get('why')}"
        ),
        "strategy_steps": [
            "Priority: Structure → VWAP → EMA20/50 → Volume → ATR → soft OI/VIX → RSI/ADX/ST.",
            f"15m trend {bias}: EMA{cfg.get('bos_ema_fast',20)}={trend.get('ema20'):.1f} "
            f"vs EMA{cfg.get('bos_ema_slow',50)}={trend.get('ema50'):.1f}.",
            f"5m {kw['event']} with VWAP + RSI/ADX/Supertrend confluence.",
            "Volume confirmed when futures/proxy volume available; else soft-skip.",
            "OI/PCR/MaxPain: contextual only — soft pass when chain not wired.",
            f"SL = structure swing; target ≥ {min_rr}R.",
        ],
    }


def scan_nifty_bos_universe(
    candle_map: dict[str, pd.DataFrame],
    day: dt.date,
    cfg: dict,
    as_of: Optional[dt.datetime] = None,
    bars_15m_map: Optional[dict[str, pd.DataFrame]] = None,
    prior_map: Optional[dict[str, pd.DataFrame]] = None,
    volume_map: Optional[dict[str, pd.DataFrame]] = None,
) -> list[dict[str, Any]]:
    hits = []
    bars_15m_map = bars_15m_map or {}
    prior_map = prior_map or {}
    volume_map = volume_map or {}
    for symbol, frame in candle_map.items():
        result = find_nifty_bos_trigger(
            frame,
            day,
            cfg,
            as_of=as_of,
            bars_15m=bars_15m_map.get(symbol),
            prior_df=prior_map.get(symbol),
            volume_df=volume_map.get(symbol),
        )
        if result.get("status") == "triggered":
            hits.append({"symbol": symbol, **result})
    hits.sort(key=lambda h: h.get("reward_risk", 0), reverse=True)
    return hits
