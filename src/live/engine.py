"""Live trading — Angel One NFO auto-fire with software SL/target/trail."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from src.angelone.client import AngelOneClient, AngelOneError, get_client
from src.core.config import live_armed, load_desk_config, normalize_desk
from src.db.store import get_store
from src.market.calendar import IST, in_entry_window, is_market_open, now_ist, parse_hhmm
from src.market.context import load_universe_context
from src.models.trade import ExitReason, Mode, TradeRecord, TradeStatus
from src.paper.engine import trade_desk
from src.strategies.pipeline import scan_and_build
from src.strategies.risk import daily_loss_limit_inr

logger = logging.getLogger(__name__)


class LiveEngine:
    """
    Per-desk live engine. Real NFO orders only when:
      - capital/desk: live_enabled == true AND paper == false
    Auto-fires MARKET BUY on signal; manages exits via MARKET SELL.
    """

    def __init__(
        self,
        cfg: Optional[dict] = None,
        client: Optional[AngelOneClient] = None,
        desk: Optional[str] = None,
    ):
        if cfg is None:
            self.cfg = load_desk_config(desk or "nifty")
        else:
            self.cfg = dict(cfg)
            if desk:
                self.cfg["desk"] = normalize_desk(desk)
        self.desk = normalize_desk(self.cfg.get("desk") or desk or "nifty")
        self.cfg["desk"] = self.desk
        self.client = client or get_client()
        self.store = get_store()
        self.positions: dict[str, TradeRecord] = {}
        for t in self.store.list_trades(mode=Mode.LIVE, status="open", limit=200):
            if trade_desk(t) != self.desk:
                continue
            self.positions[t.symbol] = t
        self._rms_cache: Optional[dict[str, Any]] = None
        self._last_error: Optional[str] = None

    def enabled(self) -> bool:
        return live_armed(self.cfg)

    def _event_kind(self) -> str:
        return "live_stocks" if self.desk == "stocks" else "live"

    def _product(self) -> str:
        return str(self.cfg.get("live_product_type") or self.cfg.get("product_type") or "INTRADAY")

    def status(self) -> dict[str, Any]:
        armed = self.enabled()
        rms: dict[str, Any] = {}
        rms_error = None
        available = None
        try:
            if self.client.configured():
                rms = self.client.get_rms()
                self._rms_cache = rms
                available = float(rms.get("availablecash") or rms.get("net") or 0)
        except Exception as e:
            rms_error = str(e)
            self._last_error = rms_error
        return {
            "desk": self.desk,
            "live_enabled": bool(self.cfg.get("live_enabled", False)),
            "paper_flag": bool(self.cfg.get("paper", True)),
            "armed": armed,
            "available_cash": available,
            "rms": {
                "availablecash": rms.get("availablecash") if rms else None,
                "net": rms.get("net") if rms else None,
                "utilisedoptionpremium": rms.get("utilisedoptionpremium") if rms else None,
                "m2munrealized": rms.get("m2munrealized") if rms else None,
            },
            "rms_error": rms_error,
            "open_positions": list(self.positions.keys()),
            "last_error": self._last_error,
            "message": (
                f"LIVE ARMED [{self.desk}] — auto NFO orders ON"
                if armed
                else "Live trading DISABLED. Set paper=false and live_enabled=true (capital.json / desks) to arm."
            ),
        }

    def _require_armed(self) -> None:
        if not self.enabled():
            raise AngelOneError(
                "Live trading is disabled. Keep paper until you set live_enabled=true "
                "and paper=false (shared capital.json + desk configs)."
            )

    def _real_available_cash(self) -> float:
        try:
            return float(self.client.available_cash())
        except Exception as e:
            logger.warning("RMS cash unavailable (%s): %s", self.desk, e)
            if self._rms_cache:
                try:
                    return float(
                        self._rms_cache.get("availablecash")
                        or self._rms_cache.get("net")
                        or 0
                    )
                except (TypeError, ValueError):
                    pass
            return 0.0

    def _available_cash(self) -> float:
        """Cash used for risk sizing. Can assume a floor so low RMS does not block attempts."""
        real = self._real_available_cash()
        assume = float(self.cfg.get("live_assume_cash") or 0)
        if assume > 0:
            return max(real, assume)
        if bool(self.cfg.get("live_ignore_cash_check", True)):
            return max(real, 50000.0)
        return real

    def _ignore_cash_gate(self) -> bool:
        return bool(self.cfg.get("live_ignore_cash_check", True)) or float(
            self.cfg.get("live_assume_cash") or 0
        ) > 0

    def _next_watch_batch(self) -> list[str]:
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
        key = f"live_scan_cursor_{self.desk}"
        cursor = int(state.get(key) or 0) % len(symbols)
        out = [symbols[(cursor + i) % len(symbols)] for i in range(min(batch, len(symbols)))]
        state[key] = (cursor + batch) % len(symbols)
        cash = float(state.get("cash") if state.get("cash") is not None else 0)
        payload = {k: v for k, v in state.items() if k != "cash"}
        self.store.set_paper_state(cash, payload)
        return out

    def _session_stats(self) -> tuple[int, float, int, float]:
        today = now_ist().date().isoformat()
        trades = self.store.list_trades(mode=Mode.LIVE, limit=400)
        n = 0
        risk = 0.0
        realized_shared = 0.0
        closed_ordered: list[TradeRecord] = []
        for t in trades:
            if t.session_date != today:
                continue
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

    def _option_leg(self, trade: TradeRecord) -> dict[str, Any]:
        opt = dict((trade.raw or {}).get("option") or {})
        token = opt.get("token") or (trade.raw or {}).get("option_token")
        tsym = trade.option_symbol or opt.get("tradingsymbol")
        if not token or not tsym:
            raise AngelOneError(
                f"Missing NFO token/symbol for {trade.symbol} — cannot place live order"
            )
        return {
            **opt,
            "token": str(token),
            "tradingsymbol": str(tsym),
            "exchange": str(opt.get("exchange") or "NFO"),
        }

    def _refresh_premium(self, trade: TradeRecord, opt: dict[str, Any]) -> float:
        prem = float(
            self.client.get_ltp_by_token(
                str(opt.get("exchange") or "NFO"),
                str(opt["tradingsymbol"]),
                str(opt["token"]),
            )
        )
        if prem <= 0:
            raise AngelOneError(f"Invalid option LTP for {opt['tradingsymbol']}")
        trade.option_premium = prem
        trade.invested_amount = round(prem * int(trade.qty or 0), 2)
        opt = {
            **opt,
            "premium": prem,
            "premium_source": "live_ltp",
            "invested_amount": trade.invested_amount,
        }
        trade.raw["option"] = opt
        return prem

    def scan_once(self) -> list[TradeRecord]:
        self._require_armed()
        when = now_ist()
        if not is_market_open(self.cfg, when):
            from src.market.calendar import holiday_name, is_holiday, is_trading_day

            day = when.date()
            if is_holiday(day):
                why = f"NSE holiday — {holiday_name(day)} ({day.isoformat()})"
            elif not is_trading_day(day):
                why = f"Weekend — {day.isoformat()}"
            else:
                why = (
                    f"Outside session "
                    f"({self.cfg.get('session_open', '09:15')}–{self.cfg.get('session_close', '15:30')} IST)"
                )
            self.store.add_event(self._event_kind(), f"[{self.desk}] LIVE scan skipped · {why}")
            return []
        if not in_entry_window(self.cfg, when):
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] LIVE outside entry window — "
                f"{self.cfg.get('no_entries_before', '09:30')}–{self.cfg.get('no_entries_after', '15:00')} IST",
            )
            return []

        symbols = self._next_watch_batch()
        day = when.date()
        trades_today, daily_risk, streak, realized = self._session_stats()
        cash = self._available_cash()

        max_streak = int(self.cfg.get("max_consecutive_losses", 2))
        if streak >= max_streak:
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] LIVE streak block — {streak} consecutive losses",
            )
            return []

        day_cap = daily_loss_limit_inr(self.cfg, cash=cash)
        if realized < 0 and abs(realized) >= day_cap - 1e-6:
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] LIVE daily loss limit — ₹{realized:.0f} ≥ ₹{day_cap:.0f}",
            )
            return []

        try:
            candle_map, bars_15m_map, prior_map = load_universe_context(
                self.client, symbols, day, self.cfg
            )
        except Exception as e:
            logger.warning("Live context load failed (%s): %s", self.desk, e)
            self._last_error = str(e)
            self.store.add_event(self._event_kind(), f"[{self.desk}] LIVE candle fetch failed — {e}")
            return []

        trades = scan_and_build(
            candle_map,
            day,
            cfg=self.cfg,
            mode=Mode.LIVE,
            as_of=when,
            bars_15m_map=bars_15m_map,
            prior_map=prior_map,
            trades_today=trades_today,
            daily_risk_used=daily_risk,
            consecutive_losses=streak,
            realized_day_pnl=realized,
            cash=cash,
        )
        opened: list[TradeRecord] = []

        for trade in trades:
            trade.mode = Mode.LIVE
            trade.desk = self.desk
            self.store.save_setup(
                {
                    "as_of": when.isoformat(),
                    "symbol": trade.symbol,
                    "strategy": trade.strategy,
                    "status": trade.status.value,
                    "why": trade.why,
                    "mode": "live",
                    "desk": self.desk,
                }
            )
            if trade.symbol in self.positions:
                continue
            if not trade.risk_verdict or not trade.risk_verdict.passed:
                self.store.upsert_trade(trade)
                continue
            if trades_today >= int(self.cfg.get("max_trades_per_day", 3)):
                trade.status = TradeStatus.RISK_BLOCKED
                if trade.risk_verdict:
                    trade.risk_verdict.reasons.append("max trades/day live")
                self.store.upsert_trade(trade)
                continue

            try:
                opt = self._option_leg(trade)
                prem = self._refresh_premium(trade, opt)
                cost = float(trade.invested_amount or 0)
                real_cash = self._real_available_cash()
                # Soft warning only — still place so Angel can accept/reject
                if cost > real_cash and not self._ignore_cash_gate():
                    trade.status = TradeStatus.RISK_BLOCKED
                    if trade.risk_verdict:
                        trade.risk_verdict.reasons.append(
                            f"insufficient Angel cash for premium ₹{cost:.0f} (avail ₹{real_cash:.0f})"
                        )
                    self.store.upsert_trade(trade)
                    continue
                if cost > real_cash:
                    steps = list(trade.strategy_steps or [])
                    steps.append(
                        f"Cash gate bypassed — real RMS ₹{real_cash:.2f}, "
                        f"assumed sizing cash ₹{cash:.0f}, premium ₹{cost:.0f}; placing anyway"
                    )
                    trade.strategy_steps = steps

                # Spot fill reference for trail timing (underlying)
                try:
                    spot_ltp = float(self.client.get_ltp(trade.symbol))
                except Exception:
                    spot_ltp = float(trade.planned_entry or 0)

                trade.raw["live"] = {
                    "order_attempted": True,
                    "product": self._product(),
                    "real_cash": real_cash,
                    "assumed_cash": cash,
                    "premium_at_attempt": prem,
                }
                resp = self.client.place_nfo_market(
                    transaction="BUY",
                    tradingsymbol=opt["tradingsymbol"],
                    token=opt["token"],
                    qty=int(trade.qty),
                    product=self._product(),
                    live_enabled=True,
                    ordertag=f"L{self.desk[:3]}",
                )
                order_id = resp.get("order_id")
                fill_prem = self.client.fill_price_for_order(
                    order_id, opt["tradingsymbol"], fallback=prem
                ) or prem
                trade.option_premium = float(fill_prem)
                trade.invested_amount = round(float(fill_prem) * int(trade.qty), 2)
                trade.raw["option"] = {
                    **opt,
                    "premium": trade.option_premium,
                    "premium_source": "live_fill",
                    "invested_amount": trade.invested_amount,
                }
                trade.raw["live"] = {
                    **(trade.raw.get("live") or {}),
                    "entry_order_id": order_id,
                    "entry_response": resp.get("response"),
                    "entry_order": resp.get("order"),
                    "order_ok": True,
                }
                trade.raw["initial_stop"] = float(trade.planned_stop or 0)
                # Trail uses underlying entry; store premium separately
                trade.mark_open(spot_ltp, when, trade.qty)
                trade.raw["entry_premium"] = trade.option_premium
                self.positions[trade.symbol] = trade
                self.store.upsert_trade(trade)
                self.store.add_event(
                    self._event_kind(),
                    f"[{self.desk}] LIVE BUY {opt['tradingsymbol']} x{trade.qty} "
                    f"@ prem ₹{fill_prem:.2f} · order {order_id} · {trade.why}",
                    trade.model_dump(mode="json"),
                )
                opened.append(trade)
                trades_today += 1
                daily_risk += trade.max_loss_planned or 0
                cash = max(0.0, cash - float(trade.invested_amount or 0))
            except Exception as e:
                self._last_error = str(e)
                logger.exception("Live entry failed %s: %s", trade.symbol, e)
                # Still show on dashboard — cancelled = order attempted but rejected/failed
                trade.status = TradeStatus.CANCELLED
                trade.entry_time = when
                trade.session_date = when.date().isoformat()
                if trade.risk_verdict:
                    trade.risk_verdict.reasons.append(f"live order failed: {e}")
                opt = (trade.raw or {}).get("option") or {}
                trade.raw["live"] = {
                    **(trade.raw.get("live") or {}),
                    "order_attempted": True,
                    "order_ok": False,
                    "entry_error": str(e),
                    "product": self._product(),
                }
                steps = list(trade.strategy_steps or [])
                steps.append(f"LIVE ORDER FAILED (still logged): {e}")
                trade.strategy_steps = steps
                self.store.upsert_trade(trade)
                self.store.add_event(
                    self._event_kind(),
                    f"[{self.desk}] LIVE BUY FAILED {trade.symbol} "
                    f"{trade.option_symbol or opt.get('tradingsymbol') or ''} — {e}",
                    trade.model_dump(mode="json"),
                )

        return opened

    def manage_open(self) -> list[TradeRecord]:
        if not self.positions:
            # Reload in case another process opened
            for t in self.store.list_trades(mode=Mode.LIVE, status="open", limit=200):
                if trade_desk(t) == self.desk:
                    self.positions[t.symbol] = t
        if not self.enabled() and self.positions:
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] LIVE disarmed but {len(self.positions)} open — still managing exits",
            )

        when = now_ist()
        day = when.date()
        closed: list[TradeRecord] = []

        for symbol, trade in list(self.positions.items()):
            try:
                df = self.client.get_session_candles(
                    symbol,
                    day,
                    interval=self.cfg.get("bar_interval", "FIVE_MINUTE"),
                )
            except Exception as e:
                logger.warning("live manage %s: %s", symbol, e)
                continue
            if df is None or df.empty or trade.entry_time is None:
                continue

            # Trail on underlying levels (same as paper)
            entry = float(trade.entry_price or trade.planned_entry)
            initial_stop = float(trade.raw.get("initial_stop") or trade.planned_stop)
            if "initial_stop" not in trade.raw:
                trade.raw["initial_stop"] = initial_stop
            target = float(trade.planned_target)
            flat_t = parse_hhmm(trade.flat_by or self.cfg.get("flat_by", "15:15"))
            flat_dt = dt.datetime.combine(day, flat_t, tzinfo=IST)
            side = trade.side or "buy"

            from src.backtest.engine import _map_exit_reason
            from src.strategies.trail import simulate_with_trail

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

            if reason_s in ("eod", "scale_out+eod") and when.time() < flat_t:
                self.store.upsert_trade(trade)
                continue

            reason = _map_exit_reason(reason_s)
            try:
                self._require_armed()
                opt = self._option_leg(trade)
                exit_resp = self.client.place_nfo_market(
                    transaction="SELL",
                    tradingsymbol=opt["tradingsymbol"],
                    token=opt["token"],
                    qty=int(trade.qty),
                    product=self._product(),
                    live_enabled=True,
                    ordertag=f"X{self.desk[:3]}",
                )
                exit_oid = exit_resp.get("order_id")
                try:
                    exit_prem = self.client.fill_price_for_order(
                        exit_oid,
                        opt["tradingsymbol"],
                        fallback=self.client.get_ltp_by_token(
                            "NFO", opt["tradingsymbol"], opt["token"]
                        ),
                    )
                except Exception:
                    exit_prem = None
                if not exit_prem or exit_prem <= 0:
                    exit_prem = float(trade.option_premium or 0)

                entry_prem = float(
                    trade.raw.get("entry_premium") or trade.option_premium or 0
                )
                # PnL from option premiums; keep side long option
                trade.side = "buy"
                trade.entry_price = entry_prem
                trade.raw.setdefault("live", {})
                trade.raw["live"]["exit_order_id"] = exit_oid
                trade.raw["live"]["exit_response"] = exit_resp.get("response")
                trade.raw["live"]["exit_premium"] = exit_prem
                trade.raw["live"]["index_exit"] = px
                trade.mark_closed(float(exit_prem), exit_tm or when, reason)
                del self.positions[symbol]
                self.store.upsert_trade(trade)
                self.store.add_event(
                    self._event_kind(),
                    f"[{self.desk}] LIVE SELL {opt['tradingsymbol']} x{trade.qty} "
                    f"@ ₹{exit_prem:.2f} ({reason.value}) PnL ₹{trade.pnl:.2f} · order {exit_oid}",
                    trade.model_dump(mode="json"),
                )
                closed.append(trade)
            except Exception as e:
                self._last_error = str(e)
                logger.exception("Live exit failed %s: %s", symbol, e)
                self.store.add_event(
                    self._event_kind(),
                    f"[{self.desk}] LIVE EXIT FAILED {symbol} ({reason.value}) — {e}",
                    {"error": str(e), "trade_id": trade.id},
                )
                self.store.upsert_trade(trade)

        return closed

    def place_bracket(self, trade: TradeRecord) -> dict[str, Any]:
        """Explicit entry helper — same path as auto-fire for one trade."""
        self._require_armed()
        trade.mode = Mode.LIVE
        trade.desk = self.desk
        opt = self._option_leg(trade)
        prem = self._refresh_premium(trade, opt)
        resp = self.client.place_nfo_market(
            transaction="BUY",
            tradingsymbol=opt["tradingsymbol"],
            token=opt["token"],
            qty=int(trade.qty),
            product=self._product(),
            live_enabled=True,
            ordertag=f"L{self.desk[:3]}",
        )
        order_id = resp.get("order_id")
        fill = self.client.fill_price_for_order(
            order_id, opt["tradingsymbol"], fallback=prem
        ) or prem
        when = now_ist()
        try:
            spot = float(self.client.get_ltp(trade.symbol))
        except Exception:
            spot = float(trade.planned_entry or 0)
        trade.option_premium = float(fill)
        trade.invested_amount = round(float(fill) * int(trade.qty), 2)
        trade.raw["entry_premium"] = trade.option_premium
        trade.raw["live"] = {
            "entry_order_id": order_id,
            "entry_response": resp.get("response"),
            "product": self._product(),
        }
        trade.raw["initial_stop"] = float(trade.planned_stop or 0)
        trade.mark_open(spot, when, trade.qty)
        self.positions[trade.symbol] = trade
        self.store.upsert_trade(trade)
        self.store.add_event(
            self._event_kind(),
            f"[{self.desk}] LIVE place_bracket {opt['tradingsymbol']} x{trade.qty} @ ₹{fill:.2f}",
            trade.model_dump(mode="json"),
        )
        return {
            "order_id": order_id,
            "response": resp.get("response"),
            "premium": fill,
            "mode": Mode.LIVE.value,
            "desk": self.desk,
        }

    def _scan_poll_seconds(self) -> int:
        """How often to run new-entry scan. Stocks default 300s (5m bars); Nifty 60s."""
        raw = self.cfg.get("scan_poll_seconds")
        if raw in (None, ""):
            return 300 if self.desk == "stocks" else 60
        return max(15, int(raw))

    def _should_scan_entries(self, when: dt.datetime) -> bool:
        """Throttle entry scans per desk (manage/exits still run every tick)."""
        poll = self._scan_poll_seconds()
        state = self.store.get_paper_state()
        key = f"live_last_scan_{self.desk}"
        prev = state.get(key)
        if not prev:
            return True
        try:
            last = dt.datetime.fromisoformat(str(prev))
            if last.tzinfo is None:
                last = last.replace(tzinfo=IST)
            return (when - last).total_seconds() >= poll - 1
        except Exception:
            return True

    def _mark_scanned(self, when: dt.datetime) -> None:
        state = self.store.get_paper_state()
        cash = float(state.get("cash") if state.get("cash") is not None else 0)
        payload = {k: v for k, v in state.items() if k != "cash"}
        payload[f"live_last_scan_{self.desk}"] = when.isoformat()
        self.store.set_paper_state(cash, payload)

    def tick(self) -> dict[str, Any]:
        when = now_ist()
        if not self.enabled():
            st = self.status()
            self.store.add_event(
                self._event_kind(),
                f"[{self.desk}] LIVE tick refused — not armed",
                st,
            )
            return {
                "desk": self.desk,
                "opened": [],
                "closed": [],
                "live": st,
                "as_of_ist": when.isoformat(),
                "note": "Not armed — no live scan/orders",
            }

        poll = self._scan_poll_seconds()
        do_scan = self._should_scan_entries(when)
        opened: list[TradeRecord] = []
        if do_scan:
            opened = self.scan_once()
            self._mark_scanned(when)
        # Exits / trail always — even between 5m entry scans
        closed = self.manage_open()
        st = self.status()
        wl_n = len(self.cfg.get("watchlist") or [])
        scan_note = f"entry-scan ON (poll {poll}s)" if do_scan else f"entry-scan skip (next ≤{poll}s)"
        self.store.add_event(
            self._event_kind(),
            f"[{self.desk}] LIVE tick @ {when.strftime('%H:%M:%S')} IST · {scan_note} · "
            f"opened {len(opened)} · closed {len(closed)} · open {len(self.positions)} · "
            f"cash ₹{(st.get('available_cash') or 0):,.0f}",
        )
        return {
            "desk": self.desk,
            "opened": [t.id for t in opened],
            "closed": [t.id for t in closed],
            "open_positions": list(self.positions.keys()),
            "available_cash": st.get("available_cash"),
            "entry_scan": do_scan,
            "scan_poll_seconds": poll,
            "live": st,
            "as_of_ist": when.isoformat(),
            "market_open": is_market_open(self.cfg, when),
            "watchlist_n": wl_n,
        }

    def snapshot(self) -> dict[str, Any]:
        closed = [
            t
            for t in self.store.list_trades(mode=Mode.LIVE, status="closed", limit=500)
            if trade_desk(t) == self.desk
        ]
        open_t = list(self.positions.values())
        st = self.status()
        return {
            "desk": self.desk,
            "armed": st["armed"],
            "available_cash": st.get("available_cash"),
            "rms": st.get("rms"),
            "invested_open": round(sum(float(t.invested_amount or 0) for t in open_t), 2),
            "open": [t.model_dump(mode="json") for t in open_t],
            "closed_count": len(closed),
            "realized_pnl": sum(t.pnl or 0 for t in closed),
            "live_enabled": bool(self.cfg.get("live_enabled", False)),
            "paper": bool(self.cfg.get("paper", True)),
            "strategy": self.cfg.get("strategy"),
            "message": st.get("message"),
            "last_error": st.get("last_error") or st.get("rms_error"),
        }


def tick_all_desks(client: Optional[AngelOneClient] = None) -> dict[str, Any]:
    """Run live tick for nifty + stocks sequentially (shared Angel session)."""
    client = client or get_client()
    out = {}
    for desk in ("nifty", "stocks"):
        eng = LiveEngine(desk=desk, client=client)
        out[desk] = eng.tick()
    return {"desks": out, "as_of_ist": now_ist().isoformat()}
