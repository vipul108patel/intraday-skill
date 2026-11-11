"""Paper trading: live Angel One data + skill pipeline, no real orders."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from src.angelone.client import AngelOneClient, get_client
from src.core.config import load_desk_config, load_trading_config, normalize_desk
from src.db.store import get_store
from src.market.calendar import IST, in_entry_window, is_market_open, now_ist, parse_hhmm
from src.market.context import load_universe_context
from src.models.trade import Mode, TradeRecord, TradeStatus
from src.strategies.pipeline import scan_and_build
from src.strategies.risk import daily_loss_limit_inr

logger = logging.getLogger(__name__)


def trade_desk(trade: TradeRecord) -> str:
    raw_desk = (trade.raw or {}).get("desk") if isinstance(trade.raw, dict) else None
    return normalize_desk(trade.desk or raw_desk or "nifty")


class PaperEngine:
    def __init__(
        self,
        cfg: Optional[dict] = None,
        client: Optional[AngelOneClient] = None,
        desk: Optional[str] = None,
    ):
        if cfg is None:
            self.cfg = load_desk_config(desk or "nifty")
        else:
            self.cfg = cfg
            if desk:
                self.cfg["desk"] = normalize_desk(desk)
        self.desk = normalize_desk(self.cfg.get("desk") or desk or "nifty")
        self.cfg["desk"] = self.desk
        self.client = client or get_client()
        self.store = get_store()
        state = self.store.get_paper_state()
        start_cash = float(self.cfg.get("paper_starting_cash", self.cfg.get("budget", 100000)))
        # Shared cash pool across nifty + stocks desks
        self.cash = float(state["cash"] if state.get("cash") is not None else start_cash)
        self.positions: dict[str, TradeRecord] = {}
        for t in self.store.list_trades(mode=Mode.PAPER, status="open", limit=200):
            if trade_desk(t) != self.desk:
                continue
            self.positions[t.symbol] = t
        self._persist()

    def _event_kind(self) -> str:
        return "paper_stocks" if self.desk == "stocks" else "paper"

    def _persist(self) -> None:
        state = self.store.get_paper_state()
        payload = {k: v for k, v in state.items() if k != "cash"}
        all_open = self.store.list_trades(mode=Mode.PAPER, status="open", limit=200)
        by_desk: dict[str, dict[str, str]] = {"nifty": {}, "stocks": {}}
        for t in all_open:
            by_desk.setdefault(trade_desk(t), {})[t.symbol] = t.id
        by_desk[self.desk] = {k: v.id for k, v in self.positions.items()}
        payload["positions_by_desk"] = by_desk
        payload["positions"] = {
            **by_desk.get("nifty", {}),
            **by_desk.get("stocks", {}),
        }
        payload["realized_pnl"] = sum(
            t.pnl or 0
            for t in self.store.list_trades(mode=Mode.PAPER, status="closed", limit=500)
        )
        self.store.set_paper_state(self.cash, payload)

    def _next_watch_batch(self) -> list[str]:
        """Return watchlist batch. batch_size 0 or >= len(watchlist) = full scan every tick."""
        symbols = [s.upper() for s in self.cfg.get("watchlist", [])]
        if not symbols:
            return []
        raw = self.cfg.get("scan_batch_size")
        if raw in (None, "", 0, "0"):
            return symbols
        batch = max(1, int(raw))
        if batch >= len(symbols):
            return symbols
        state = self.store.get_paper_state()
        key = f"scan_cursor_{self.desk}"
        cursor = int(state.get(key) or 0) % len(symbols)
        out = [symbols[(cursor + i) % len(symbols)] for i in range(min(batch, len(symbols)))]
        state[key] = (cursor + batch) % len(symbols)
        cash = float(state.get("cash") if state.get("cash") is not None else self.cash)
        payload = {k: v for k, v in state.items() if k != "cash"}
        self.store.set_paper_state(cash, payload)
        return out

    def _session_stats(self) -> tuple[int, float, int, float]:
        """Return (trades_today, planned_risk, consecutive_losses, realized_day_pnl).

        Per-desk trade counts / streak; shared daily realized PnL for account loss limit.
        """
        today = now_ist().date().isoformat()
        trades = self.store.list_trades(mode=Mode.PAPER, limit=400)
        n = 0
        risk = 0.0
        realized_shared = 0.0
        closed_ordered: list[TradeRecord] = []
        for t in trades:
            if t.session_date != today:
                continue
            # Daily loss uses whole account
            if t.status == TradeStatus.CLOSED:
                realized_shared += float(t.pnl or 0)
            if trade_desk(t) != self.desk:
                continue
            if t.status in (TradeStatus.OPEN, TradeStatus.CLOSED, TradeStatus.TRIGGERED):
                if t.risk_verdict and t.risk_verdict.passed:
                    n += 1
                    risk += t.max_loss_planned or 0
            if t.status == TradeStatus.CLOSED:
                closed_ordered.append(t)

        closed_ordered.sort(
            key=lambda t: t.exit_time or t.entry_time or t.session_date or "",
        )
        streak = 0
        for t in reversed(closed_ordered):
            if (t.pnl or 0) < 0:
                streak += 1
            else:
                break
        return n, risk, streak, realized_shared

    def scan_once(self) -> list[TradeRecord]:
        when = now_ist()
        if not is_market_open(self.cfg, when):
            from src.market.calendar import holiday_name, is_holiday, is_trading_day

            day = when.date()
            if is_holiday(day):
                why = f"NSE holiday — {holiday_name(day)} ({day.isoformat()})"
            elif not is_trading_day(day):
                why = f"Weekend — {day.isoformat()} (no NSE session)"
            else:
                why = (
                    f"Outside session hours "
                    f"({self.cfg.get('session_open', '09:15')}–{self.cfg.get('session_close', '15:30')} IST)"
                )
            self.store.add_event(self._event_kind(), f"[{self.desk}] Market closed — scan skipped · {why}")
            return []
        if not in_entry_window(self.cfg, when):
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] Outside entry window — scan skipped · "
                f"{self.cfg.get('no_entries_before', '09:30')}–{self.cfg.get('no_entries_after', '15:00')} IST",
            )
            return []

        symbols = self._next_watch_batch()
        day = when.date()
        trades_today, daily_risk, streak, realized = self._session_stats()

        max_streak = int(self.cfg.get("max_consecutive_losses", 2))
        if streak >= max_streak:
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] Streak/revenge block — {streak} consecutive losses (max {max_streak})",
            )
            return []

        day_cap = daily_loss_limit_inr(self.cfg, cash=self.cash)
        if realized < 0 and abs(realized) >= day_cap - 1e-6:
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] Daily loss limit hit — realized ₹{realized:.0f} ≥ ₹{day_cap:.0f}",
            )
            return []

        try:
            candle_map, bars_15m_map, prior_map = load_universe_context(
                self.client, symbols, day, self.cfg
            )
        except Exception as e:
            logger.warning("Paper context load failed (%s): %s", self.desk, e)
            candle_map, bars_15m_map, prior_map = {}, {}, {}
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] Candle fetch failed — {e}",
            )

        trades = scan_and_build(
            candle_map,
            day,
            cfg=self.cfg,
            mode=Mode.PAPER,
            as_of=when,
            bars_15m_map=bars_15m_map,
            prior_map=prior_map,
            trades_today=trades_today,
            daily_risk_used=daily_risk,
            consecutive_losses=streak,
            realized_day_pnl=realized,
        )
        opened: list[TradeRecord] = []

        for trade in trades:
            self.store.save_setup(
                {
                    "as_of": when.isoformat(),
                    "symbol": trade.symbol,
                    "strategy": trade.strategy,
                    "status": trade.status.value,
                    "why": trade.why,
                    "planned_entry": trade.planned_entry,
                    "planned_stop": trade.planned_stop,
                    "planned_target": trade.planned_target,
                    "reward_risk": trade.reward_risk,
                }
            )
            if trade.symbol in self.positions:
                continue
            if not trade.risk_verdict or not trade.risk_verdict.passed:
                self.store.upsert_trade(trade)
                continue
            if trades_today >= int(self.cfg.get("max_trades_per_day", 3)):
                trade.status = TradeStatus.RISK_BLOCKED
                self.store.upsert_trade(trade)
                continue

            # Paper fill at planned entry + slippage; refresh option premium for invested ₹
            slip = float(self.cfg.get("paper_slippage_bps", 2)) / 10000.0
            try:
                ltp = self.client.get_ltp(trade.symbol)
            except Exception:
                ltp = float(trade.planned_entry)
            fill = max(ltp, float(trade.planned_entry)) * (1 + slip)

            # Live CE/PE LTP → invested = premium × qty (e.g. ₹207 × 65 = ₹13,455)
            opt = (trade.raw or {}).get("option") or {}
            token = opt.get("token")
            tsym = trade.option_symbol or opt.get("tradingsymbol")
            if token and tsym:
                try:
                    prem = float(
                        self.client.get_ltp_by_token(
                            str(opt.get("exchange") or "NFO"), str(tsym), str(token)
                        )
                    )
                    if prem > 0:
                        trade.option_premium = prem
                        trade.invested_amount = round(prem * int(trade.qty or 0), 2)
                        opt = {**opt, "premium": prem, "premium_source": "live_ltp",
                               "invested_amount": trade.invested_amount}
                        trade.raw["option"] = opt
                except Exception as e:
                    logger.debug("Paper option LTP: %s", e)
            if not trade.invested_amount and trade.option_premium and trade.qty:
                trade.invested_amount = round(
                    float(trade.option_premium) * int(trade.qty), 2
                )

            index_mode = str(self.cfg.get("instrument_type", "")).lower() == "index"
            options_mode = str(self.cfg.get("trade_instrument", "")).lower() == "options"
            if options_mode and trade.invested_amount:
                cost = float(trade.invested_amount)
                if cost > self.cash:
                    trade.status = TradeStatus.RISK_BLOCKED
                    if trade.risk_verdict:
                        trade.risk_verdict.reasons.append(
                            f"insufficient paper cash for option premium ₹{cost:.0f}"
                        )
                    self.store.upsert_trade(trade)
                    continue
                trade.mark_open(fill, when, trade.qty)
                self.cash -= cost
            elif index_mode:
                reserve = float(trade.max_loss_planned or 0)
                if reserve > self.cash:
                    trade.status = TradeStatus.RISK_BLOCKED
                    trade.risk_verdict.reasons.append("insufficient paper cash for risk reserve")
                    self.store.upsert_trade(trade)
                    continue
                trade.mark_open(fill, when, trade.qty)
            else:
                cost = fill * trade.qty
                if cost > self.cash:
                    trade.status = TradeStatus.RISK_BLOCKED
                    trade.risk_verdict.reasons.append("insufficient paper cash")
                    self.store.upsert_trade(trade)
                    continue
                trade.mark_open(fill, when, trade.qty)
                self.cash -= cost
            self.positions[trade.symbol] = trade
            self.store.upsert_trade(trade)
            inv = trade.invested_amount
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] PAPER BUY {trade.symbol} x{trade.qty} @ {fill:.2f}"
                + (f" · invested ₹{inv:.0f}" + (f" (prem {trade.option_premium})" if trade.option_premium else "") if inv else "")
                + f" | {trade.why}",
                trade.model_dump(mode="json"),
            )
            opened.append(trade)
            trades_today += 1
            daily_risk += trade.max_loss_planned or 0

        self._persist()
        return opened

    def manage_open(self) -> list[TradeRecord]:
        """Check stops/targets/flat-by on open paper positions using today's candles."""
        when = now_ist()
        day = when.date()
        closed: list[TradeRecord] = []
        for symbol, trade in list(self.positions.items()):
            try:
                df = self.client.get_session_candles(
                    symbol, day, interval=self.cfg.get("bar_interval", "FIVE_MINUTE"),
                )
            except Exception as e:
                logger.warning("manage %s: %s", symbol, e)
                continue
            if df.empty or trade.entry_time is None:
                continue

            entry = float(trade.entry_price or trade.planned_entry)
            initial_stop = float(trade.raw.get("initial_stop") or trade.planned_stop)
            if "initial_stop" not in trade.raw:
                trade.raw["initial_stop"] = initial_stop
            target = float(trade.planned_target)
            flat_t = parse_hhmm(trade.flat_by or self.cfg.get("flat_by", "15:15"))
            flat_dt = dt.datetime.combine(day, flat_t, tzinfo=IST)
            side = trade.side or "buy"

            from src.strategies.trail import simulate_with_trail
            from src.backtest.engine import _map_exit_reason

            px, exit_tm, reason_s, meta = simulate_with_trail(
                df,
                trade.entry_time,
                entry,
                initial_stop,
                target,
                flat_dt,
                self.cfg,
                side=side,
                qty=int(trade.qty or 0),
            )
            trade.raw["trail"] = meta
            if meta.get("final_stop") is not None:
                trade.planned_stop = float(meta["final_stop"])
            if meta.get("scale_out") and not trade.raw.get("scale_out_noted"):
                so = meta["scale_out"]
                trade.strategy_steps = list(trade.strategy_steps or []) + [
                    f"Scale-out booked {so.get('lots')} lot @ {so.get('price')} ({so.get('r')}R); "
                    f"trailing runner."
                ]
                trade.raw["scale_out_noted"] = True
                self.store.add_event(
                    self._event_kind(),
                    f"[{self.desk}] PAPER SCALE-OUT {symbol} {so.get('lots')} lot @ {so.get('price')} "
                    f"({trade.option_type or 'EQ'}{trade.strike or ''})",
                    so,
                )

            if reason_s in ("eod", "scale_out+eod") and when.time() < flat_t:
                self.store.upsert_trade(trade)
                continue

            reason = _map_exit_reason(reason_s)
            slip = float(self.cfg.get("paper_slippage_bps", 2)) / 10000.0
            if side == "buy":
                px = px * (1 - slip)
            else:
                px = px * (1 + slip)

            trade.mark_closed(px, exit_tm or when, reason)
            index_mode = str(self.cfg.get("instrument_type", "")).lower() == "index"
            options_mode = str(self.cfg.get("trade_instrument", "")).lower() == "options"
            if options_mode and trade.invested_amount is not None:
                # Return premium capital ± index-proxy PnL
                self.cash += float(trade.invested_amount) + float(trade.pnl or 0)
            elif index_mode:
                self.cash += float(trade.pnl or 0)
            else:
                self.cash += px * trade.qty
            del self.positions[symbol]
            self.store.upsert_trade(trade)
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] PAPER EXIT {symbol} @ {px:.2f} ({reason.value}) PnL ₹{trade.pnl:.2f}",
                trade.model_dump(mode="json"),
            )
            closed.append(trade)

        self._persist()
        return closed

    def tick(self) -> dict:
        when = now_ist()
        # Reload shared cash in case the other desk spent it
        state = self.store.get_paper_state()
        if state.get("cash") is not None:
            self.cash = float(state["cash"])
        opened = self.scan_once()
        closed = self.manage_open()
        wl_n = len(self.cfg.get("watchlist") or [])
        raw_batch = self.cfg.get("scan_batch_size")
        batch_n = wl_n if raw_batch in (None, "", 0, "0") else min(int(raw_batch), wl_n)
        if opened or closed:
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] Paper tick OK @ {when.strftime('%H:%M:%S')} IST · "
                f"scanned {batch_n}/{wl_n} · opened {len(opened)} · closed {len(closed)} · "
                f"cash ₹{self.cash:,.0f}",
            )
        elif is_market_open(self.cfg, when) and in_entry_window(self.cfg, when):
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] Paper tick OK @ {when.strftime('%H:%M:%S')} IST · "
                f"market open · scanned {batch_n}/{wl_n} · no new setup · "
                f"open {len(self.positions)} · cash ₹{self.cash:,.0f}",
            )
        return {
            "desk": self.desk,
            "opened": [t.id for t in opened],
            "closed": [t.id for t in closed],
            "cash": self.cash,
            "open_positions": list(self.positions.keys()),
            "as_of_ist": when.isoformat(),
            "market_open": is_market_open(self.cfg, when),
            "watchlist_n": wl_n,
            "scan_batch_size": batch_n,
        }

    def snapshot(self) -> dict:
        closed = [
            t
            for t in self.store.list_trades(mode=Mode.PAPER, status="closed", limit=500)
            if trade_desk(t) == self.desk
        ]
        open_t = list(self.positions.values())
        invested = sum(float(t.invested_amount or 0) for t in open_t)
        return {
            "desk": self.desk,
            "cash": self.cash,
            "invested_open": round(invested, 2),
            "open": [t.model_dump(mode="json") for t in open_t],
            "closed_count": len(closed),
            "realized_pnl": sum(t.pnl or 0 for t in closed),
            "live_enabled": bool(self.cfg.get("live_enabled", False)),
            "paper": True,
            "strategy": self.cfg.get("strategy", "nifty_signal_v1"),
            "trade_instrument": self.cfg.get("trade_instrument"),
            "watchlist_n": len(self.cfg.get("watchlist") or []),
        }
