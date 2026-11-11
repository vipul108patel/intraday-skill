"""Angel One SmartAPI client for historical candles, LTP, and (optional) orders."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd
import pyotp

from src.core.config import AngelSettings, get_angel_settings, reload_angel_settings
from src.core.paths import CACHE_DIR, INSTRUMENTS_DIR, ensure_dirs
from src.market.calendar import IST, is_trading_day, now_ist

logger = logging.getLogger(__name__)

INSTRUMENT_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

# Common NSE index aliases (Angel OpenAPI tokens)
INDEX_ALIASES = {
    "NIFTY": {"exchange": "NSE", "name": "Nifty 50", "token": "99926000", "symbol": "Nifty 50"},
    "NIFTY50": {"exchange": "NSE", "name": "Nifty 50", "token": "99926000", "symbol": "Nifty 50"},
    "BANKNIFTY": {"exchange": "NSE", "name": "Nifty Bank", "token": "99926009", "symbol": "Nifty Bank"},
    "FINNIFTY": {"exchange": "NSE", "name": "Nifty Fin Service", "token": "99926037", "symbol": "Nifty Fin Service"},
}

INTERVAL_MAP = {
    "1m": "ONE_MINUTE",
    "3m": "THREE_MINUTE",
    "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
    "ONE_MINUTE": "ONE_MINUTE",
    "FIVE_MINUTE": "FIVE_MINUTE",
    "ONE_DAY": "ONE_DAY",
}


class AngelOneError(Exception):
    pass


def _is_rate_limit_error(err: Any) -> bool:
    text = str(err or "").lower()
    return any(
        s in text
        for s in (
            "access denied because of exceeding access rate",
            "exceeding access rate",
            "access rate",
            "rate limit",
            "too many requests",
        )
    )


class AngelOneClient:
    # Candle API official caps: 3/sec, 180/min. Stay under both with headroom for Nifty+Stocks.
    _CANDLE_MIN_INTERVAL_S = 0.45

    def __init__(self, settings: AngelSettings | None = None):
        ensure_dirs()
        self.settings = settings or get_angel_settings()
        self._smart = None
        self._jwt: Optional[str] = self.settings.angel_jwt_token or None
        self._feed: Optional[str] = self.settings.angel_feed_token or None
        self._refresh: Optional[str] = self.settings.angel_refresh_token or None
        self._instruments: Optional[pd.DataFrame] = None
        self._token_cache: dict[str, str] = {}
        self._last_candle_call_at = 0.0

    def _throttle_candles(self) -> None:
        """Space candle calls so we stay near ≤2.2 req/sec (safe under Angel's 3/sec)."""
        gap = self._CANDLE_MIN_INTERVAL_S
        now = time.monotonic()
        wait = gap - (now - self._last_candle_call_at)
        if wait > 0:
            time.sleep(wait)
        self._last_candle_call_at = time.monotonic()

    # ------------------------------------------------------------------ auth

    def configured(self) -> bool:
        return self.settings.configured()

    def _validate_api_key(self) -> None:
        key = (self.settings.angel_api_key or "").strip()
        if not key:
            raise AngelOneError(
                "ANGEL_API_KEY is empty in .env. Open https://smartapi.angelone.in/apps "
                "→ your app → copy the API Key (a short code, not a website URL)."
            )
        lower = key.lower()
        if lower.startswith("http://") or lower.startswith("https://") or "angelone" in lower or "angelbroking" in lower:
            raise AngelOneError(
                f"ANGEL_API_KEY looks like a URL ({key!r}). "
                "Replace it with the API Key from SmartAPI → My Apps (e.g. a code like '9bmmTPjj'), "
                "not https://smartapi.angelone.in."
            )

    def login(self, force: bool = False) -> dict[str, Any]:
        if not self.configured():
            raise AngelOneError(
                "Angel One credentials missing. Copy .env.example -> .env and fill "
                "ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PASSWORD, ANGEL_TOTP_SECRET."
            )
        self._validate_api_key()
        if self._jwt and not force:
            return {
                "jwtToken": self._jwt,
                "feedToken": self._feed,
                "refreshToken": self._refresh,
            }

        try:
            from SmartApi import SmartConnect
        except ImportError as e:
            raise AngelOneError(
                "smartapi-python not installed. Run: pip install -r requirements.txt"
            ) from e

        smart = SmartConnect(api_key=self.settings.angel_api_key)
        totp = pyotp.TOTP(self.settings.angel_totp_secret).now()
        data = smart.generateSession(
            self.settings.angel_client_code,
            self.settings.angel_password,
            totp,
        )
        if not data or not data.get("status"):
            raise AngelOneError(f"Angel login failed: {data}")

        payload = data["data"]
        self._jwt = payload.get("jwtToken")
        self._refresh = payload.get("refreshToken")
        self._feed = smart.getfeedToken()
        self._smart = smart
        logger.info("Angel One session created for %s", self.settings.angel_client_code)
        return {
            "jwtToken": self._jwt,
            "feedToken": self._feed,
            "refreshToken": self._refresh,
        }

    def _ensure_session(self):
        if self._smart is None:
            self.login()
            return
        if self._jwt is None:
            self.login(force=True)

    # ----------------------------------------------------------- instruments

    def download_instruments(self, force: bool = False) -> Path:
        path = INSTRUMENTS_DIR / "OpenAPIScripMaster.json"
        if path.exists() and not force:
            age = time.time() - path.stat().st_mtime
            if age < 24 * 3600:
                return path
        logger.info("Downloading Angel instrument master…")
        with httpx.Client(timeout=120) as client:
            r = client.get(INSTRUMENT_URL)
            r.raise_for_status()
            path.write_bytes(r.content)
        self._instruments = None
        self._token_cache.clear()
        return path

    def instruments(self, force: bool = False) -> pd.DataFrame:
        if self._instruments is not None and not force:
            return self._instruments
        path = self.download_instruments(force=force)
        df = pd.read_json(path)
        self._instruments = df
        return df

    def resolve_instrument(self, symbol: str, exchange: str = "NSE") -> dict:
        """Return {symbol, token, exchange, tradingsymbol, kind} for equity or index."""
        sym = symbol.upper().strip()
        if sym in INDEX_ALIASES:
            meta = INDEX_ALIASES[sym]
            return {
                "symbol": sym,
                "token": meta["token"],
                "exchange": meta["exchange"],
                "tradingsymbol": meta["symbol"],
                "kind": "index",
                "name": meta["name"],
            }

        key = f"{exchange}:{sym}"
        if key in self._token_cache:
            return {
                "symbol": sym,
                "token": self._token_cache[key],
                "exchange": exchange,
                "tradingsymbol": f"{sym}-EQ",
                "kind": "equity",
                "name": sym,
            }

        df = self.instruments()
        candidates = [f"{sym}-EQ", sym]
        sub = df[(df["exch_seg"] == exchange) & (df["symbol"].isin(candidates))]
        if sub.empty:
            sub = df[
                (df["exch_seg"] == exchange)
                & (df["name"].str.upper() == sym)
            ]
        if sub.empty:
            # index name search e.g. "NIFTY 50"
            sub = df[
                (df["exch_seg"] == "NSE")
                & (df["name"].str.upper().str.contains(sym.replace(" ", ""), na=False))
                & (df["instrumenttype"].fillna("").isin(["", "AMXIDX"]))
            ]
        if sub.empty:
            raise AngelOneError(f"Symbol token not found for {symbol} on {exchange}")
        eq = sub[sub["symbol"].astype(str).str.endswith("-EQ")]
        row = eq.iloc[0] if not eq.empty else sub.iloc[0]
        token = str(row["token"])
        self._token_cache[key] = token
        tsym = str(row.get("symbol") or f"{sym}-EQ")
        kind = "index" if str(row.get("instrumenttype") or "") in ("AMXIDX",) else "equity"
        return {
            "symbol": sym,
            "token": token,
            "exchange": str(row.get("exch_seg") or exchange),
            "tradingsymbol": tsym,
            "kind": kind,
            "name": str(row.get("name") or sym),
        }

    def symbol_token(self, symbol: str, exchange: str = "NSE") -> str:
        key = f"{exchange}:{symbol.upper()}"
        if key in self._token_cache:
            return self._token_cache[key]
        info = self.resolve_instrument(symbol, exchange)
        self._token_cache[key] = info["token"]
        self._token_cache[f"{info['exchange']}:{symbol.upper()}"] = info["token"]
        return info["token"]

    def resolve_symbols(self, symbols: list[str], exchange: str = "NSE") -> list[dict]:
        out = []
        for s in symbols:
            try:
                token = self.symbol_token(s, exchange)
                out.append({"symbol": s.upper(), "token": token, "exchange": exchange})
            except AngelOneError as e:
                logger.warning("%s", e)
        return out

    # --------------------------------------------------------------- candles

    def get_candles(
        self,
        symbol: str,
        from_dt: dt.datetime,
        to_dt: dt.datetime,
        interval: str = "FIVE_MINUTE",
        exchange: str = "NSE",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        self._ensure_session()
        interval = INTERVAL_MAP.get(interval, interval)
        info = self.resolve_instrument(symbol, exchange)
        token = info["token"]
        exchange = info["exchange"]

        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=IST)
        if to_dt.tzinfo is None:
            to_dt = to_dt.replace(tzinfo=IST)

        cache_key = (
            f"{exchange}_{symbol}_{interval}_"
            f"{from_dt.strftime('%Y%m%d%H%M')}_{to_dt.strftime('%Y%m%d%H%M')}.csv"
        )
        cache_path = CACHE_DIR / cache_key
        if use_cache and cache_path.exists():
            df = pd.read_csv(cache_path, parse_dates=["datetime"])
            if df["datetime"].dt.tz is None:
                df["datetime"] = df["datetime"].dt.tz_localize(IST)
            return df

        # Angel historical API has day limits per call; chunk by ~ days
        frames: list[pd.DataFrame] = []
        cursor = from_dt
        max_span = dt.timedelta(days=5 if interval in ("ONE_MINUTE", "THREE_MINUTE") else 30)

        while cursor < to_dt:
            chunk_end = min(cursor + max_span, to_dt)
            params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": cursor.strftime("%Y-%m-%d %H:%M"),
                "todate": chunk_end.strftime("%Y-%m-%d %H:%M"),
            }
            resp = None
            for attempt in range(4):
                self._throttle_candles()
                try:
                    resp = self._smart.getCandleData(params)
                    # SmartAPI sometimes returns rate-limit as a non-JSON / failed body
                    if isinstance(resp, dict) and not resp.get("status"):
                        msg = str(resp.get("message") or resp.get("errorcode") or resp)
                        if _is_rate_limit_error(msg):
                            raise AngelOneError(msg)
                    break
                except Exception as e:
                    if _is_rate_limit_error(e):
                        backoff = 1.5 * (attempt + 1)
                        logger.info(
                            "Rate-limit %s — wait %.1fs (%s/4)",
                            symbol,
                            backoff,
                            attempt + 1,
                        )
                        time.sleep(backoff)
                        resp = None
                        continue
                    # Auth / other errors: re-login once, then retry once
                    if attempt == 0:
                        logger.warning("Candle fetch error, re-login: %s", e)
                        try:
                            self.login(force=True)
                        except Exception as login_err:
                            logger.warning("Re-login failed: %s", login_err)
                        continue
                    logger.warning("Candle fetch failed for %s: %s", symbol, e)
                    resp = None
                    break

            if not resp or not resp.get("status"):
                logger.warning(
                    "No candle data for %s %s->%s: %s",
                    symbol,
                    params["fromdate"],
                    params["todate"],
                    resp,
                )
            else:
                data = resp.get("data") or []
                if data:
                    df = pd.DataFrame(
                        data, columns=["datetime", "open", "high", "low", "close", "volume"]
                    )
                    dt_series = pd.to_datetime(df["datetime"], utc=False)
                    if getattr(dt_series.dt, "tz", None) is None:
                        df["datetime"] = dt_series.dt.tz_localize(
                            IST, ambiguous="infer", nonexistent="shift_forward"
                        )
                    else:
                        df["datetime"] = dt_series.dt.tz_convert(IST)
                    for col in ("open", "high", "low", "close", "volume"):
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                    frames.append(df)
            cursor = chunk_end + dt.timedelta(minutes=1)

        if not frames:
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )

        out = (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["datetime"])
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        if use_cache and not out.empty:
            out.to_csv(cache_path, index=False)
        return out

    def get_session_candles(
        self,
        symbol: str,
        day: dt.date,
        interval: str = "FIVE_MINUTE",
        exchange: str = "NSE",
        use_cache: Optional[bool] = None,
    ) -> pd.DataFrame:
        """
        Session candles 09:15–15:30 IST.
        Past days: cache ON (stable). Today: cache OFF by default so paper ticks stay live.
        """
        start = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
        end = dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST)
        if use_cache is None:
            use_cache = day < now_ist().date()
        return self.get_candles(
            symbol, start, end, interval=interval, exchange=exchange, use_cache=use_cache
        )

    def write_session_cache(
        self,
        symbol: str,
        day: dt.date,
        df: pd.DataFrame,
        interval: str = "FIVE_MINUTE",
        exchange: str = "NSE",
    ) -> None:
        """Persist a completed session slice so later ticks skip Angel for that day."""
        if df is None or df.empty or day >= now_ist().date():
            return
        interval = INTERVAL_MAP.get(interval, interval)
        try:
            info = self.resolve_instrument(symbol, exchange)
            exchange = info["exchange"]
        except Exception:
            exchange = exchange or "NSE"
        start = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
        end = dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST)
        cache_path = CACHE_DIR / (
            f"{exchange}_{symbol}_{interval}_"
            f"{start.strftime('%Y%m%d%H%M')}_{end.strftime('%Y%m%d%H%M')}.csv"
        )
        try:
            out = df.copy()
            if "datetime" in out.columns:
                out.to_csv(cache_path, index=False)
        except Exception as e:
            logger.debug("write_session_cache %s %s: %s", symbol, day, e)

    def get_ltp(self, symbol: str, exchange: str = "NSE") -> float:
        self._ensure_session()
        info = self.resolve_instrument(symbol, exchange)
        resp = self._smart.ltpData(info["exchange"], info["tradingsymbol"], info["token"])
        if not resp or not resp.get("status"):
            raise AngelOneError(f"LTP failed for {symbol}: {resp}")
        return float(resp["data"]["ltp"])

    def get_ltp_by_token(
        self,
        exchange: str,
        tradingsymbol: str,
        token: str,
    ) -> float:
        """LTP for any instrument when token is already known (e.g. NFO options)."""
        self._ensure_session()
        resp = self._smart.ltpData(exchange, tradingsymbol, str(token))
        if not resp or not resp.get("status"):
            raise AngelOneError(f"LTP failed for {tradingsymbol}@{exchange}: {resp}")
        return float(resp["data"]["ltp"])

    def get_candles_by_token(
        self,
        exchange: str,
        token: str,
        from_dt: dt.datetime,
        to_dt: dt.datetime,
        interval: str = "FIVE_MINUTE",
        use_cache: bool = True,
        cache_key: str = "",
    ) -> pd.DataFrame:
        """Historical candles when token is known (NFO options)."""
        self._ensure_session()
        interval = INTERVAL_MAP.get(interval, interval)
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=IST)
        if to_dt.tzinfo is None:
            to_dt = to_dt.replace(tzinfo=IST)

        cache_path = CACHE_DIR / (
            f"{cache_key or token}_{interval}_{from_dt.date()}_{to_dt.date()}.csv"
        )
        if use_cache and cache_path.exists():
            try:
                cached = pd.read_csv(cache_path, parse_dates=["datetime"])
                if not cached.empty:
                    return cached
            except Exception:
                pass

        params = {
            "exchange": exchange,
            "symboltoken": str(token),
            "interval": interval,
            "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
            "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
        }
        try:
            resp = self._smart.getCandleData(params)
        except Exception as e:
            logger.warning("Option candle fetch error: %s", e)
            self.login(force=True)
            resp = self._smart.getCandleData(params)

        if not resp or not resp.get("status"):
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )
        data = resp.get("data") or []
        if not data:
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )
        df = pd.DataFrame(
            data, columns=["datetime", "open", "high", "low", "close", "volume"]
        )
        dt_series = pd.to_datetime(df["datetime"], utc=False)
        if getattr(dt_series.dt, "tz", None) is None:
            df["datetime"] = dt_series.dt.tz_localize(
                IST, ambiguous="infer", nonexistent="shift_forward"
            )
        else:
            df["datetime"] = dt_series.dt.tz_convert(IST)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if use_cache and not df.empty:
            try:
                df.to_csv(cache_path, index=False)
            except Exception:
                pass
        return df

    def get_quote(self, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
        self._ensure_session()
        token = self.symbol_token(symbol, exchange)
        mode = "LTP"
        payload = {
            "mode": mode,
            "exchangeTokens": {exchange: [token]},
        }
        try:
            resp = self._smart.getMarketData(mode, {exchange: [token]})
            return resp or {}
        except Exception:
            return {"ltp": self.get_ltp(symbol, exchange), "symbol": symbol}

    # --------------------------------------------------------------- orders

    def place_order(self, order: dict[str, Any], live_enabled: bool = False) -> dict:
        """Place order only when live_enabled=True. Otherwise refuse."""
        if not live_enabled:
            raise AngelOneError(
                "Live trading is DISABLED. Set live_enabled=true in config/trading.json "
                "only when you intentionally want real orders."
            )
        self._ensure_session()
        return self._smart.placeOrder(order)

    def health(self) -> dict[str, Any]:
        ok_creds = self.configured()
        info: dict[str, Any] = {
            "credentials_present": ok_creds,
            "logged_in": bool(self._jwt),
            "client_code": self.settings.angel_client_code if ok_creds else None,
        }
        if ok_creds:
            try:
                self.login()
                info["logged_in"] = True
                info["profile"] = self._smart.getProfile(self._refresh)
            except Exception as e:
                info["error"] = str(e)
        return info


_client: Optional[AngelOneClient] = None


def get_client(reload: bool = False) -> AngelOneClient:
    global _client
    if _client is None or reload:
        if reload:
            reload_angel_settings()
        _client = AngelOneClient()
    return _client