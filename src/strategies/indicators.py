"""Technical indicators for NIFTY BOS strategy: EMA, RSI, ADX, ATR, Supertrend, structure."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    s = series.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Return DataFrame with +DI, -DI, ADX columns."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = atr(df, period)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / tr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / tr.replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    adx_s = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return pd.DataFrame({"+di": plus_di.fillna(0), "-di": minus_di.fillna(0), "adx": adx_s.fillna(0)})


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Classic Supertrend. direction: 1=bullish (green), -1=bearish (red)."""
    hl2 = (df["high"].astype(float) + df["low"].astype(float)) / 2.0
    atr_s = atr(df, period)
    upper = hl2 + multiplier * atr_s
    lower = hl2 - multiplier * atr_s
    st = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)
    close = df["close"].astype(float)

    for i in range(len(df)):
        if i == 0 or pd.isna(atr_s.iloc[i]):
            st.iloc[i] = lower.iloc[i] if not pd.isna(lower.iloc[i]) else close.iloc[i]
            direction.iloc[i] = 1
            continue
        curr_upper = upper.iloc[i]
        curr_lower = lower.iloc[i]
        prev_st = st.iloc[i - 1]
        prev_dir = int(direction.iloc[i - 1])
        prev_close = close.iloc[i - 1]

        if curr_lower > prev_st or prev_close < prev_st:
            final_lower = curr_lower
        else:
            final_lower = prev_st if prev_dir == 1 else curr_lower

        if curr_upper < prev_st or prev_close > prev_st:
            final_upper = curr_upper
        else:
            final_upper = prev_st if prev_dir == -1 else curr_upper

        if prev_dir == 1:
            if close.iloc[i] < final_lower:
                direction.iloc[i] = -1
                st.iloc[i] = final_upper
            else:
                direction.iloc[i] = 1
                st.iloc[i] = final_lower
        else:
            if close.iloc[i] > final_upper:
                direction.iloc[i] = 1
                st.iloc[i] = final_lower
            else:
                direction.iloc[i] = -1
                st.iloc[i] = final_upper

    return pd.DataFrame({"supertrend": st, "st_dir": direction})


def swing_points(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Mark fractal swing highs/lows."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    sh = pd.Series(False, index=df.index)
    sl = pd.Series(False, index=df.index)
    for i in range(left, len(df) - right):
        window_h = high.iloc[i - left : i + right + 1]
        window_l = low.iloc[i - left : i + right + 1]
        if high.iloc[i] >= window_h.max():
            sh.iloc[i] = True
        if low.iloc[i] <= window_l.min():
            sl.iloc[i] = True
    return pd.DataFrame({"swing_high": sh, "swing_low": sl})


def detect_bos_choch(
    df: pd.DataFrame,
    left: int = 2,
    right: int = 2,
) -> pd.DataFrame:
    """
    Market structure events on closed bars:
      bos_bull: close breaks last confirmed swing high (continuation)
      bos_bear: close breaks last confirmed swing low
      choch_bull: after bearish structure, close breaks last swing high
      choch_bear: after bullish structure, close breaks last swing low
      structure: 1 bullish / -1 bearish / 0 undefined
    """
    swings = swing_points(df, left=left, right=right)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    last_sh: Optional[float] = None
    last_sl: Optional[float] = None
    structure = 0  # 1 bull, -1 bear
    bos_bull = []
    bos_bear = []
    choch_bull = []
    choch_bear = []
    struct_series = []
    sh_level = []
    sl_level = []

    for i in range(len(df)):
        # Confirm swings only after `right` bars have passed
        conf_i = i - right
        if conf_i >= left:
            if bool(swings["swing_high"].iloc[conf_i]):
                last_sh = float(high.iloc[conf_i])
            if bool(swings["swing_low"].iloc[conf_i]):
                last_sl = float(low.iloc[conf_i])

        c = float(close.iloc[i])
        bb = be = cb = ce = False
        if last_sh is not None and c > last_sh:
            if structure == -1:
                cb = True
            else:
                bb = True
            structure = 1
        elif last_sl is not None and c < last_sl:
            if structure == 1:
                ce = True
            else:
                be = True
            structure = -1

        bos_bull.append(bb)
        bos_bear.append(be)
        choch_bull.append(cb)
        choch_bear.append(ce)
        struct_series.append(structure)
        sh_level.append(last_sh)
        sl_level.append(last_sl)

    return pd.DataFrame(
        {
            "bos_bull": bos_bull,
            "bos_bear": bos_bear,
            "choch_bull": choch_bull,
            "choch_bear": choch_bear,
            "structure": struct_series,
            "last_swing_high": sh_level,
            "last_swing_low": sl_level,
        },
        index=df.index,
    )


def daily_cpr(prior_high: float, prior_low: float, prior_close: float) -> dict:
    pivot = (prior_high + prior_low + prior_close) / 3.0
    bc = (prior_high + prior_low) / 2.0
    tc = (pivot - bc) + pivot
    return {"pivot": pivot, "bc": min(bc, tc), "tc": max(bc, tc)}
