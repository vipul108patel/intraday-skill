"""Live trading stubs — wired to Angel One but DISABLED by default."""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.angelone.client import AngelOneClient, AngelOneError, get_client
from src.core.config import load_trading_config
from src.db.store import get_store
from src.models.trade import Mode, TradeRecord
from src.paper.engine import PaperEngine

logger = logging.getLogger(__name__)


class LiveEngine:
    """
    Mirrors paper flow but can place real Angel One orders when:
      - config live_enabled == true
      - config paper == false
    Until then every call is refused or routed to paper.
    """

    def __init__(
        self,
        cfg: Optional[dict] = None,
        client: Optional[AngelOneClient] = None,
    ):
        self.cfg = cfg or load_trading_config()
        self.client = client or get_client()
        self.store = get_store()
        self.paper = PaperEngine(cfg=self.cfg, client=self.client)

    def enabled(self) -> bool:
        return bool(self.cfg.get("live_enabled", False)) and not bool(
            self.cfg.get("paper", True)
        )

    def status(self) -> dict[str, Any]:
        return {
            "live_enabled": bool(self.cfg.get("live_enabled", False)),
            "paper_flag": bool(self.cfg.get("paper", True)),
            "armed": self.enabled(),
            "message": (
                "LIVE ARMED — real orders can be sent"
                if self.enabled()
                else "Live trading DISABLED. Paper only. Set paper=false and live_enabled=true to arm."
            ),
        }

    def place_bracket(self, trade: TradeRecord) -> dict[str, Any]:
        if not self.enabled():
            self.store.add_event(
                "live",
                "Refused live order — live trading disabled",
                {"symbol": trade.symbol, "trade_id": trade.id},
            )
            raise AngelOneError(
                "Live trading is disabled. Keep paper trading until you explicitly "
                "set live_enabled=true and paper=false in config/trading.json."
            )

        token = self.client.symbol_token(trade.symbol)
        order = {
            "variety": "NORMAL",
            "tradingsymbol": f"{trade.symbol}-EQ",
            "symboltoken": token,
            "transactiontype": "BUY",
            "exchange": "NSE",
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "quantity": str(trade.qty),
        }
        # Note: Angel One brackets differ from Alpaca; stop/target often via
        # separate SL/target orders or GTT. Configured here as primary entry;
        # attach SL/target after fill in a future hardening pass.
        self.store.add_event("live", f"Placing LIVE order {trade.symbol}", order)
        resp = self.client.place_order(order, live_enabled=True)
        self.store.add_event("live", "Live order response", {"resp": resp})
        return {"order": order, "response": resp, "mode": Mode.LIVE.value}

    def tick(self) -> dict:
        """While disabled, run paper tick. When armed, still start with paper scan
        then require explicit place_bracket per trade (safety)."""
        if not self.enabled():
            result = self.paper.tick()
            result["live"] = self.status()
            return result
        # Armed: discover setups via paper path but do not auto-send
        opened = self.paper.scan_once()
        self.paper.manage_open()
        return {
            "candidates": [t.model_dump(mode="json") for t in opened],
            "live": self.status(),
            "note": "Candidates found; call place_bracket(trade) explicitly — no auto live fire.",
        }