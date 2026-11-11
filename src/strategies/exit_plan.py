"""Exit planner — capital % risk/reward + OR structure stop (Nifty)."""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Optional

import pandas as pd

from src.models.trade import TradePlan

CENT = Decimal("0.05")
MIN_RISK_PCT = Decimal("0.05")
# Fallback floor; preferred risk comes from loss_per_lot_inr / lot_size (~1100/65 ≈ 16.9)
MIN_RISK_POINTS_INDEX = Decimal("15")


def loss_per_lot_inr(cfg: dict) -> float:
    """Target loss band per lot (₹1000–1200 default mid)."""
    mid = float(cfg.get("loss_per_lot_inr", 1100))
    lo = float(cfg.get("loss_per_lot_min_inr", 1000))
    hi = float(cfg.get("loss_per_lot_max_inr", 1200))
    return max(lo, min(hi, mid))


def profit_per_lot_inr(cfg: dict) -> float:
    """Target profit band per lot (₹1500–1700 default mid)."""
    mid = float(cfg.get("profit_per_lot_inr", 1600))
    lo = float(cfg.get("profit_per_lot_min_inr", 1500))
    hi = float(cfg.get("profit_per_lot_max_inr", 1700))
    return max(lo, min(hi, mid))


def pts_for_lot_inr(cfg: dict, inr: float) -> Decimal:
    lot = _d(str(cfg.get("option_lot_size") or cfg.get("lot_size") or 65))
    pv = _d(str(cfg.get("point_value", 1)))
    return _d(str(inr)) / max(lot * pv, Decimal(1))


def _d(x: float | str | Decimal) -> Decimal:
    return Decimal(str(x))


def _round_stop(price: Decimal) -> Decimal:
    return (price / CENT).to_integral_value(rounding=ROUND_FLOOR) * CENT


def _round_target(price: Decimal) -> Decimal:
    return (price / CENT).to_integral_value(rounding=ROUND_HALF_UP) * CENT


def atr(df: pd.DataFrame, n: int = 14) -> Optional[float]:
    if df is None or len(df) < 2:
        return None
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    use = tr.dropna()
    if use.empty:
        return None
    window = use.iloc[-n:] if len(use) >= n else use
    return float(window.mean())


def capital_rr(cfg: dict) -> tuple[float, float, float]:
    """Return (risk_pct, profit_pct, target_r) from capital percentage bands."""
    risk_pct = float(cfg.get("risk_pct_of_capital", 4.0))
    profit_pct = float(cfg.get("profit_pct_of_capital", 10.0))
    # clamp into declared bands
    risk_pct = max(float(cfg.get("risk_pct_min", 3)), min(float(cfg.get("risk_pct_max", 5)), risk_pct))
    profit_pct = max(
        float(cfg.get("profit_pct_min", 9)),
        min(float(cfg.get("profit_pct_max", 12)), profit_pct),
    )
    target_r = profit_pct / risk_pct if risk_pct else float(cfg.get("default_target_r", 1.5))
    return risk_pct, profit_pct, target_r


def _affordable_risk_pts(cfg: dict, entry_d: Decimal, budget: float, risk_pct: float) -> Decimal:
    # Prefer explicit per-lot INR loss → points (e.g. ₹1100 / 65 ≈ 16.9 pts)
    per_lot = pts_for_lot_inr(cfg, loss_per_lot_inr(cfg))
    pct_cap = entry_d * _d(str(cfg.get("max_struct_risk_pct", 0.10))) / Decimal(100)
    max_struct = min(per_lot, pct_cap) if pct_cap > 0 else per_lot
    if max_struct < MIN_RISK_POINTS_INDEX:
        max_struct = MIN_RISK_POINTS_INDEX
    return max_struct


