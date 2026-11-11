"""Risk manager — capital % risk (5–7%), profit (10–12%), streak + daily caps."""

from __future__ import annotations

from typing import Optional

from src.market.calendar import in_entry_window, is_market_open, now_ist
from src.models.trade import RiskVerdict, TradePlan
from src.strategies.exit_plan import capital_rr, loss_per_lot_inr


def is_index_mode(cfg: dict, symbol: str = "") -> bool:
    if str(cfg.get("instrument_type", "")).lower() == "index":
        return True
    return symbol.upper() in {"NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY"}


def daily_loss_limit_inr(cfg: dict, cash: Optional[float] = None) -> float:
    """Daily loss cap from starting budget (not shrinking equity — avoids blocking after wins/losses)."""
    budget = float(cfg.get("budget", 25000))
    pct = float(cfg.get("max_daily_loss_pct", 10.0))
    from_pct = budget * pct / 100.0
    explicit = cfg.get("max_daily_loss_inr")
    if explicit is not None and float(explicit) > 0:
        return min(float(explicit), from_pct) if from_pct > 0 else float(explicit)
    return from_pct


def risk_budget_inr(cfg: dict) -> float:
    """Per-trade risk budget from loss_per_lot × max_lots, capped by max_loss_per_trade."""
    per_lot = loss_per_lot_inr(cfg)
    max_lots = max(1, int(cfg.get("max_lots", 1)))
    ladder_budget = per_lot * max_lots
    explicit = cfg.get("max_loss_per_trade_inr")
    if explicit is not None and float(explicit) > 0:
        return min(float(explicit), ladder_budget)
    return ladder_budget


