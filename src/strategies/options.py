"""Options strike selection — index (NIFTY) or stock F&O (OPTSTK) CE/PE."""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

NIFTY_STRIKE_STEP = 50


def round_strike(spot: float, step: float = NIFTY_STRIKE_STEP, mode: str = "atm") -> int:
    step = float(step) or float(NIFTY_STRIKE_STEP)
    base = int(round(spot / step) * step)
    if mode == "atm":
        return base
    if mode == "otm1":
        return int(base + step)
    if mode == "itm1":
        return int(base - step)
    return base


def option_side_for_trade(side: str = "buy") -> str:
    """Long buy → CE; sell/short → PE."""
    if side.lower() == "sell":
        return "PE"
    return "CE"


def infer_strike_step(
    instruments_df: Optional[pd.DataFrame],
    underlying: str,
    spot: float,
    fallback: int = 10,
) -> int:
    """Infer strike interval from NFO master around spot; else price heuristic."""
    if instruments_df is not None and not instruments_df.empty:
        try:
            work = instruments_df
            sub = work[
                (work["exch_seg"].astype(str).str.upper() == "NFO")
                & (work["name"].astype(str).str.upper() == underlying.upper())
            ].copy()
            if sub.empty:
                sub = work[
                    (work["exch_seg"].astype(str).str.upper() == "NFO")
                    & (work["symbol"].astype(str).str.upper().str.startswith(underlying.upper()))
                ].copy()
            if not sub.empty and "strike" in sub.columns:
                strikes = sorted(
                    {
                        s
                        for s in (_strike_num(v) for v in sub["strike"].tolist())
                        if s is not None and abs(s - spot) <= max(spot * 0.12, 200)
                    }
                )
                diffs = [int(round(strikes[i + 1] - strikes[i])) for i in range(len(strikes) - 1)]
                diffs = [d for d in diffs if d > 0]
                if diffs:
                    step = Counter(diffs).most_common(1)[0][0]
                    if step > 0:
                        return int(step)
        except Exception as e:
            logger.debug("infer_strike_step: %s", e)

    # Heuristic by spot price
    if spot < 100:
        return 2
    if spot < 250:
        return 5
    if spot < 1000:
        return 10
    if spot < 5000:
        return 20
    return int(fallback or 50)


def pick_option_contract(
    spot: float,
    cfg: dict,
    side: str = "buy",
    as_of: Optional[dt.datetime] = None,
    instruments_df: Optional[pd.DataFrame] = None,
    underlying: Optional[str] = None,
) -> dict[str, Any]:
    """
    Pick CE/PE contract.
    underlying: stock symbol (RELIANCE) or index (NIFTY). Defaults to cfg.underlying.
    """
    opt_type = option_side_for_trade(side=side)
    desk = str(cfg.get("desk") or "nifty").lower()
    und = str(underlying or cfg.get("option_underlying") or cfg.get("underlying") or "NIFTY").upper()
    # Stocks desk always uses the scanned equity symbol as option underlying
    if desk == "stocks" and underlying:
        und = str(underlying).upper()

    configured_step = cfg.get("option_strike_step")
    if configured_step in (None, 0, "auto", "AUTO"):
        step = infer_strike_step(instruments_df, und, float(spot), fallback=10 if desk == "stocks" else 50)
    else:
        step = int(configured_step)

    mode = str(cfg.get("option_strike_mode", "atm")).lower()

    if opt_type == "CE":
        strike = round_strike(spot, step=step, mode=mode)
    else:
        base = int(round(spot / step) * step)
        if mode == "otm1":
            strike = int(base - step)
        elif mode == "itm1":
            strike = int(base + step)
        else:
            strike = base

    if isinstance(as_of, dt.datetime):
        as_of_date = as_of.date()
    elif isinstance(as_of, dt.date):
        as_of_date = as_of
    else:
        as_of_date = dt.date.today()

    expiry = None
    token = None
    tradingsymbol = None
    name = None
    lot_size = None
    instrument_type = None

    if instruments_df is not None and not instruments_df.empty:
        try:
            (
                expiry,
                token,
                tradingsymbol,
                name,
                lot_size,
                instrument_type,
                resolved_strike,
            ) = _resolve_from_master(instruments_df, und, strike, opt_type, as_of_date)
            if resolved_strike is not None:
                strike = int(resolved_strike)
        except Exception as e:
            logger.warning("Option master resolve failed (%s): %s", und, e)

    if expiry is None:
        expiry = _next_tuesday(as_of_date) if und == "NIFTY" else _next_month_endish(as_of_date)
    if tradingsymbol is None:
        mon = expiry.strftime("%d%b%y").upper()
        tradingsymbol = f"{und}{mon}{strike}{opt_type}"

    default_lot = int(cfg.get("option_lot_size") or cfg.get("lot_size") or (65 if und == "NIFTY" else 1))
    lot_size = int(lot_size or default_lot)

    half = step / 2
    if opt_type == "CE":
        if strike > spot + half:
            moneyness = "OTM"
        elif strike < spot - half:
            moneyness = "ITM"
        else:
            moneyness = "ATM"
    else:
        if strike < spot - half:
            moneyness = "OTM"
        elif strike > spot + half:
            moneyness = "ITM"
        else:
            moneyness = "ATM"

    return {
        "underlying": und,
        "option_type": opt_type,
        "strike": int(strike),
        "strike_step": int(step),
        "strike_mode": mode,
        "moneyness": moneyness,
        "spot_ref": round(float(spot), 2),
        "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry),
        "tradingsymbol": tradingsymbol,
        "token": token,
        "name": name or tradingsymbol,
        "exchange": "NFO",
        "lot_size": lot_size,
        "instrument_type": instrument_type or ("OPTSTK" if und != "NIFTY" else "OPTIDX"),
        "label": f"{und} {opt_type} {strike}",
        "why": (
            f"{und} {opt_type} strike {strike} ({moneyness}, {mode}, step {step}) "
            f"from spot {spot:.2f}; expiry {expiry}; symbol {tradingsymbol}; lot {lot_size}"
        ),
    }


