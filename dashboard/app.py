"""Dashboard API + UI — Nifty Desk: Backtest / Paper / Live separated."""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.angelone.client import get_client
from src.backtest.engine import run_backtest
from src.core.config import (
    list_desks,
    live_armed,
    load_capital_config,
    load_desk_config,
    load_trading_config,
    normalize_desk,
    save_capital_config,
    save_desk_config,
    save_trading_config,
    set_live_arm,
)
from src.core.paths import ensure_dirs
from src.db.store import get_store
from src.live.engine import LiveEngine, tick_all_desks
from src.market.calendar import IST, now_ist
from src.models.trade import Mode, TradeRecord

from src.paper.engine import PaperEngine, trade_desk

ensure_dirs()
app = FastAPI(title="Intraday Desk Dashboard", version="0.4.0")
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


class BacktestRequest(BaseModel):
    days: int = 20
    symbols: Optional[List[str]] = None
    desk: str = "nifty"


class ConfigPatch(BaseModel):
    updates: Dict[str, Any]
    desk: str = "nifty"


class PaperTickRequest(BaseModel):
    desk: str = "nifty"


class LiveTickRequest(BaseModel):
    desk: str = "all"  # nifty | stocks | all


class LiveArmRequest(BaseModel):
    armed: bool = False


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except Exception:
        return None


def period_start_ist(period: str) -> Optional[datetime]:
    """Calendar window in IST: Day = today 00:00, Week/Month = rolling calendar days."""
    now = now_ist()
    p = (period or "all").lower()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if p == "day":
        return today0
    if p == "week":
        return today0 - timedelta(days=6)
    if p == "month":
        return today0 - timedelta(days=29)
    return None


def _as_naive_ist(ts: datetime) -> datetime:
    if getattr(ts, "tzinfo", None) is not None:
        return ts.astimezone(IST).replace(tzinfo=None)
    return ts


def trade_session_dt(t: TradeRecord) -> Optional[datetime]:
    """
    Prefer market session day for performance filters.
    Backtest entry_time is the simulated bar — that is the correct period axis.
    """
    if t.session_date:
        try:
            d = date.fromisoformat(str(t.session_date)[:10])
            return datetime(d.year, d.month, d.day)
        except Exception:
            pass
    if t.entry_time is not None:
        return _as_naive_ist(t.entry_time).replace(hour=0, minute=0, second=0, microsecond=0)
    if t.created_at is not None:
        return _as_naive_ist(t.created_at)
    if t.updated_at is not None:
        return _as_naive_ist(t.updated_at)
    return None


def filter_trades(trades: List[TradeRecord], period: str) -> List[TradeRecord]:
    start = period_start_ist(period)
    if start is None:
        return trades
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start
    out = []
    for t in trades:
        ts = trade_session_dt(t)
        if ts is None:
            continue
        if ts >= start_naive:
            out.append(t)
    return out


def filter_backtests(rows: List[Any], period: str) -> List[Any]:
    """Filter saved backtest runs by when the run started (IST calendar)."""
    start = period_start_ist(period)
    if start is None:
        return rows
    start_naive = start.replace(tzinfo=None) if start.tzinfo else start
    out = []
    for b in rows:
        ts = getattr(b, "started_at", None)
        if ts is None:
            continue
        ts = _as_naive_ist(ts) if isinstance(ts, datetime) else _parse_dt(str(ts))
        if ts is None:
            continue
        if ts.replace(tzinfo=None) >= start_naive:
            out.append(b)
    return out


def filter_desk(trades: List[TradeRecord], desk: Optional[str]) -> List[TradeRecord]:
    if not desk or desk in ("all", "overview"):
        return trades
    key = normalize_desk(desk)
    return [t for t in trades if trade_desk(t) == key]


def invested_open(trades: List[TradeRecord]) -> float:
    return round(
        sum(float(t.invested_amount or 0) for t in trades if t.status.value == "open"),
        2,
    )


