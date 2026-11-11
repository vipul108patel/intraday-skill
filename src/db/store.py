"""SQLite persistence for setups, trades, backtests, paper runs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from src.core.paths import DB_PATH, ensure_dirs
from src.models.trade import BacktestSummary, Mode, TradeRecord


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT,
    setup TEXT,
    side TEXT,
    payload TEXT NOT NULL,
    session_date TEXT,
    created_at TEXT,
    updated_at TEXT,
    pnl REAL
);

CREATE TABLE IF NOT EXISTS setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT,
    symbol TEXT,
    strategy TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtests (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    ended_at TEXT,
    strategy TEXT,
    payload TEXT NOT NULL,
    total_pnl REAL,
    trades INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    kind TEXT,
    message TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS paper_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL,
    payload TEXT
);
"""


class Store:
    def __init__(self, path=None):
        ensure_dirs()
        self.path = path or DB_PATH
        self._init()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # trades
    def upsert_trade(self, trade: TradeRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO trades (id, mode, status, symbol, strategy, setup, side, payload, session_date, created_at, updated_at, pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at,
                    pnl=excluded.pnl
                """,
                (
                    trade.id,
                    trade.mode.value,
                    trade.status.value,
                    trade.symbol,
                    trade.strategy,
                    trade.setup,
                    trade.side,
                    trade.model_dump_json(),
                    trade.session_date,
                    trade.created_at.isoformat(),
                    trade.updated_at.isoformat(),
                    trade.pnl,
                ),
            )

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT payload FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return None
        return TradeRecord.model_validate_json(row["payload"])

    def list_trades(
        self,
        mode: Optional[Mode] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[TradeRecord]:
        sql = "SELECT payload FROM trades WHERE 1=1"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode.value)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [TradeRecord.model_validate_json(r["payload"]) for r in rows]

    def save_setup(self, setup: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO setups (as_of, symbol, strategy, payload) VALUES (?, ?, ?, ?)",
                (
                    setup.get("as_of"),
                    setup.get("symbol"),
                    setup.get("strategy"),
                    json.dumps(setup, default=str),
                ),
            )

    def list_setups(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM setups ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def save_backtest(self, summary: BacktestSummary) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO backtests (id, started_at, ended_at, strategy, payload, total_pnl, trades)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.id,
                    summary.started_at.isoformat(),
                    summary.ended_at.isoformat(),
                    summary.strategy,
                    summary.model_dump_json(),
                    summary.total_pnl,
                    summary.trades,
                ),
            )

    def list_backtests(self, limit: int = 50) -> list[BacktestSummary]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM backtests ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [BacktestSummary.model_validate_json(r["payload"]) for r in rows]

    def add_event(self, kind: str, message: str, payload: Optional[dict] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, message, payload) VALUES (?, ?, ?, ?)",
                (
                    datetime.utcnow().isoformat(),
                    kind,
                    message,
                    json.dumps(payload or {}, default=str),
                ),
            )

    def list_events(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, kind, message, payload FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": r["ts"],
                "kind": r["kind"],
                "message": r["message"],
                "payload": json.loads(r["payload"] or "{}"),
            }
            for r in rows
        ]

    def get_paper_state(self) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT cash, payload FROM paper_state WHERE id=1").fetchone()
        if not row:
            return {"cash": None, "positions": {}, "realized_pnl": 0.0}
        data = json.loads(row["payload"] or "{}")
        data["cash"] = row["cash"]
        return data

    def set_paper_state(self, cash: float, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO paper_state (id, cash, payload) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET cash=excluded.cash, payload=excluded.payload
                """,
                (cash, json.dumps(payload, default=str)),
            )

    @staticmethod
    def _normalize_reset_modes(modes: Optional[list[str]]) -> set[str]:
        """Only backtest/paper are wipeable. Live is never cleared."""
        allowed = {"backtest", "paper"}
        raw = modes if modes is not None else ["backtest", "paper"]
        out = {str(m).lower().strip() for m in raw if str(m).lower().strip() in allowed}
        return out or {"backtest", "paper"}

    @staticmethod
    def _desk_of_payload(payload: str) -> str:
        from src.core.config import normalize_desk

        try:
            data = json.loads(payload)
        except Exception:
            return "nifty"
        d = data.get("desk")
        if not d and isinstance(data.get("raw"), dict):
            d = data["raw"].get("desk")
        if not d:
            return "nifty"
        return normalize_desk(d)

    def reset_all(
        self,
        starting_cash: float = 25000,
        modes: Optional[list[str]] = None,
    ) -> dict:
        """
        Clear backtest and/or paper across all desks.
        LIVE trades / live state are never deleted.
        """
        wipe = self._normalize_reset_modes(modes)
        clear_paper = "paper" in wipe
        clear_bt = "backtest" in wipe
        deleted_trades = 0
        deleted_backtests = 0
        deleted_setups = 0
        deleted_events = 0

        with self._conn() as conn:
            rows = conn.execute("SELECT id, mode, payload FROM trades").fetchall()
            for row in rows:
                mode = str(row["mode"] or "").lower()
                if mode == "live":
                    continue
                if mode not in wipe:
                    continue
                conn.execute("DELETE FROM trades WHERE id=?", (row["id"],))
                deleted_trades += 1

            if clear_bt:
                deleted_backtests = conn.execute("SELECT COUNT(*) AS n FROM backtests").fetchone()["n"]
                conn.execute("DELETE FROM backtests")

            if clear_paper:
                deleted_setups = conn.execute("SELECT COUNT(*) AS n FROM setups").fetchone()["n"]
                conn.execute("DELETE FROM setups")
                # Keep live-related events if any; drop paper/backtest/system noise for a clean paper book
                ev = conn.execute("SELECT id, kind, message FROM events").fetchall()
                for row in ev:
                    kind = str(row["kind"] or "")
                    msg = str(row["message"] or "").lower()
                    if kind == "live" or "[live]" in msg:
                        continue
                    conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
                    deleted_events += 1
                conn.execute("DELETE FROM paper_state")
                conn.execute(
                    "INSERT INTO paper_state (id, cash, payload) VALUES (1, ?, ?)",
                    (
                        starting_cash,
                        json.dumps(
                            {
                                "positions": {},
                                "realized_pnl": 0.0,
                                "reset_at": datetime.utcnow().isoformat(),
                                "reset_modes": sorted(wipe),
                            }
                        ),
                    ),
                )
            elif clear_bt:
                ev = conn.execute("SELECT id, kind, message FROM events").fetchall()
                for row in ev:
                    kind = str(row["kind"] or "")
                    msg = str(row["message"] or "").lower()
                    if kind in ("backtest",) or "backtest" in msg:
                        conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
                        deleted_events += 1

        scope = "+".join(sorted(wipe))
        cash = starting_cash if clear_paper else float(self.get_paper_state().get("cash") or starting_cash)
        self.add_event(
            "system",
            f"[all] Reset {scope} · trades {deleted_trades} · live kept · cash ₹{cash:,.0f}",
            {"modes": sorted(wipe), "deleted_trades": deleted_trades, "live_kept": True},
        )
        return {
            "desk": "all",
            "modes": sorted(wipe),
            "deleted_trades": deleted_trades,
            "deleted_setups": deleted_setups,
            "deleted_backtests": deleted_backtests,
            "deleted_events": deleted_events,
            "refund": 0.0,
            "cash": round(cash, 2),
            "live_kept": True,
        }

    def reset_desk(self, desk: str, modes: Optional[list[str]] = None) -> dict:
        """
        Wipe one desk's backtest and/or paper data.
        LIVE trades are never deleted. Shared paper cash kept; open paper capital refunded.
        """
        from src.core.config import normalize_desk

        key = normalize_desk(desk)
        wipe = self._normalize_reset_modes(modes)
        clear_paper = "paper" in wipe
        clear_bt = "backtest" in wipe
        deleted_trades = 0
        deleted_setups = 0
        deleted_backtests = 0
        deleted_events = 0
        refund = 0.0

        state = self.get_paper_state()
        cash = float(state.get("cash") or 0)

        with self._conn() as conn:
            rows = conn.execute("SELECT id, mode, status, payload FROM trades").fetchall()
            for row in rows:
                mode = str(row["mode"] or "").lower()
                if mode == "live":
                    continue
                if mode not in wipe:
                    continue
                if self._desk_of_payload(row["payload"]) != key:
                    continue
                try:
                    data = json.loads(row["payload"])
                except Exception:
                    data = {}
                if clear_paper and mode == "paper" and str(row["status"]).lower() == "open":
                    inv = data.get("invested_amount")
                    if inv:
                        refund += float(inv)
                    elif data.get("entry_price") and data.get("qty"):
                        if str(data.get("raw", {}).get("trade_instrument") or "").lower() == "equity":
                            refund += float(data["entry_price"]) * int(data["qty"])
                conn.execute("DELETE FROM trades WHERE id=?", (row["id"],))
                deleted_trades += 1

            if clear_paper:
                setup_rows = conn.execute("SELECT id, symbol, payload FROM setups").fetchall()
                for row in setup_rows:
                    keep = True
                    try:
                        data = json.loads(row["payload"] or "{}")
                        d = data.get("desk") or (data.get("raw") or {}).get("desk")
                        if d:
                            keep = normalize_desk(d) != key
                        elif key == "stocks":
                            keep = str(row["symbol"] or "").upper() in {"NIFTY", "NIFTY50", "BANKNIFTY"}
                        else:
                            keep = str(row["symbol"] or "").upper() not in {"NIFTY", "NIFTY50", ""}
                    except Exception:
                        keep = True
                    if not keep:
                        conn.execute("DELETE FROM setups WHERE id=?", (row["id"],))
                        deleted_setups += 1

            if clear_bt:
                bt_rows = conn.execute("SELECT id, payload FROM backtests").fetchall()
                for row in bt_rows:
                    try:
                        data = json.loads(row["payload"])
                        symbols = [str(s).upper() for s in (data.get("symbols") or [])]
                        if key == "nifty":
                            is_desk = (
                                (not symbols)
                                or symbols == ["NIFTY"]
                                or set(symbols) <= {"NIFTY", "NIFTY50"}
                            )
                        else:
                            is_desk = (
                                bool(symbols)
                                and "NIFTY" not in symbols
                                and "NIFTY50" not in symbols
                            )
                    except Exception:
                        is_desk = False
                    if is_desk:
                        conn.execute("DELETE FROM backtests WHERE id=?", (row["id"],))
                        deleted_backtests += 1

            ev_rows = conn.execute("SELECT id, kind, message FROM events").fetchall()
            for row in ev_rows:
                kind = str(row["kind"] or "")
                msg = str(row["message"] or "")
                msg_l = msg.lower()
                if kind == "live" or "[live]" in msg_l:
                    continue
                drop = False
                if key == "stocks":
                    desk_hit = kind in ("paper_stocks", "stocks") or "[stocks]" in msg
                else:
                    desk_hit = (
                        kind in ("paper", "nifty") and "[stocks]" not in msg
                    ) or "[nifty]" in msg
                if not desk_hit and kind != "backtest" and "backtest" not in msg_l:
                    continue
                if clear_paper and desk_hit and (
                    kind.startswith("paper") or "[stocks]" in msg or "[nifty]" in msg
                ):
                    drop = True
                if clear_bt and (kind == "backtest" or "backtest" in msg_l):
                    drop = True
                if clear_paper and desk_hit and kind == "system" and "reset" not in msg_l:
                    drop = True
                if drop:
                    conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
                    deleted_events += 1

            if clear_paper:
                cash = cash + refund
                payload = {k: v for k, v in state.items() if k != "cash"}
                by_desk = payload.get("positions_by_desk") or {}
                if isinstance(by_desk, dict):
                    by_desk[key] = {}
                    payload["positions_by_desk"] = by_desk
                    payload["positions"] = {
                        **(by_desk.get("nifty") or {}),
                        **(by_desk.get("stocks") or {}),
                    }
                payload.pop(f"scan_cursor_{key}", None)
                payload[f"reset_at_{key}"] = datetime.utcnow().isoformat()
                payload["reset_modes"] = sorted(wipe)
                conn.execute(
                    """
                    INSERT INTO paper_state (id, cash, payload) VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET cash=excluded.cash, payload=excluded.payload
                    """,
                    (cash, json.dumps(payload, default=str)),
                )

        scope = "+".join(sorted(wipe))
        self.add_event(
            "system",
            f"[{key}] Reset {scope} · trades {deleted_trades} · live kept · "
            f"refund ₹{refund:,.0f} · cash ₹{cash:,.0f}",
            {
                "desk": key,
                "modes": sorted(wipe),
                "deleted_trades": deleted_trades,
                "deleted_setups": deleted_setups,
                "deleted_backtests": deleted_backtests,
                "deleted_events": deleted_events,
                "refund": refund,
                "cash": cash,
                "live_kept": True,
            },
        )
        return {
            "desk": key,
            "modes": sorted(wipe),
            "deleted_trades": deleted_trades,
            "deleted_setups": deleted_setups,
            "deleted_backtests": deleted_backtests,
            "deleted_events": deleted_events,
            "refund": round(refund, 2),
            "cash": round(cash, 2),
            "live_kept": True,
        }


_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store