"""Trailing + INR lot ladder: book lots at ₹ targets; stop = exit all remaining."""

from __future__ import annotations

from typing import Any, List, Optional, Tuple


def unrealized_r(entry: float, stop: float, price: float, side: str = "buy") -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    if side == "buy":
        return (price - entry) / risk
    return (entry - price) / risk


def _lot_size(cfg: dict) -> int:
    if str(cfg.get("trade_instrument", "")).lower() == "options":
        return max(1, int(cfg.get("option_lot_size") or cfg.get("lot_size") or 65))
    return max(1, int(cfg.get("lot_size") or cfg.get("option_lot_size") or 65))


def _ladder_inr(cfg: dict) -> List[float]:
    raw = cfg.get("scale_out_ladder_inr") or [1000, 1500, 1800]
    return [float(x) for x in raw]


def _pnl_per_lot(entry: float, price: float, lot: int, side: str, point_value: float = 1.0) -> float:
    direction = 1 if side == "buy" else -1
    return direction * (price - entry) * lot * point_value


def _price_for_lot_pnl(
    entry: float, pnl_inr: float, lot: int, side: str, point_value: float = 1.0
) -> float:
    """Index/proxy price that yields pnl_inr on one lot."""
    move = pnl_inr / max(lot * point_value, 1e-9)
    return entry + move if side == "buy" else entry - move


def update_trail_stop(
    entry: float,
    initial_stop: float,
    current_stop: float,
    extreme: float,
    cfg: dict,
    side: str = "buy",
    force_active: bool = False,
) -> Tuple[float, bool, str]:
    if not cfg.get("trail_enabled", True):
        return current_stop, False, "trail off"

    risk = abs(entry - initial_stop)
    if risk <= 0:
        return current_stop, False, "no risk"

    activate_r = float(cfg.get("trail_activate_r", 0.8))
    step_r = float(cfg.get("trail_step_r", 0.35))
    to_be = bool(cfg.get("trail_to_breakeven", True))

    fav = extreme - entry if side == "buy" else entry - extreme
    r_now = fav / risk
    if not force_active and r_now < activate_r:
        return current_stop, False, f"trail waiting ({r_now:.2f}R < {activate_r}R)"

    if side == "buy":
        trailed = extreme - step_r * risk
        if to_be:
            trailed = max(trailed, entry)
        new_stop = max(current_stop, trailed, initial_stop)
        note = f"trail active · peak {extreme:.2f} · stop→{new_stop:.2f} ({r_now:.2f}R)"
    else:
        trailed = extreme + step_r * risk
        if to_be:
            trailed = min(trailed, entry)
        new_stop = min(current_stop, trailed, initial_stop)
        note = f"trail active · trough {extreme:.2f} · stop→{new_stop:.2f} ({r_now:.2f}R)"

    return new_stop, True, note


