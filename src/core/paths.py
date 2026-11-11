from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
TRADER_DIR = ROOT / "trader"
STATE_DIR = TRADER_DIR / "state"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
INSTRUMENTS_DIR = DATA_DIR / "instruments"
DB_PATH = DATA_DIR / "trades.db"
ENV_PATH = ROOT / ".env"
TRADING_CONFIG_PATH = CONFIG_DIR / "trading.json"
STOCKS_CONFIG_PATH = CONFIG_DIR / "stocks.json"
CAPITAL_CONFIG_PATH = CONFIG_DIR / "capital.json"

DESK_CONFIG_PATHS = {
    "nifty": TRADING_CONFIG_PATH,
    "stocks": STOCKS_CONFIG_PATH,
}


def ensure_dirs() -> None:
    for path in (TRADER_DIR, STATE_DIR, DATA_DIR, CACHE_DIR, INSTRUMENTS_DIR):
        path.mkdir(parents=True, exist_ok=True)