def build_plan(
    symbol: str,
    entry: float,
    or_high: float,
    or_low: float,
    cfg: dict,
    stop_rule: Optional[str] = None,
    target_r: Optional[float] = None,
    bars: Optional[pd.DataFrame] = None,
    entry_time=None,
    why: str = "",
    strategy_steps: Optional[list[str]] = None,
    signal_low: Optional[float] = None,
    signal_high: Optional[float] = None,
    side: str = "buy",
    planned_target: Optional[float] = None,
    planned_stop: Optional[float] = None,
) -> TradePlan:
    stop_rule = stop_rule or cfg.get("default_stop", "signal-low")
    risk_pct, profit_pct, auto_r = capital_rr(cfg)
    loss_inr = loss_per_lot_inr(cfg)
    profit_inr = profit_per_lot_inr(cfg)
    # Prefer capital % R, then INR bands, then config floor (min 1:2 for v1)
    inr_r = (profit_inr / loss_inr) if loss_inr else auto_r
    cfg_r = float(cfg.get("default_target_r", max(auto_r, inr_r)))
    min_rr_cfg = float(cfg.get("min_reward_risk", 2.0))
    target_r = float(
        target_r if target_r is not None else max(min_rr_cfg, auto_r, inr_r, cfg_r)
    )
    entry_d = _d(entry)
    budget = float(cfg.get("budget", 25000))
    is_short = str(side).lower() == "sell"
    # Pin stop distance to per-lot INR loss band
    min_pts = pts_for_lot_inr(cfg, float(cfg.get("loss_per_lot_min_inr", 1250)))
    max_struct = _affordable_risk_pts(cfg, entry_d, budget, risk_pct)
    or_mid = (_d(or_high) + _d(or_low)) / Decimal(2)

    # When signal provides a structure stop, keep it (do not widen to INR min pts).
    structure_stop = planned_stop is not None

    if is_short:
        if planned_stop is not None:
            stop_raw = _d(planned_stop)
            stop_expl = f"signal stop {planned_stop}"
        elif signal_high is not None:
            stop_raw = _d(signal_high)
            stop_expl = f"retest swing high {signal_high}"
        else:
            stop_raw = _d(or_low) + Decimal("0.5")
            stop_expl = "fallback above OR low"
        if (stop_raw - entry_d) > max_struct:
            stop_raw = entry_d + max_struct
            stop_expl += f"; hard-capped to affordable risk {float(max_struct):.2f} pts"
        stop = (stop_raw / CENT).to_integral_value(rounding=ROUND_HALF_UP) * CENT
        if stop <= entry_d:
            stop = (entry_d + MIN_RISK_POINTS_INDEX) / CENT
            stop = stop.to_integral_value(rounding=ROUND_HALF_UP) * CENT
            stop_expl += f"; adjusted above entry by {float(MIN_RISK_POINTS_INDEX):.2f}"
        risk = stop - entry_d
        if not structure_stop and risk < min_pts:
            stop = (entry_d + min_pts) / CENT
            stop = stop.to_integral_value(rounding=ROUND_HALF_UP) * CENT
            risk = stop - entry_d
            stop_expl += f"; widened to min risk {float(min_pts):.2f} pts"
        if planned_target is not None:
            target = _round_stop(_d(planned_target))
            stop_expl += "; structure target from signal"
        else:
            target = _round_stop(entry_d - _d(target_r) * risk)
        rr = float((entry_d - target) / risk) if risk else 0.0
        if rr + 1e-9 < min_rr_cfg:
            target = _round_stop(entry_d - _d(str(min_rr_cfg)) * risk)
            rr = float((entry_d - target) / risk) if risk else 0.0
        target_r = rr
    else:
        if planned_stop is not None and stop_rule == "signal-low":
            stop_raw = _d(planned_stop)
            stop_expl = f"signal stop {planned_stop}"
        elif stop_rule == "signal-low":
            if signal_low is None:
                stop_raw = _d(or_high) - Decimal("0.5")
                stop_expl = "fallback under OR high (no signal bar low)"
            else:
                stop_raw = _d(signal_low)
                stop_expl = f"retest swing low {signal_low}"
            if (entry_d - stop_raw) > max_struct:
                stop_raw = entry_d - max_struct
                stop_expl += f"; hard-capped to affordable risk {float(max_struct):.2f} pts"
        elif stop_rule == "or-mid":
            stop_raw = or_mid
            stop_expl = f"opening-range midpoint: ({or_high}+{or_low})/2"
        elif stop_rule == "or-low":
            stop_raw = _d(or_low)
            stop_expl = "opening-range low"
        elif stop_rule.startswith("pct:"):
            pct = _d(stop_rule.split(":", 1)[1])
            stop_raw = entry_d * (Decimal(1) - pct / Decimal(100))
            stop_expl = f"{pct}% under entry"
        elif stop_rule.startswith("atr:"):
            k = _d(stop_rule.split(":", 1)[1])
            a = atr(bars) if bars is not None else None
            if a is None:
                raise ValueError("ATR stop needs enough bars")
            stop_raw = entry_d - k * _d(a)
            stop_expl = f"{k}×ATR({a:.2f}) under entry"
        elif stop_rule.startswith("price:"):
            stop_raw = _d(stop_rule.split(":", 1)[1])
            stop_expl = f"fixed price {stop_raw}"
        else:
            raise ValueError(f"Unknown stop rule: {stop_rule}")

        stop = _round_stop(stop_raw)
        if stop >= entry_d:
            stop = _round_stop(entry_d - MIN_RISK_POINTS_INDEX)
            stop_expl += f"; adjusted under entry by {float(MIN_RISK_POINTS_INDEX):.2f}"
        if stop >= entry_d:
            raise ValueError(f"Stop {stop} must be below entry {entry_d}")
        risk = entry_d - stop
        if not structure_stop and risk < min_pts:
            stop = _round_stop(entry_d - min_pts)
            risk = entry_d - stop
            stop_expl += f"; widened to min risk {float(min_pts):.2f} pts"
        # Prefer structure target from signal; else enforce min RR
        rr_pts = _d(str(min_rr_cfg)) * risk
        if planned_target is not None:
            target = _round_target(_d(planned_target))
            stop_expl += "; structure target from signal"
        else:
            profit_pts = pts_for_lot_inr(cfg, profit_inr)
            target = _round_target(entry_d + max(profit_pts, rr_pts))
        rr = float((target - entry_d) / risk) if risk else 0.0
        if rr + 1e-9 < min_rr_cfg:
            target = _round_target(entry_d + rr_pts)
            rr = float((target - entry_d) / risk) if risk else 0.0
        target_r = rr

    ladder = cfg.get("scale_out_ladder_inr") or [1500, 2250, 2750]
    steps = list(strategy_steps or [])
    steps.extend(
        [
            f"Side={'short/PE' if is_short else 'long/CE'}; stop {stop} via {stop_expl}.",
            f"Capital bands: risk {risk_pct:.1f}% (₹{loss_inr:.0f}/lot) · "
            f"profit {profit_pct:.1f}% (₹{profit_inr:.0f}/lot) · min RR {min_rr_cfg}.",
            (
                f"Scale-out ladder {ladder} INR/lot."
                if cfg.get("scale_out_enabled")
                else "Scale-out disabled — primary exit is SL or ≥1:2 target."
            ),
            f"Target {target} ≈ {target_r:.2f}R (R={float(risk):.2f} pts).",
            f"Flat by {cfg.get('flat_by', '15:10')} IST — no overnight hold.",
        ]
    )

    return TradePlan(
        symbol=symbol.upper(),
        side="sell" if is_short else "buy",
        strategy=str(cfg.get("strategy", "nifty_signal_v1")),
        setup="nifty_signal_v1_5m",
        entry=float(entry_d),
        stop=float(stop),
        target=float(target),
        stop_rule=stop_rule,
        target_r=round(target_r, 3),
        risk_per_share=float(risk),
        reward_risk=round(rr, 3),
        or_high=or_high,
        or_low=or_low,
        entry_time=entry_time,
        flat_by=cfg.get("flat_by", "15:10"),
        why=why
        or (
            f"NIFTY Signal v1 {('short' if is_short else 'long')} {symbol}: entry {entry}, "
            f"stop {stop}, target {target} ({target_r:.2f}R)"
        ),
        strategy_steps=steps,
    )


def plan_to_dict(plan: TradePlan) -> dict[str, Any]:
    return plan.model_dump(mode="json")