def _next_tuesday(day: dt.date) -> dt.date:
    days_ahead = (1 - day.weekday()) % 7
    return day if days_ahead == 0 else day + dt.timedelta(days=days_ahead)


def _next_month_endish(day: dt.date) -> dt.date:
    """Rough monthly expiry fallback (last Thursday-ish) when master miss."""
    if day.month == 12:
        first_next = dt.date(day.year + 1, 1, 1)
    else:
        first_next = dt.date(day.year, day.month + 1, 1)
    last = first_next - dt.timedelta(days=1)
    # walk back to Thursday
    while last.weekday() != 3:
        last -= dt.timedelta(days=1)
    if last < day:
        return _next_month_endish(first_next)
    return last


def _parse_expiry(val) -> Optional[dt.date]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, dt.datetime):
        return val.date()
    if isinstance(val, dt.date):
        return val
    s = str(val).strip()
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y", "%d%b%y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(s.upper(), fmt).date()
        except ValueError:
            continue
    try:
        n = int(float(s))
        if n > 10_000_000_000:
            n //= 1000
        return dt.datetime.utcfromtimestamp(n).date()
    except Exception:
        return None


def _strike_num(v) -> Optional[float]:
    try:
        x = float(v)
        # Angel NFO master stores strikes in paise (×100): 80000 → 800, 2020000 → 20200
        if abs(x) >= 1000:
            x = x / 100.0
        return x
    except Exception:
        return None


def _strike_from_symbol(sym: str) -> Optional[float]:
    m = re.search(r"(\d+)(CE|PE)$", str(sym).upper())
    return float(m.group(1)) if m else None


def _resolve_from_master(
    df: pd.DataFrame,
    underlying: str,
    strike: int,
    opt_type: str,
    as_of: dt.date,
):
    work = df.copy()
    und = underlying.upper()
    sub = work[
        (work["exch_seg"].astype(str).str.upper() == "NFO")
        & (work["name"].astype(str).str.upper() == und)
    ].copy()
    if sub.empty:
        sub = work[
            (work["exch_seg"].astype(str).str.upper() == "NFO")
            & (work["symbol"].astype(str).str.upper().str.startswith(und))
        ].copy()
    if sub.empty:
        return None, None, None, None, None, None, None

    # Prefer options rows (OPTIDX / OPTSTK)
    if "instrumenttype" in sub.columns:
        opts = sub[sub["instrumenttype"].astype(str).str.upper().str.startswith("OPT")]
        if not opts.empty:
            sub = opts

    if "strike" in sub.columns:
        sub["_strike"] = sub["strike"].map(_strike_num)
    else:
        sub["_strike"] = sub["symbol"].astype(str).map(_strike_from_symbol)

    # Exact strike, else nearest available
    exact = sub[sub["_strike"] == float(strike)]
    pe_ce = opt_type.upper()
    exact = exact[exact["symbol"].astype(str).str.upper().str.endswith(pe_ce)]
    if exact.empty:
        near = sub[sub["symbol"].astype(str).str.upper().str.endswith(pe_ce)].copy()
        if near.empty or near["_strike"].isna().all():
            return None, None, None, None, None, None, None
        near["_dist"] = (near["_strike"] - float(strike)).abs()
        near = near.sort_values("_dist")
        sub = near
    else:
        sub = exact

    def _lot(row) -> Optional[int]:
        lot = row.get("lotsize")
        try:
            return int(float(lot)) if lot is not None and str(lot) not in ("", "nan") else None
        except Exception:
            return None

    if "expiry" in sub.columns:
        sub = sub.copy()
        sub["_exp"] = sub["expiry"].map(_parse_expiry)
        fut = sub[sub["_exp"].notna() & (sub["_exp"] >= as_of)].sort_values("_exp")
        if fut.empty:
            return None, None, None, None, None, None, None
        nearest_exp = fut.iloc[0]["_exp"]
        fut = fut[fut["_exp"] == nearest_exp]
        if "_dist" in fut.columns:
            fut = fut.sort_values("_dist")
        row = fut.iloc[0]
        resolved = int(row["_strike"]) if pd.notna(row.get("_strike")) else strike
        return (
            row["_exp"],
            str(row.get("token")),
            str(row.get("symbol")),
            str(row.get("name")),
            _lot(row),
            str(row.get("instrumenttype") or ""),
            resolved,
        )

    row = sub.iloc[0]
    resolved = int(row["_strike"]) if pd.notna(row.get("_strike")) else strike
    return (
        None,
        str(row.get("token")),
        str(row.get("symbol")),
        str(row.get("name")),
        _lot(row),
        str(row.get("instrumenttype") or ""),
        resolved,
    )