def simulate_with_trail(
    bars,
    entry_time,
    entry: float,
    stop: float,
    target: float,
    flat_by_dt,
    cfg: dict,
    side: str = "buy",
    qty: int = 0,
) -> Tuple[float, object, str, dict]:
    """
    Multi-lot INR ladder (preferred):
      - Loss/stop → exit ALL remaining lots together
      - Profit → book 1 lot at each ladder: ₹1000, ₹1500, ₹1800 (configurable)
      - After each book, stop → breakeven; trail leftover lots
      - 1-lot trade: stop loss band / full profit target (no ladder)

    Returns VWAP exit, time, reason, meta.
    """
    import pandas as pd

    after = bars[bars["datetime"] > entry_time].copy() if bars is not None else pd.DataFrame()
    lot = _lot_size(cfg)
    point_value = float(cfg.get("point_value", 1))
    total_qty = int(qty or 0)
    total_lots = total_qty // lot if lot else 0
    mode = str(cfg.get("scale_out_mode", "inr_ladder")).lower()
    ladder = _ladder_inr(cfg) if cfg.get("scale_out_enabled", True) else []

    cur_stop = stop
    initial_stop = stop
    extreme = entry
    remaining_qty = total_qty
    booked: list[dict[str, Any]] = []
    ladder_i = 0
    force_trail = False

    trail_meta: dict[str, Any] = {
        "activated": False,
        "updates": [],
        "final_stop": stop,
        "peak": entry,
        "scale_out": None,
        "scale_outs": [],
        "ladder_inr": ladder,
        "lots": total_lots,
        "mode": mode,
    }

    def _hit_stop(high: float, low: float) -> bool:
        return low <= cur_stop if side == "buy" else high >= cur_stop

    def _hit_tgt(high: float, low: float) -> bool:
        return high >= target if side == "buy" else low <= target

    def _vwap_and_finish(final_px: float, t, reason: str):
        legs = list(booked)
        if remaining_qty > 0:
            legs.append(
                {
                    "lot_index": len(booked) + 1,
                    "lots": remaining_qty // lot,
                    "qty": remaining_qty,
                    "price": round(final_px, 2),
                    "pnl_inr": round(
                        _pnl_per_lot(entry, final_px, remaining_qty, side, point_value), 2
                    ),
                    "reason": reason,
                }
            )
        tot_qty = sum(int(x["qty"]) for x in legs) or 1
        vwap = sum(float(x["price"]) * int(x["qty"]) for x in legs) / tot_qty
        trail_meta["scale_outs"] = booked
        trail_meta["scale_out"] = booked[0] if booked else None
        trail_meta["runner"] = legs[-1] if legs else None
        trail_meta["legs"] = legs
        trail_meta["vwap_exit"] = round(vwap, 2)
        trail_meta["final_stop"] = cur_stop
        label = "scale_out+" + reason if booked else reason
        return vwap, t, label, trail_meta

    for _, row in after.iterrows():
        t = row["datetime"].to_pydatetime()
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        if side == "buy":
            extreme = max(extreme, high)
        else:
            extreme = min(extreme, low)

        # 1) STOP = dump all remaining lots (hard loss exit)
        if _hit_stop(high, low):
            reason = "trail" if (force_trail or trail_meta["activated"]) and cur_stop != initial_stop else "stop"
            return _vwap_and_finish(cur_stop, t, reason)

        # 2) INR ladder: book one lot at each profit step while multiple lots remain
        if mode == "inr_ladder" and total_lots >= 2 and remaining_qty > lot:
            while ladder_i < len(ladder) and remaining_qty > lot:
                need = ladder[ladder_i]
                lvl = _price_for_lot_pnl(entry, need, lot, side, point_value)
                hit = high >= lvl if side == "buy" else low <= lvl
                if not hit:
                    break
                booked.append(
                    {
                        "lot_index": ladder_i + 1,
                        "lots": 1,
                        "qty": lot,
                        "price": round(lvl, 2),
                        "pnl_inr": round(need, 2),
                        "time": t.isoformat(),
                        "note": f"Lot {ladder_i + 1} booked @ ₹{need:.0f} profit ({lvl:.2f})",
                    }
                )
                remaining_qty -= lot
                ladder_i += 1
                if cfg.get("scale_out_move_be", True):
                    cur_stop = entry
                force_trail = True
                trail_meta["updates"].append(
                    {
                        "t": t.isoformat(),
                        "stop": cur_stop,
                        "note": booked[-1]["note"],
                        "event": "scale_out",
                        "extreme": extreme,
                    }
                )
                # If last lot also reaches next ladder on same bar, loop continues
            # If all but managed via ladder on this bar and nothing left — shouldn't happen
            if remaining_qty <= 0:
                return _vwap_and_finish(booked[-1]["price"], t, "target")

        # 3) Single-lot (or last runner) full target
        if remaining_qty > 0 and _hit_tgt(high, low):
            return _vwap_and_finish(target, t, "target")

        # 4) Trail leftover after at least one scale-out (or if trail forced)
        new_stop, activated, note = update_trail_stop(
            entry,
            initial_stop,
            cur_stop,
            extreme,
            cfg,
            side=side,
            force_active=force_trail,
        )
        if activated:
            trail_meta["activated"] = True
        if abs(new_stop - cur_stop) > 1e-9:
            trail_meta["updates"].append(
                {"t": t.isoformat(), "stop": new_stop, "note": note, "extreme": extreme}
            )
            cur_stop = new_stop
        trail_meta["final_stop"] = cur_stop
        trail_meta["peak"] = extreme

        if flat_by_dt is not None and t >= flat_by_dt:
            return _vwap_and_finish(close, t, "flat_by")

    if not after.empty:
        last = after.iloc[-1]
        return _vwap_and_finish(
            float(last["close"]), last["datetime"].to_pydatetime(), "eod"
        )
    return entry, entry_time, "eod", trail_meta