def trade_stats(rows: List[TradeRecord]) -> Dict[str, Any]:
    closed = [t for t in rows if t.status.value == "closed"]
    open_ = [t for t in rows if t.status.value == "open"]
    blocked = [t for t in rows if t.status.value == "risk_blocked"]
    triggered = [t for t in rows if t.status.value == "triggered"]
    wins = [t for t in closed if (t.pnl or 0) > 0]
    losses = [t for t in closed if (t.pnl or 0) < 0]
    flats = [t for t in closed if (t.pnl or 0) == 0]
    pnl = sum(t.pnl or 0 for t in closed)
    gross_win = sum(t.pnl or 0 for t in wins)
    gross_loss = abs(sum(t.pnl or 0 for t in losses))
    avg_r = (
        sum(t.pnl_r or 0 for t in closed) / len(closed) if closed else 0.0
    )
    best = max((t.pnl or 0 for t in closed), default=0.0)
    worst = min((t.pnl or 0 for t in closed), default=0.0)
    invested_open = sum(float(t.invested_amount or 0) for t in open_)
    invested_closed = sum(float(t.invested_amount or 0) for t in closed)
    by_exit: Dict[str, int] = {}
    for t in closed:
        key = t.exit_reason.value if t.exit_reason else "none"
        by_exit[key] = by_exit.get(key, 0) + 1
    # equity curve points (closed only, chronological)
    curve = []
    running = 0.0
    for t in sorted(closed, key=lambda x: x.exit_time or x.updated_at or x.created_at):
        running += t.pnl or 0
        curve.append(
            {
                "t": (t.exit_time or t.updated_at or t.created_at).isoformat(),
                "pnl": round(t.pnl or 0, 2),
                "equity": round(running, 2),
                "symbol": t.symbol,
            }
        )
    return {
        "total": len(rows),
        "open": len(open_),
        "closed": len(closed),
        "blocked": len(blocked),
        "triggered": len(triggered),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "pnl": round(pnl, 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "invested_open": round(invested_open, 2),
        "invested_closed": round(invested_closed, 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "avg_r": round(avg_r, 3),
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "profit_factor": round(
            (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
            3,
        ),
        "avg_pnl": round(pnl / len(closed), 2) if closed else 0.0,
        "exit_breakdown": by_exit,
        "equity_curve": curve,
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return (static_dir / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    cfg = load_desk_config("nifty")
    capital = load_capital_config()
    client = get_client()
    angel = {"credentials_present": client.configured()}
    if client.configured():
        try:
            angel = client.health()
        except Exception as e:
            angel["error"] = str(e)
    nifty_ltp = None
    try:
        if client.configured():
            client.login()
            nifty_ltp = client.get_ltp("NIFTY")
    except Exception as e:
        angel["ltp_error"] = str(e)
    paper = PaperEngine(desk="nifty").snapshot()
    angel_cash = None
    if isinstance(angel.get("rms"), dict):
        angel_cash = angel["rms"].get("availablecash")
    return {
        "ok": True,
        "time_ist": now_ist().isoformat(),
        "underlying": cfg.get("underlying", "NIFTY"),
        "paper": cfg.get("paper", True),
        "live_enabled": cfg.get("live_enabled", False),
        "live_armed": live_armed(cfg),
        "budget": capital.get("total_capital") or cfg.get("budget"),
        "paper_cash": paper.get("cash"),
        "angel_available_cash": angel_cash,
        "nifty_ltp": nifty_ltp,
        "angel": angel,
        "desks": list_desks(),
        "watchlist": cfg.get("watchlist", []),
    }


@app.get("/api/desks")
def desks():
    return {"desks": list_desks(), "capital": load_capital_config()}


@app.get("/api/config")
def get_config(desk: str = Query("nifty")):
    return load_desk_config(desk)


@app.post("/api/config")
def patch_config(body: ConfigPatch):
    desk = normalize_desk(body.desk)
    cfg = load_desk_config(desk)
    cfg.update(body.updates)
    save_desk_config(cfg, desk)
    # Keep shared capital in sync when budget patched
    if "budget" in body.updates or "paper_starting_cash" in body.updates:
        capital = load_capital_config()
        if "budget" in body.updates:
            capital["total_capital"] = float(body.updates["budget"])
        if "paper_starting_cash" in body.updates:
            capital["paper_starting_cash"] = float(body.updates["paper_starting_cash"])
        save_capital_config(capital)
    return load_desk_config(desk)


class ResetRequest(BaseModel):
    desk: str = "overview"  # nifty | stocks | overview/all
    # backtest | paper | both — live is never cleared
    mode: str = "paper"


@app.post("/api/reset")
def reset_data(body: ResetRequest = ResetRequest()):
    capital = load_capital_config()
    desk = str(body.desk or "overview").lower()
    mode_raw = str(body.mode or "paper").lower().strip()
    if mode_raw in ("live",):
        raise HTTPException(400, "Live market data cannot be reset.")
    if mode_raw in ("both", "all", "backtest+paper", "paper+backtest"):
        modes = ["backtest", "paper"]
    elif mode_raw in ("backtest", "bt"):
        modes = ["backtest"]
    elif mode_raw in ("paper",):
        modes = ["paper"]
    else:
        raise HTTPException(400, "mode must be backtest, paper, or both (live never allowed)")

    scope = "+".join(modes)
    if desk in ("overview", "all", "both"):
        cash = float(capital.get("paper_starting_cash") or capital.get("total_capital") or 150000)
        result = get_store().reset_all(starting_cash=cash, modes=modes)
        return {
            "ok": True,
            **result,
            "message": (
                f"All desks · cleared {scope} · live kept · "
                f"{result['deleted_trades']} trades · cash ₹{result['cash']:,.0f}"
            ),
        }
    key = normalize_desk(desk)
    result = get_store().reset_desk(key, modes=modes)
    label = "Nifty" if key == "nifty" else "Stocks"
    return {
        "ok": True,
        **result,
        "message": (
            f"{label} · cleared {scope} · live kept · "
            f"{result['deleted_trades']} trades · refund ₹{result['refund']:,.0f} · "
            f"cash ₹{result['cash']:,.0f}"
        ),
    }


@app.get("/api/trades")
def trades(
    mode: Optional[str] = None,
    status: Optional[str] = None,
    period: str = Query("all"),
    desk: str = Query("all"),
    limit: int = 200,
):
    store = get_store()
    m = Mode(mode) if mode else None
    rows = store.list_trades(mode=m, status=status, limit=limit)
    rows = filter_desk(filter_trades(rows, period), desk)
    return [t.model_dump(mode="json") for t in rows]


@app.get("/api/trades/{trade_id}")
def trade_detail(trade_id: str):
    t = get_store().get_trade(trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
    return t.model_dump(mode="json")


@app.get("/api/setups")
def setups(limit: int = 50):
    return get_store().list_setups(limit=limit)


@app.get("/api/events")
def events(
    limit: int = 100,
    kind: Optional[str] = None,
    desk: str = Query("all"),
    mode: str = Query("all"),  # paper | live | all
):
    rows = get_store().list_events(limit=max(limit * 3, 100))
    d = str(desk or "all").lower()
    m = str(mode or "all").lower()

    def _is_stocks(r: dict) -> bool:
        k = str(r.get("kind") or "")
        msg = str(r.get("message") or "")
        return k in ("paper_stocks", "stocks", "live_stocks") or "[stocks]" in msg

    def _is_live(r: dict) -> bool:
        k = str(r.get("kind") or "")
        msg = str(r.get("message") or "")
        return k in ("live", "live_stocks") or k.startswith("live") or "LIVE " in msg or "] LIVE" in msg

    def _is_paper(r: dict) -> bool:
        k = str(r.get("kind") or "")
        msg = str(r.get("message") or "")
        if _is_live(r):
            return False
        return k in ("paper", "paper_stocks") or k.startswith("paper") or "PAPER " in msg or "] PAPER" in msg

    if d == "stocks":
        rows = [r for r in rows if _is_stocks(r)]
    elif d == "nifty":
        rows = [r for r in rows if not _is_stocks(r)]

    if m == "live":
        rows = [r for r in rows if _is_live(r)]
    elif m == "paper":
        rows = [r for r in rows if _is_paper(r)]

    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows[:limit]


@app.get("/api/backtests")
def backtests():
    return [b.model_dump(mode="json") for b in get_store().list_backtests()]


@app.post("/api/backtest/run")
def backtest_run(body: BacktestRequest):
    desk = normalize_desk(body.desk)
    cfg = load_desk_config(desk)
    if body.symbols:
        symbols = [s.upper() for s in body.symbols]
    else:
        symbols = [s.upper() for s in (cfg.get("watchlist") or [])]
        limit = int(cfg.get("backtest_symbol_limit") or 0)
        if desk == "stocks" and limit > 0:
            symbols = symbols[:limit]
        if not symbols:
            symbols = ["NIFTY"] if desk == "nifty" else []
    try:
        # Fresh Angel session — long-running dashboard JWT goes stale
        client = get_client()
        try:
            client.login(force=True)
        except Exception as login_err:
            raise HTTPException(400, f"Angel login failed (needed for candles): {login_err}") from login_err
        summary = run_backtest(symbols=symbols, days=body.days, cfg=cfg, client=client)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    return summary.model_dump(mode="json")


@app.get("/api/paper")
def paper_snapshot(desk: str = Query("nifty")):
    return PaperEngine(desk=desk).snapshot()


@app.post("/api/paper/tick")
def paper_tick(body: PaperTickRequest = PaperTickRequest(), desk: str = Query("nifty")):
    d = normalize_desk(body.desk or desk)
    try:
        return PaperEngine(desk=d).tick()
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/live/status")
def live_status(desk: str = Query("all")):
    d = str(desk or "all").lower()
    if d in ("all", "overview", "both"):
        nifty = LiveEngine(desk="nifty").status()
        stocks = LiveEngine(desk="stocks").status()
        cash = nifty.get("available_cash")
        if cash is None:
            cash = stocks.get("available_cash")
        return {
            "armed": live_armed(),
            "available_cash": cash,
            "nifty": nifty,
            "stocks": stocks,
            "message": (
                "LIVE ARMED — auto NFO orders ON (both desks)"
                if live_armed()
                else "Live OFF — paper only. Arm via Live panel or run_live.py --arm"
            ),
        }
    return LiveEngine(desk=normalize_desk(d)).status()


@app.post("/api/live/tick")
def live_tick(body: LiveTickRequest = LiveTickRequest()):
    d = str(body.desk or "all").lower()
    try:
        if d in ("all", "overview", "both"):
            return tick_all_desks()
        return LiveEngine(desk=normalize_desk(d)).tick()
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/live/arm")
def live_arm(body: LiveArmRequest):
    """Kill switch: armed=true → paper=false + live_enabled=true on capital + desks."""
    try:
        return set_live_arm(bool(body.armed))
    except Exception as e:
        raise HTTPException(400, str(e)) from e


def _config_public(cfg: dict) -> Dict[str, Any]:
    budget = float(cfg.get("budget") or cfg.get("paper_starting_cash") or 0)
    risk_pct = float(cfg.get("risk_pct_of_capital") or 0)
    profit_pct = float(cfg.get("profit_pct_of_capital") or 0)
    max_lots = float(cfg.get("max_lots") or 1)
    loss_lot = float(cfg.get("loss_per_lot_inr") or 0)
    profit_lot = float(cfg.get("profit_per_lot_inr") or 0)
    target_r = float(cfg.get("default_target_r") or cfg.get("min_reward_risk") or 0)
    max_loss = float(cfg.get("max_loss_per_trade_inr") or 0)
    if max_loss <= 0 and risk_pct > 0 and budget > 0:
        max_loss = budget * risk_pct / 100.0
    if max_loss <= 0 and loss_lot > 0:
        max_loss = loss_lot * max_lots
    max_profit = budget * profit_pct / 100.0 if profit_pct > 0 and budget > 0 else 0.0
    if max_profit <= 0 and profit_lot > 0:
        max_profit = profit_lot * max_lots
    if max_profit <= 0 and max_loss > 0 and target_r > 0:
        max_profit = max_loss * target_r
    trail_activate_r = float(cfg.get("trail_activate_r") or 0)
    trail_step_r = float(cfg.get("trail_step_r") or 0)
    trail_activate_inr = max_loss * trail_activate_r if max_loss and trail_activate_r else 0.0
    trail_step_inr = max_loss * trail_step_r if max_loss and trail_step_r else 0.0
    return {
        "desk": cfg.get("desk", "nifty"),
        "desk_label": cfg.get("desk_label"),
        "strategy": cfg.get("strategy", "nifty_bos_v1"),
        "budget": cfg.get("budget"),
        "paper_starting_cash": cfg.get("paper_starting_cash"),
        "risk_pct_of_capital": cfg.get("risk_pct_of_capital"),
        "profit_pct_of_capital": cfg.get("profit_pct_of_capital"),
        "max_loss_per_trade_inr": round(max_loss, 2) if max_loss else cfg.get("max_loss_per_trade_inr"),
        "max_profit_per_trade_inr": round(max_profit, 2),
        "max_daily_loss_inr": cfg.get("max_daily_loss_inr"),
        "max_daily_loss_pct": cfg.get("max_daily_loss_pct"),
        "max_trades_per_day": cfg.get("max_trades_per_day"),
        "max_consecutive_losses": cfg.get("max_consecutive_losses"),
        "min_reward_risk": cfg.get("min_reward_risk"),
        "default_target_r": cfg.get("default_target_r"),
        "default_stop": cfg.get("default_stop"),
        "loss_per_lot_inr": cfg.get("loss_per_lot_inr"),
        "profit_per_lot_inr": cfg.get("profit_per_lot_inr"),
        "trail_enabled": cfg.get("trail_enabled", True),
        "trail_activate_r": trail_activate_r or cfg.get("trail_activate_r"),
        "trail_step_r": trail_step_r or cfg.get("trail_step_r"),
        "trail_activate_inr": round(trail_activate_inr, 2),
        "trail_step_inr": round(trail_step_inr, 2),
        "paper": cfg.get("paper"),
        "live_enabled": cfg.get("live_enabled"),
        "watchlist": cfg.get("watchlist"),
        "watchlist_n": len(cfg.get("watchlist") or []),
        "scan_batch_size": cfg.get("scan_batch_size"),
        "scan_poll_seconds": cfg.get("scan_poll_seconds"),
        "session_open": cfg.get("session_open"),
        "session_close": cfg.get("session_close"),
        "flat_by": cfg.get("flat_by"),
        "instrument_type": cfg.get("instrument_type"),
        "trade_instrument": cfg.get("trade_instrument"),
        "option_lot_size": cfg.get("option_lot_size", 65),
        "lot_size": cfg.get("lot_size", 65),
        "option_strike_mode": cfg.get("option_strike_mode", "atm"),
        "point_value": cfg.get("point_value"),
        "backtest_default_days": cfg.get("backtest_default_days", 30),
        "backtest_symbol_limit": cfg.get("backtest_symbol_limit"),
        "plan": {
            "cash_budget": budget,
            "max_risk_inr": round(max_loss, 2),
            "max_profit_inr": round(max_profit, 2),
            "risk_pct": risk_pct or None,
            "profit_pct": profit_pct or None,
            "trail_activate_inr": round(trail_activate_inr, 2),
            "trail_step_inr": round(trail_step_inr, 2),
            "trail_activate_r": trail_activate_r,
            "trail_step_r": trail_step_r,
            "loss_per_lot_inr": loss_lot or None,
            "profit_per_lot_inr": profit_lot or None,
        },
    }


def _desk_bundle(store, period: str, desk: str) -> Dict[str, Any]:
    cfg = load_desk_config(desk)
    paper_rows = filter_desk(
        filter_trades(store.list_trades(mode=Mode.PAPER, limit=1000), period), desk
    )
    back_rows = filter_desk(
        filter_trades(store.list_trades(mode=Mode.BACKTEST, limit=1000), period), desk
    )
    live_rows = filter_desk(
        filter_trades(store.list_trades(mode=Mode.LIVE, limit=1000), period), desk
    )
    paper_acct = PaperEngine(desk=desk).snapshot()
    bt_stats = trade_stats(back_rows)
    paper_stats = trade_stats(paper_rows)
    live_stats = trade_stats(live_rows)
    return {
        "desk": desk,
        "config": _config_public(cfg),
        "backtest": bt_stats,
        "paper": paper_stats,
        "live": live_stats,
        "paper_account": paper_acct,
        "invested_open": invested_open(paper_rows),
        "trades": {
            "backtest": {
                "open": [t.model_dump(mode="json") for t in back_rows if t.status.value == "open"][:50],
                "closed": [t.model_dump(mode="json") for t in back_rows if t.status.value == "closed"][:100],
                "all": [t.model_dump(mode="json") for t in back_rows[:100]],
            },
            "paper": {
                "open": [t.model_dump(mode="json") for t in paper_rows if t.status.value == "open"][:50],
                "closed": [t.model_dump(mode="json") for t in paper_rows if t.status.value == "closed"][:100],
                "all": [t.model_dump(mode="json") for t in paper_rows[:100]],
            },
            "live": {
                "open": [t.model_dump(mode="json") for t in live_rows if t.status.value == "open"][:50],
                "closed": [
                    t.model_dump(mode="json")
                    for t in live_rows
                    if t.status.value in ("closed", "cancelled")
                    or (
                        t.status.value == "risk_blocked"
                        and isinstance(t.raw, dict)
                        and (t.raw.get("live") or {}).get("order_attempted")
                    )
                ][:100],
                "all": [t.model_dump(mode="json") for t in live_rows[:100]],
            },
        },
    }


def _fetch_angel_rms() -> dict:
    """Single RMS fetch for dashboard (client caches ~20s)."""
    out: dict[str, Any] = {
        "available_cash": None,
        "rms": {},
        "rms_error": None,
    }
    try:
        client = get_client()
        if not client.configured():
            out["rms_error"] = "Angel credentials missing in .env"
            return out
        rms = client.get_rms()
        out["rms"] = {
            "availablecash": rms.get("availablecash"),
            "net": rms.get("net"),
            "collateral": rms.get("collateral"),
            "availablelimitmargin": rms.get("availablelimitmargin"),
            "utilisedoptionpremium": rms.get("utilisedoptionpremium"),
            "m2munrealized": rms.get("m2munrealized"),
        }
        cash = rms.get("availablecash")
        if cash is None:
            cash = rms.get("net")
        out["available_cash"] = float(cash) if cash is not None else None
    except Exception as e:
        out["rms_error"] = str(e)
    return out


def _angel_live_status(desk: str = "all") -> dict:
    d = str(desk or "all").lower()
    rms_info = _fetch_angel_rms()
    cash = rms_info.get("available_cash")

    def _desk_status(name: str) -> dict:
        st = LiveEngine(desk=name).status()
        # Prefer shared RMS so we don't fail the 2nd desk on rate-limit
        if cash is not None:
            st["available_cash"] = cash
            st["rms"] = rms_info.get("rms") or st.get("rms")
            st["rms_error"] = None
        elif rms_info.get("rms_error") and not st.get("available_cash"):
            st["rms_error"] = rms_info.get("rms_error")
        return st

    if d in ("nifty", "stocks"):
        st = _desk_status(d)
        st["rms"] = rms_info.get("rms") or st.get("rms")
        if cash is not None:
            st["available_cash"] = cash
        return st

    nifty = _desk_status("nifty")
    stocks = _desk_status("stocks")
    return {
        "armed": live_armed(),
        "available_cash": cash,
        "live_enabled": nifty.get("live_enabled"),
        "paper_flag": nifty.get("paper_flag"),
        "nifty": nifty,
        "stocks": stocks,
        "rms": rms_info.get("rms") or {},
        "rms_error": rms_info.get("rms_error"),
        "message": (
            "LIVE ARMED — auto NFO orders ON"
            if live_armed()
            else "Live OFF — paper only. Arm via Live panel or run_live.py --arm"
        ),
    }


@app.get("/api/summary")
def summary(period: str = Query("all"), desk: str = Query("overview")):
    store = get_store()
    capital_cfg = load_capital_config()
    capital = float(capital_cfg.get("total_capital") or capital_cfg.get("paper_starting_cash") or 150000)
    start = period_start_ist(period)
    desk_key = str(desk or "overview").lower()
    live = _angel_live_status("all" if desk_key == "overview" else desk_key)
    recent = filter_backtests(store.list_backtests(25), period)[:10]

    nifty = _desk_bundle(store, period, "nifty")
    stocks = _desk_bundle(store, period, "stocks")
    paper_cash = float(nifty["paper_account"].get("cash") or capital)
    nifty_inv = float(nifty.get("invested_open") or 0)
    stocks_inv = float(stocks.get("invested_open") or 0)
    free_cash = round(paper_cash, 2)
    angel_cash = live.get("available_cash")
    live_balance = float(angel_cash) if angel_cash is not None else None

    desk_key = str(desk or "overview").lower()
    if desk_key in ("nifty", "stocks"):
        active = nifty if desk_key == "nifty" else stocks
        total_profit = round(
            float(active["backtest"]["pnl"])
            + float(active["paper"]["pnl"])
            + float(active["live"]["pnl"]),
            2,
        )
        return {
            "time_ist": now_ist().isoformat(),
            "period": period,
            "period_start": start.isoformat() if start else None,
            "period_axis": "session_date",
            "view": desk_key,
            "desk": desk_key,
            "underlying": active["config"].get("underlying") or desk_key,
            "account": {
                "total_capital": round(capital, 2),
                "total_profit": total_profit,
                "total_balance": free_cash,
                "paper_cash": free_cash,
                "free_cash": free_cash,
                "angel_available_cash": live_balance,
                "live_cash": live_balance,
                "nifty_invested": nifty_inv,
                "stocks_invested": stocks_inv,
                "backtest_balance": round(capital + float(active["backtest"]["pnl"]), 2),
                "backtest_profit": float(active["backtest"]["pnl"]),
                "paper_profit": float(active["paper"]["pnl"]),
                "live_profit": float(active["live"]["pnl"]),
                "gross_profit": round(
                    float(active["backtest"]["gross_profit"])
                    + float(active["paper"]["gross_profit"])
                    + float(active["live"]["gross_profit"]),
                    2,
                ),
            },
            "config": active["config"],
            "backtest": active["backtest"],
            "paper": active["paper"],
            "live": active["live"],
            "paper_account": active["paper_account"],
            "live_status": live,
            "recent_backtests": [b.model_dump(mode="json") for b in recent],
            "trades": active["trades"],
            "desks": {"nifty": nifty, "stocks": stocks},
        }

    # Overview — both desks + shared capital split
    paper_profit = round(float(nifty["paper"]["pnl"]) + float(stocks["paper"]["pnl"]), 2)
    bt_profit = round(float(nifty["backtest"]["pnl"]) + float(stocks["backtest"]["pnl"]), 2)
    live_profit = round(float(nifty["live"]["pnl"]) + float(stocks["live"]["pnl"]), 2)
    return {
        "time_ist": now_ist().isoformat(),
        "period": period,
        "period_start": start.isoformat() if start else None,
        "period_axis": "session_date",
        "view": "overview",
        "desk": "overview",
        "underlying": "ALL",
        "account": {
            "total_capital": round(capital, 2),
            "total_profit": round(paper_profit + bt_profit + live_profit, 2),
            "total_balance": free_cash,
            "paper_cash": free_cash,
            "free_cash": free_cash,
            "angel_available_cash": live_balance,
            "live_cash": live_balance,
            "nifty_invested": nifty_inv,
            "stocks_invested": stocks_inv,
            "backtest_balance": round(capital + bt_profit, 2),
            "backtest_profit": bt_profit,
            "paper_profit": paper_profit,
            "live_profit": live_profit,
            "gross_profit": round(
                float(nifty["backtest"]["gross_profit"])
                + float(stocks["backtest"]["gross_profit"])
                + float(nifty["paper"]["gross_profit"])
                + float(stocks["paper"]["gross_profit"]),
                2,
            ),
        },
        "config": {
            "strategy": "multi-desk",
            "budget": capital,
            "paper_starting_cash": capital_cfg.get("paper_starting_cash"),
            "desk_label": "Overview",
            "trade_instrument": "mixed",
            "lot_size": "—",
            "option_lot_size": 65,
            "backtest_default_days": 30,
            "watchlist_n": (nifty["config"].get("watchlist_n") or 0)
            + (stocks["config"].get("watchlist_n") or 0),
        },
        "backtest": trade_stats(
            filter_trades(store.list_trades(mode=Mode.BACKTEST, limit=1000), period)
        ),
        "paper": trade_stats(
            filter_trades(store.list_trades(mode=Mode.PAPER, limit=1000), period)
        ),
        "live": trade_stats(
            filter_trades(store.list_trades(mode=Mode.LIVE, limit=1000), period)
        ),
        "paper_account": {
            "cash": free_cash,
            "nifty": nifty["paper_account"],
            "stocks": stocks["paper_account"],
        },
        "live_status": live,
        "recent_backtests": [b.model_dump(mode="json") for b in recent],
        "trades": {
            "backtest": {"open": [], "closed": [], "all": []},
            "paper": {"open": [], "closed": [], "all": []},
            "live": {"open": [], "closed": [], "all": []},
        },
        "desks": {"nifty": nifty, "stocks": stocks},
    }
