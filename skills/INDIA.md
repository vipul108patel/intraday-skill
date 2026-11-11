# Indian market adaptation notes

Upstream: [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (US / Yahoo / Alpaca).

This repo remaps the skill chain to NSE + Angel One and runs **NIFTY Intraday Signal Strategy v1.0**.

| US skill default | India (this repo) |
| --- | --- |
| NYSE 09:30–16:00 ET | NSE 09:15–15:30 IST |
| Opening range first 15m from 09:30 | Opening range 09:15–09:30 IST |
| `no_entries_before` 09:45 | 09:30 IST |
| Yahoo charts | Angel One SmartAPI candles (5m + 15m) |
| Alpaca paper | Internal paper engine (default); live Angel orders gated off |
| USD limits | INR capital % in `config/trading.json` |

## Active strategy — `nifty_signal_v1`

```
prep (OR + PDH/PDL/PDC)
  → 15m EMA20 bias + session VWAP
  → 5m OR breakout/breakdown (volume if available)
  → retest of level
  → confirmation candle
  → entry next 5m open → CE/PE
  → exit (retest swing SL, ≥1:2 target)
  → risk (5–7% / 10–12%, max 3 trades, 2-loss streak stop, 10% daily loss)
```

Executable pipeline: `src/strategies/nifty_signal.py` + `pipeline` + `backtest` / `paper` / `live`.

Claude skill folders under `skills/trader-*` still describe the upstream US ORB chain; prefer the Python app for Angel One + NIFTY Signal v1.
