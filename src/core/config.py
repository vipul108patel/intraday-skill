from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import (
    CAPITAL_CONFIG_PATH,
    DESK_CONFIG_PATHS,
    ENV_PATH,
    STOCKS_CONFIG_PATH,
    TRADING_CONFIG_PATH,
    ensure_dirs,
)


class AngelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    ANGEL_API_KEY: str = ""
    ANGEL_CLIENT_CODE: str = ""
    ANGEL_PASSWORD: str = ""
    ANGEL_TOTP_SECRET: str = ""
    ANGEL_JWT_TOKEN: str = ""
    ANGEL_FEED_TOKEN: str = ""
    ANGEL_REFRESH_TOKEN: str = ""

    @property
    def angel_api_key(self) -> str:
        return self.ANGEL_API_KEY

    @property
    def angel_client_code(self) -> str:
        return self.ANGEL_CLIENT_CODE

    @property
    def angel_password(self) -> str:
        return self.ANGEL_PASSWORD

    @property
    def angel_totp_secret(self) -> str:
        return self.ANGEL_TOTP_SECRET

    @property
    def angel_jwt_token(self) -> str:
        return self.ANGEL_JWT_TOKEN

    @property
    def angel_feed_token(self) -> str:
        return self.ANGEL_FEED_TOKEN

    @property
    def angel_refresh_token(self) -> str:
        return self.ANGEL_REFRESH_TOKEN

    def configured(self) -> bool:
        return bool(
            self.ANGEL_API_KEY
            and self.ANGEL_CLIENT_CODE
            and self.ANGEL_PASSWORD
            and self.ANGEL_TOTP_SECRET
        )


def load_trading_config(path=None) -> dict[str, Any]:
    ensure_dirs()
    cfg_path = path or TRADING_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


def save_trading_config(cfg: dict[str, Any], path=None) -> None:
    cfg_path = path or TRADING_CONFIG_PATH
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def load_capital_config() -> dict[str, Any]:
    ensure_dirs()
    if not CAPITAL_CONFIG_PATH.exists():
        nifty = load_trading_config()
        return {
            "total_capital": float(nifty.get("budget") or nifty.get("paper_starting_cash") or 150000),
            "paper_starting_cash": float(nifty.get("paper_starting_cash") or nifty.get("budget") or 150000),
            "currency": "INR",
            "shared": True,
        }
    with open(CAPITAL_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_capital_config(cfg: dict[str, Any]) -> None:
    ensure_dirs()
    with open(CAPITAL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def normalize_desk(desk: str | None) -> str:
    d = str(desk or "nifty").strip().lower()
    if d in ("stock", "equity", "nifty100", "n100"):
        return "stocks"
    if d in ("index", "nifty50", "nse"):
        return "nifty"
    if d not in DESK_CONFIG_PATHS:
        return "nifty"
    return d


def load_desk_config(desk: str | None = "nifty") -> dict[str, Any]:
    """Load desk config and overlay shared capital so both desks use one pool."""
    key = normalize_desk(desk)
    path = DESK_CONFIG_PATHS.get(key, TRADING_CONFIG_PATH)
    cfg = load_trading_config(path)
    capital = load_capital_config()
    total = float(capital.get("total_capital") or capital.get("paper_starting_cash") or cfg.get("budget") or 150000)
    start = float(capital.get("paper_starting_cash") or total)
    cfg["desk"] = key
    cfg["budget"] = total
    cfg["paper_starting_cash"] = start
    cfg["_capital"] = capital
    cfg["_config_path"] = str(path)
    return cfg


def save_desk_config(cfg: dict[str, Any], desk: str | None = None) -> None:
    key = normalize_desk(desk or cfg.get("desk"))
    path = DESK_CONFIG_PATHS.get(key, TRADING_CONFIG_PATH)
    payload = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    save_trading_config(payload, path)


def list_desks() -> list[dict[str, Any]]:
    out = []
    for key, path in DESK_CONFIG_PATHS.items():
        if not path.exists() and key == "stocks":
            continue
        cfg = load_desk_config(key)
        out.append(
            {
                "desk": key,
                "label": cfg.get("desk_label") or key.title(),
                "strategy": cfg.get("strategy"),
                "trade_instrument": cfg.get("trade_instrument"),
                "watchlist_n": len(cfg.get("watchlist") or []),
                "config_path": str(path),
            }
        )
    return out


@lru_cache(maxsize=1)
def get_angel_settings() -> AngelSettings:
    return AngelSettings()


def reload_angel_settings() -> AngelSettings:
    get_angel_settings.cache_clear()
    return get_angel_settings()