"""Full skill pipeline: scan → entry → exit → size → risk (Indian market)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

import pandas as pd

from src.core.config import load_trading_config
from src.core.paths import TRADER_DIR, ensure_dirs
from src.market.calendar import now_ist
from src.models.trade import Mode, TradeRecord, TradeStatus
from src.strategies.exit_plan import build_plan
from src.strategies.nifty_bos import (
    find_nifty_bos_trigger,
    scan_nifty_bos_universe,
)
from src.strategies.nifty_signal import (
    STRATEGY_NAME as SIGNAL_STRATEGY,
    find_nifty_signal_trigger,
    scan_nifty_signal_universe,
)
from src.strategies.option_price import invested_from_premium, resolve_option_premium
from src.strategies.options import pick_option_contract
from src.strategies.risk import check_entry


def _active_strategy(cfg: dict) -> str:
    return str(cfg.get("strategy", SIGNAL_STRATEGY)).lower()


def _find_trigger(df, day, cfg, as_of=None, bars_15m=None, prior_df=None):
    name = _active_strategy(cfg)
    if name in ("nifty_bos_v1", "nifty_bos", "bos"):
        return find_nifty_bos_trigger(
            df, day, cfg, as_of=as_of, bars_15m=bars_15m, prior_df=prior_df
        )
    return find_nifty_signal_trigger(
        df, day, cfg, as_of=as_of, bars_15m=bars_15m, prior_df=prior_df
    )


def _scan_universe(candle_map, day, cfg, as_of=None, bars_15m_map=None, prior_map=None):
    name = _active_strategy(cfg)
    if name in ("nifty_bos_v1", "nifty_bos", "bos"):
        return scan_nifty_bos_universe(
            candle_map,
            day,
            cfg,
            as_of=as_of,
            bars_15m_map=bars_15m_map,
            prior_map=prior_map,
        )
    return scan_nifty_signal_universe(
        candle_map,
        day,
        cfg,
        as_of=as_of,
        bars_15m_map=bars_15m_map,
        prior_map=prior_map,
    )


def _instruments_df():
    try:
        from src.angelone.client import get_client

        return get_client().instruments()
    except Exception:
        return None


def write_json(name: str, payload: dict) -> None:
    ensure_dirs()
    path = TRADER_DIR / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
        f.write("\n")


def build_trade_from_trigger(
    symbol: str,
    trigger: dict[str, Any],
    cfg: dict,
    mode: Mode,
    bars: Optional[pd.DataFrame] = None,
    cash: Optional[float] = None,
    trades_today: int = 0,
    daily_risk_used: float = 0.0,
    consecutive_losses: int = 0,
    realized_day_pnl: float = 0.0,
) -> TradeRecord:
    side = str(trigger.get("side") or "buy").lower()
    plan = build_plan(
        symbol=symbol,
        entry=float(trigger["entry"]),
        or_high=float(trigger["or_high"]),
        or_low=float(trigger["or_low"]),
        cfg=cfg,
        bars=bars,
        entry_time=trigger.get("entry_time"),
        why=trigger.get("why", ""),
        strategy_steps=trigger.get("strategy_steps"),
        signal_low=trigger.get("signal_low") or trigger.get("bar_low"),
        signal_high=trigger.get("signal_high") or trigger.get("bar_high"),
        side=side,
        planned_target=trigger.get("computed_target") or trigger.get("planned_target"),
        planned_stop=trigger.get("computed_stop"),
    )
    desk = str(cfg.get("desk") or "nifty").lower()
    trade_instrument = str(cfg.get("trade_instrument") or "options").lower()
    steps = list(plan.strategy_steps or [])
    session_date = (
        plan.entry_time.date().isoformat() if plan.entry_time is not None else None
    )
    size_cfg = dict(cfg)

    # Equity cash shares (optional path)
    if trade_instrument in ("equity", "stock", "stocks", "cash"):
        verdict = check_entry(
            plan,
            size_cfg,
            cash=cash,
            trades_today=trades_today,
            daily_risk_used=daily_risk_used,
            consecutive_losses=consecutive_losses,
            realized_day_pnl=realized_day_pnl,
            when=trigger.get("entry_time") or now_ist(),
            skip_session_checks=(mode == Mode.BACKTEST),
        )
        status = TradeStatus.TRIGGERED if verdict.passed else TradeStatus.RISK_BLOCKED
        invested = round(float(plan.entry) * int(verdict.qty or 0), 2)
        steps.append(
            f"Equity leg: BUY {symbol.upper()} x{verdict.qty} @ {plan.entry:.2f} "
            f"· invested ₹{invested:.2f}"
        )
        return TradeRecord(
            mode=mode,
            status=status,
            symbol=symbol.upper(),
            strategy=plan.strategy,
            setup=str(trigger.get("setup") or plan.setup),
            side=plan.side,
            desk=desk,
            why=plan.why,
            strategy_steps=steps,
            how=(
                f"Skill chain: {cfg.get('strategy', 'nifty_bos_v1')} → "
                f"equity entry → exit ({plan.stop_rule} stop, {plan.target_r}R target) → "
                "share size → risk check."
            ),
            or_high=plan.or_high,
            or_low=plan.or_low,
            planned_entry=plan.entry,
            planned_stop=plan.stop,
            planned_target=plan.target,
            stop_rule=plan.stop_rule,
            target_r=plan.target_r,
            risk_per_share=plan.risk_per_share,
            reward_risk=plan.reward_risk,
            qty=verdict.qty,
            risk_verdict=verdict,
            max_loss_planned=verdict.max_loss,
            spot_ref=plan.entry,
            invested_amount=invested,
            entry_time=plan.entry_time,
            session_date=session_date,
            flat_by=plan.flat_by,
            raw={
                "trigger": trigger,
                "plan": plan.model_dump(mode="json"),
                "desk": desk,
                "trade_instrument": "equity",
            },
        )

    # Options (index NIFTY or stock F&O OPTSTK): Long → CE; short → PE
    opt_underlying = symbol.upper() if desk == "stocks" else str(cfg.get("underlying") or "NIFTY").upper()
    instruments = _instruments_df()
    option = pick_option_contract(
        spot=float(plan.entry),
        cfg=cfg,
        side=plan.side,
        as_of=plan.entry_time or now_ist(),
        instruments_df=instruments,
        underlying=opt_underlying,
    )
    # Size with the real NFO lot size (varies per stock)
    lot = int(option.get("lot_size") or cfg.get("option_lot_size") or cfg.get("lot_size") or 65)
    size_cfg["option_lot_size"] = lot
    size_cfg["lot_size"] = lot

    verdict = check_entry(
        plan,
        size_cfg,
        cash=cash,
        trades_today=trades_today,
        daily_risk_used=daily_risk_used,
        consecutive_losses=consecutive_losses,
        realized_day_pnl=realized_day_pnl,
        when=trigger.get("entry_time") or now_ist(),
        skip_session_checks=(mode == Mode.BACKTEST),
    )

    # Stock F&O requires a resolvable NFO token; otherwise block
    if desk == "stocks" and not option.get("token"):
        from src.models.trade import RiskVerdict

        verdict = RiskVerdict(
            passed=False,
            reasons=[f"{opt_underlying} has no tradeable NFO option in instrument master"],
            warnings=list(verdict.warnings or []),
            max_loss=0.0,
            reward_risk=plan.reward_risk,
            qty=0,
            notional=0.0,
        )

    status = TradeStatus.TRIGGERED if verdict.passed else TradeStatus.RISK_BLOCKED

    if mode == Mode.BACKTEST or str(cfg.get("option_premium_mode", "")).lower() == "estimate":
        from src.strategies.option_price import estimate_atm_premium

        premium = estimate_atm_premium(float(option.get("spot_ref") or plan.entry), cfg)
        premium_src = "estimate"
    else:
        premium, premium_src = resolve_option_premium(
            option, cfg, as_of=plan.entry_time or now_ist()
        )
    invested = invested_from_premium(premium, int(verdict.qty or 0))
    option = {
        **option,
        "premium": premium,
        "premium_source": premium_src,
        "invested_amount": invested,
    }
    steps.append(
        f"Options leg: BUY {option['option_type']} strike {option['strike']} "
        f"({option['moneyness']}) · expiry {option['expiry']} · {option['tradingsymbol']} "
        f"· lot {lot}"
    )
    steps.append(
        f"Lot cost: premium ₹{premium:.2f} × qty {verdict.qty} = invested ₹{invested:.2f} "
        f"({premium_src})"
    )

    trade = TradeRecord(
        mode=mode,
        status=status,
        symbol=symbol.upper(),
        strategy=plan.strategy,
        setup=str(trigger.get("setup") or plan.setup),
        side=plan.side,
        desk=desk,
        why=f"{plan.why} → {option['label']} ({option['moneyness']})",
        strategy_steps=steps,
        how=(
            f"Skill chain: {cfg.get('strategy', 'nifty_bos_v1')} → "
            f"{opt_underlying} {'PE' if plan.side == 'sell' else 'CE'} F&O → "
            f"exit ({plan.stop_rule} stop, {plan.target_r}R target) → "
            "lot size → risk check."
        ),
        or_high=plan.or_high,
        or_low=plan.or_low,
        planned_entry=plan.entry,
        planned_stop=plan.stop,
        planned_target=plan.target,
        stop_rule=plan.stop_rule,
        target_r=plan.target_r,
        risk_per_share=plan.risk_per_share,
        reward_risk=plan.reward_risk,
        qty=verdict.qty,
        risk_verdict=verdict,
        max_loss_planned=verdict.max_loss,
        option_type=option["option_type"],
        strike=option["strike"],
        expiry=option["expiry"],
        option_symbol=option["tradingsymbol"],
        moneyness=option["moneyness"],
        spot_ref=option["spot_ref"],
        option_premium=premium,
        invested_amount=invested,
        entry_time=plan.entry_time,
        session_date=session_date,
        flat_by=plan.flat_by,
        raw={
            "trigger": trigger,
            "plan": plan.model_dump(mode="json"),
            "option": option,
            "desk": desk,
            "trade_instrument": "options",
        },
    )
    return trade


def evaluate_symbol_day(
    symbol: str,
    df: pd.DataFrame,
    day: dt.date,
    cfg: Optional[dict] = None,
    mode: Mode = Mode.BACKTEST,
    as_of: Optional[dt.datetime] = None,
    cash: Optional[float] = None,
    bars_15m: Optional[pd.DataFrame] = None,
    prior_df: Optional[pd.DataFrame] = None,
    trades_today: int = 0,
    daily_risk_used: float = 0.0,
    consecutive_losses: int = 0,
    realized_day_pnl: float = 0.0,
) -> Optional[TradeRecord]:
    cfg = cfg or load_trading_config()
    trigger = _find_trigger(
        df, day, cfg, as_of=as_of, bars_15m=bars_15m, prior_df=prior_df
    )
    if trigger.get("status") != "triggered":
        return None
    return build_trade_from_trigger(
        symbol,
        trigger,
        cfg,
        mode=mode,
        bars=df,
        cash=cash,
        trades_today=trades_today,
        daily_risk_used=daily_risk_used,
        consecutive_losses=consecutive_losses,
        realized_day_pnl=realized_day_pnl,
    )


def scan_and_build(
    candle_map: dict[str, pd.DataFrame],
    day: dt.date,
    cfg: Optional[dict] = None,
    mode: Mode = Mode.PAPER,
    as_of: Optional[dt.datetime] = None,
    bars_15m_map: Optional[dict[str, pd.DataFrame]] = None,
    prior_map: Optional[dict[str, pd.DataFrame]] = None,
    trades_today: int = 0,
    daily_risk_used: float = 0.0,
    consecutive_losses: int = 0,
    realized_day_pnl: float = 0.0,
) -> list[TradeRecord]:
    cfg = cfg or load_trading_config()
    hits = _scan_universe(
        candle_map,
        day,
        cfg,
        as_of=as_of,
        bars_15m_map=bars_15m_map,
        prior_map=prior_map,
    )
    write_json(
        "watchlist.json",
        {
            "mode": _active_strategy(cfg),
            "as_of": (as_of or now_ist()).isoformat(),
            "market": "NSE",
            "hits": hits,
        },
    )
    trades: list[TradeRecord] = []
    daily_risk = float(daily_risk_used)
    passed_today = int(trades_today)
    for hit in hits:
        symbol = hit["symbol"]
        trade = build_trade_from_trigger(
            symbol,
            hit,
            cfg,
            mode=mode,
            bars=candle_map.get(symbol),
            trades_today=passed_today,
            daily_risk_used=daily_risk,
            consecutive_losses=consecutive_losses,
            realized_day_pnl=realized_day_pnl,
        )
        if trade.risk_verdict and trade.risk_verdict.passed:
            daily_risk += trade.risk_verdict.max_loss
            passed_today += 1
        trades.append(trade)
        if trade.risk_verdict and trade.risk_verdict.passed:
            write_json("entry.json", trade.raw.get("trigger", {}))
            write_json("plan.json", trade.raw.get("plan", {}))
    return trades