def size_position(plan: TradePlan, cfg: dict, cash: Optional[float] = None) -> RiskVerdict:
    budget = float(cfg.get("budget", 25000))
    cash = budget if cash is None else float(cash)
    risk_pct, profit_pct, _ = capital_rr(cfg)
    max_loss = risk_budget_inr(cfg)
    min_rr = float(cfg.get("min_reward_risk", 2.0))
    point_value = float(cfg.get("point_value", 1))
    if str(cfg.get("trade_instrument", "")).lower() == "options":
        lot_size = max(1, int(cfg.get("option_lot_size") or cfg.get("lot_size") or 65))
    else:
        lot_size = max(1, int(cfg.get("lot_size") or cfg.get("option_lot_size") or 65))
    max_lots = max(1, int(cfg.get("max_lots", 1)))
    min_lots = max(1, int(cfg.get("min_lots", 1)))
    index_mode = is_index_mode(cfg, plan.symbol)

    reasons: list[str] = []
    warnings: list[str] = []

    if plan.symbol.upper() in [b.upper() for b in cfg.get("blocked", [])]:
        return RiskVerdict(passed=False, reasons=[f"{plan.symbol} is blocked"], qty=0)

    if plan.reward_risk < min_rr - 1e-9:
        reasons.append(f"reward:risk {plan.reward_risk:.2f} under minimum {min_rr}")

    risk_per_unit = plan.risk_per_share * point_value
    if risk_per_unit <= 0:
        return RiskVerdict(passed=False, reasons=["risk per unit must be > 0"], qty=0)

    raw_units = int(max_loss // risk_per_unit)
    lots = raw_units // lot_size
    lots = max(0, min(lots, max_lots))
    while lots < max_lots and (lots + 1) * lot_size * risk_per_unit <= max_loss * 1.05:
        lots += 1

    min_lot_risk = lot_size * min_lots * risk_per_unit
    one_lot_risk = lot_size * risk_per_unit
    slack = float(cfg.get("wide_stop_lot_slack", 2.5))

    # Always respect min_lots: bump up when affordable, else soft-allow or block.
    if lots < min_lots:
        if min_lot_risk <= max_loss * 1.05:
            lots = min_lots
        elif min_lot_risk <= max_loss * slack:
            lots = min_lots
            warnings.append(
                f"wide stop: {min_lots} lot structural ₹{min_lot_risk:.0f} > budget ₹{max_loss:.0f} "
                f"(≤{slack}×) — {min_lots} lots forced, risk capped to budget for daily limit"
            )
        else:
            reasons.append(
                f"cannot fit min_lots={min_lots} ({lot_size * min_lots} units): "
                f"need ₹{min_lot_risk:.0f} risk vs budget ₹{max_loss:.0f} "
                f"(₹{risk_per_unit:.2f}/unit · raise max_loss_per_trade_inr or tighten stop)"
            )
            lots = 0

    if lots <= 0 and not reasons:
        if min_lot_risk <= max_loss * slack:
            lots = min_lots
            warnings.append(
                f"wide stop: structural ₹{min_lot_risk:.0f} > budget ₹{max_loss:.0f} "
                f"(≤{slack}×) — {min_lots} lots forced, risk capped to budget"
            )
        else:
            reasons.append(
                f"cannot fit min_lots={min_lots} ({lot_size * min_lots} units): "
                f"max risk ₹{max_loss:.0f} / ₹{risk_per_unit:.2f} per unit = {raw_units} units"
            )

    qty = lots * lot_size

    planned_loss = qty * risk_per_unit
    # Never report planned loss above per-trade budget (keeps daily gate consistent)
    if qty and planned_loss > max_loss:
        planned_loss = float(max_loss)
    planned_profit = qty * abs(plan.target - plan.entry) * point_value
    notional = qty * plan.entry * point_value

    band_max = max(
        budget * float(cfg.get("risk_pct_max", 7)) / 100.0,
        float(max_loss),
    )
    # Shrink lots for risk band, but never below min_lots.
    if qty and planned_loss > band_max + 1.0:
        while lots > min_lots and lots * lot_size * risk_per_unit > band_max:
            lots -= 1
        qty = lots * lot_size
        planned_loss = qty * risk_per_unit
        if planned_loss > max_loss:
            planned_loss = float(max_loss)
        planned_profit = qty * abs(plan.target - plan.entry) * point_value
        notional = qty * plan.entry * point_value
        if lots * lot_size * risk_per_unit > band_max * slack:
            reasons.append(
                f"min_lots={min_lots} planned loss exceeds risk band ₹{band_max:.0f}"
            )
            qty = 0
            lots = 0
            planned_loss = 0.0
            planned_profit = 0.0
            notional = 0.0
        elif qty <= 0:
            reasons.append(f"planned loss would exceed risk budget ₹{band_max:.0f}")

    profit_band_min = budget * float(cfg.get("profit_pct_min", 10)) / 100.0
    profit_band_max = budget * float(cfg.get("profit_pct_max", 12)) / 100.0
    if qty and planned_profit + 1 < profit_band_min:
        warnings.append(
            f"target profit ₹{planned_profit:.0f} under {cfg.get('profit_pct_min', 10)}% band "
            f"(₹{profit_band_min:.0f}) — tight structure stop; still allowed if RR ok"
        )
    elif qty and planned_profit > profit_band_max * 1.25:
        warnings.append(
            f"target profit ₹{planned_profit:.0f} above {cfg.get('profit_pct_max', 12)}% band"
        )

    per = loss_per_lot_inr(cfg)
    warnings.append(
        f"Size: {lots} lots × {lot_size} = {qty} · "
        f"risk ₹{planned_loss:.0f} (≈₹{per:.0f}/lot · {risk_pct}% capital) · "
        f"target ₹{planned_profit:.0f} ({profit_pct}% band)"
    )
    if index_mode:
        warnings.append("Nifty index mode: PnL = points × units × point_value")

    options_mode = str(cfg.get("trade_instrument", "")).lower() == "options"
    # Equity cash: enforce share notional vs cash. Options use premium at fill time.
    if not index_mode and not options_mode:
        usable = cash * (1.0 - float(cfg.get("cash_buffer_pct", 10)) / 100.0)
        if notional > usable:
            reasons.append(f"notional ₹{notional:.0f} over usable cash ₹{usable:.0f}")

    if reasons:
        return RiskVerdict(
            passed=False,
            reasons=reasons,
            warnings=warnings,
            max_loss=round(planned_loss, 2) if qty else 0.0,
            reward_risk=plan.reward_risk,
            qty=0,
            notional=0.0,
        )

    return RiskVerdict(
        passed=True,
        reasons=[],
        warnings=warnings,
        max_loss=round(planned_loss, 2),
        reward_risk=plan.reward_risk,
        qty=qty,
        notional=round(notional, 2),
    )


def check_entry(
    plan: TradePlan,
    cfg: dict,
    cash: Optional[float] = None,
    trades_today: int = 0,
    daily_risk_used: float = 0.0,
    consecutive_losses: int = 0,
    realized_day_pnl: float = 0.0,
    when=None,
    skip_session_checks: bool = False,
) -> RiskVerdict:
    when = now_ist(when)
    verdict = size_position(plan, cfg, cash=cash)
    reasons = list(verdict.reasons)
    warnings = list(verdict.warnings)

    if cfg.get("require_stop", True):
        if plan.side == "sell":
            if plan.stop <= plan.entry:
                reasons.append("short stop required and must be above entry")
        elif plan.stop >= plan.entry:
            reasons.append("stop required and must be below entry")

    if not skip_session_checks:
        if not is_market_open(cfg, when):
            reasons.append("market is closed")
        elif not in_entry_window(cfg, when):
            reasons.append(
                f"outside entry window "
                f"{cfg.get('no_entries_before')}-{cfg.get('no_entries_after')} IST"
            )

    max_trades = int(cfg.get("max_trades_per_day", 3))
    if trades_today >= max_trades:
        reasons.append(f"already {trades_today} trades today (max {max_trades})")

    max_streak = int(cfg.get("max_consecutive_losses", 2))
    if consecutive_losses >= max_streak:
        reasons.append(
            f"revenge/streak block: {consecutive_losses} consecutive losses "
            f"(max {max_streak}) — no new entries today"
        )

    max_daily = daily_loss_limit_inr(cfg, cash=cash)
    # Realized day loss hits first
    if realized_day_pnl < 0 and abs(realized_day_pnl) >= max_daily - 1e-6:
        reasons.append(
            f"daily loss limit hit: realized ₹{realized_day_pnl:.0f} ≥ max ₹{max_daily:.0f} "
            f"({cfg.get('max_daily_loss_pct', 10)}% capital)"
        )
    projected = daily_risk_used + verdict.max_loss
    if projected > max_daily + 1.0:
        reasons.append(
            f"daily loss budget: used ₹{daily_risk_used:.0f} + this ₹{verdict.max_loss:.0f} "
            f"> max ₹{max_daily:.0f}"
        )

    if not cfg.get("paper", True) and not cfg.get("live_enabled", False):
        reasons.append("live_enabled is false — only paper/backtest allowed")

    passed = verdict.passed and not reasons
    return RiskVerdict(
        passed=passed,
        reasons=reasons if not passed else [],
        warnings=warnings,
        max_loss=verdict.max_loss,
        reward_risk=verdict.reward_risk,
        qty=verdict.qty if passed else 0,
        notional=verdict.notional if passed else 0.0,
    )
