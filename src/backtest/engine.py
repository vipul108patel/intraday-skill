"""Backtest: replay historical Angel One candles through the skill pipeline."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from src.angelone.client import AngelOneClient, get_client
from src.core.config import load_trading_config
from src.db.store import get_store
from src.market.calendar import IST, is_trading_day, parse_hhmm, previous_trading_days
from src.market.context import load_symbol_context
from src.models.trade import BacktestSummary, ExitReason, Mode, TradeStatus
from src.strategies.pipeline import evaluate_symbol_day
from src.strategies.risk import daily_loss_limit_inr
from src.strategies.trail import simulate_with_trail

logger = logging.getLogger(__name__)


def _map_exit_reason(reason: str) -> ExitReason:
    key = reason.split("+")[-1] if reason else "eod"
    return {
        "stop": ExitReason.STOP,
        "trail": ExitReason.TRAIL,
        "target": ExitReason.TARGET,
        "flat_by": ExitReason.FLAT_BY,
        "eod": ExitReason.EOD,
    }.get(key, ExitReason.EOD)


def simulate_exit(
    bars: pd.DataFrame,
    entry_time: dt.datetime,
    entry: float,
    stop: float,
    target: float,
    flat_by: str,
    day: dt.date,
    side: str = "buy",
    cfg: Optional[dict] = None,
    qty: int = 0,
) -> tuple[float, dt.datetime, ExitReason, dict]:
    """Bar-by-bar after entry with optional scale-out + trailing stop."""
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=IST)
    flat_t = parse_hhmm(flat_by)
    flat_dt = dt.datetime.combine(day, flat_t, tzinfo=IST)
    cfg = cfg or {}

    px, t, reason, meta = simulate_with_trail(
        bars, entry_time, entry, stop, target, flat_dt, cfg, side=side, qty=qty
    )
    return px, t, _map_exit_reason(reason), meta


def run_backtest(
    symbols: Optional[list[str]] = None,
    days: Optional[int] = None,
    end_date: Optional[dt.date] = None,
    cfg: Optional[dict] = None,
    client: Optional[AngelOneClient] = None,
) -> BacktestSummary:
    cfg = cfg or load_trading_config()
    client = client or get_client()
    store = get_store()
    symbols = [s.upper() for s in (symbols or cfg.get("watchlist", []))]
    days = int(days or cfg.get("backtest_default_days", 30))
    end_date = end_date or dt.datetime.now(IST).date()
    if not is_trading_day(end_date):
        while not is_trading_day(end_date):
            end_date -= dt.timedelta(days=1)

    session_days = previous_trading_days(end_date + dt.timedelta(days=1), days)
    started = dt.datetime.utcnow()
    trades_out = []
    equity = float(cfg.get("paper_starting_cash", cfg.get("budget", 100000)))
    peak = equity
    max_dd = 0.0

    store.add_event(
        "backtest",
        f"Starting backtest {len(symbols)} symbols × {days} days ({cfg.get('strategy')})",
        {"symbols": symbols, "days": days},
    )

    for day in session_days:
        day_trades = 0
        daily_risk = 0.0
        realized_day = 0.0
        consecutive_losses = 0
        day_cap = daily_loss_limit_inr(cfg, cash=equity)

        for symbol in symbols:
            if consecutive_losses >= int(cfg.get("max_consecutive_losses", 2)):
                break
            if realized_day < 0 and abs(realized_day) >= day_cap - 1e-6:
                break

            try:
                ctx = load_symbol_context(client, symbol, day, cfg)
            except Exception as e:
                logger.warning("Skip %s %s: %s", symbol, day, e)
                continue
            df = ctx.get("df_5m")
            if df is None or df.empty:
                continue

            trade = evaluate_symbol_day(
                symbol,
                df,
                day,
                cfg=cfg,
                mode=Mode.BACKTEST,
                cash=equity,
                bars_15m=ctx.get("df_15m"),
                prior_df=ctx.get("prior_df"),
                trades_today=day_trades,
                daily_risk_used=daily_risk,
                consecutive_losses=consecutive_losses,
                realized_day_pnl=realized_day,
            )
            if trade is None:
                continue
            if not trade.risk_verdict or not trade.risk_verdict.passed:
                trade.status = TradeStatus.RISK_BLOCKED
                store.upsert_trade(trade)
                continue
            if day_trades >= int(cfg.get("max_trades_per_day", 3)):
                trade.status = TradeStatus.RISK_BLOCKED
                if trade.risk_verdict:
                    trade.risk_verdict.passed = False
                    trade.risk_verdict.reasons.append("max trades/day in backtest")
                store.upsert_trade(trade)
                continue
            if daily_risk + (trade.max_loss_planned or 0) > day_cap:
                trade.status = TradeStatus.RISK_BLOCKED
                store.upsert_trade(trade)
                continue

            qty = trade.qty
            entry_px = float(trade.planned_entry)
            entry_tm = trade.entry_time
            stop = float(trade.planned_stop)
            target = float(trade.planned_target)
            side = trade.side or "buy"
            slip = float(cfg.get("paper_slippage_bps", 2)) / 10000.0
            fill_entry = entry_px * (1 + slip) if side == "buy" else entry_px * (1 - slip)

            trade.mark_open(fill_entry, entry_tm, qty)
            exit_px, exit_tm, reason, trail_meta = simulate_exit(
                df,
                entry_tm,
                fill_entry,
                stop,
                target,
                trade.flat_by or cfg.get("flat_by", "15:15"),
                day,
                side=side,
                cfg=cfg,
                qty=qty,
            )
            if side == "buy":
                fill_exit = exit_px * (1 - slip)
            else:
                fill_exit = exit_px * (1 + slip)

            trade.raw["trail"] = trail_meta
            steps = list(trade.strategy_steps or [])
            for leg in trail_meta.get("scale_outs") or []:
                steps.append(
                    f"Lot {leg.get('lot_index')}: booked @ {leg.get('price')} "
                    f"(≈₹{leg.get('pnl_inr')} / lot)"
                )
            if trail_meta.get("runner"):
                rnr = trail_meta["runner"]
                steps.append(
                    f"Remaining exit: {rnr.get('qty')} qty @ {rnr.get('price')} ({rnr.get('reason')})"
                )
            if trail_meta.get("vwap_exit") is not None:
                steps.append(f"Blended VWAP exit {trail_meta.get('vwap_exit')}")
            trade.strategy_steps = steps
            if trail_meta.get("scale_outs"):
                trade.raw["exit_label"] = f"ladder+{reason.value}"
            trade.mark_closed(fill_exit, exit_tm, reason)
            pnl = float(trade.pnl or 0.0)
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            daily_risk += trade.max_loss_planned or 0.0
            realized_day += pnl
            day_trades += 1
            if pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0
            store.upsert_trade(trade)
            trades_out.append(trade)

    ended = dt.datetime.utcnow()
    wins = sum(1 for t in trades_out if (t.pnl or 0) > 0)
    losses = sum(1 for t in trades_out if (t.pnl or 0) < 0)
    flats = len(trades_out) - wins - losses
    gross_win = sum(t.pnl for t in trades_out if (t.pnl or 0) > 0)
    gross_loss = abs(sum(t.pnl for t in trades_out if (t.pnl or 0) < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_r = (
        sum(t.pnl_r or 0 for t in trades_out) / len(trades_out) if trades_out else 0.0
    )
    summary = BacktestSummary(
        started_at=started,
        ended_at=ended,
        symbols=symbols,
        strategy=str(cfg.get("strategy", "nifty_signal_v1")),
        days=days,
        trades=len(trades_out),
        wins=wins,
        losses=losses,
        flats=flats,
        win_rate=(wins / len(trades_out) * 100) if trades_out else 0.0,
        total_pnl=round(sum(t.pnl or 0 for t in trades_out), 2),
        avg_r=round(avg_r, 3),
        max_drawdown=round(max_dd, 2),
        profit_factor=round(pf if pf != float("inf") else 999.0, 3),
        notes=(
            f"{cfg.get('strategy', 'nifty_signal_v1')}: "
            + (
                "BOS/CHOCH + VWAP + EMA20/50 + RSI/ADX/ST/ATR; soft OI/VIX. "
                if str(cfg.get("strategy", "")).lower().startswith("nifty_bos")
                else "EMA20/VWAP/OR break+retest+confirm. "
            )
            + "Risk 5–7% · profit 10–12% · daily loss / trade caps from config."
        ),
    )
    store.save_backtest(summary)
    store.add_event(
        "backtest",
        f"Backtest done: {summary.trades} trades, PnL ₹{summary.total_pnl}",
        summary.model_dump(mode="json"),
    )
    return summary
