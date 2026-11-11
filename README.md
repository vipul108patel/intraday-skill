# NSE Intraday Skill

Indian-market adaptation of [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill): **NIFTY Signal v1.0** (EMA/VWAP/OR breakout + retest + confirm), size risk, backtest, and **paper-trade** with **Angel One** data. Live orders are wired but **off by default**.

Not financial advice. Day trading can lose money.

## What you get

- **Setup finder** — NIFTY Signal v1: 15m EMA20 + VWAP bias, OR break/retest/confirm on 5m, CE/PE
- **Risk & reward** — Risk 5–7% capital / trade, profit 10–12%, min 1:2 RR, max 3 trades/day, stop after 2 consecutive losses, 10% daily loss cap
- **Backtesting** — Historical 5m + 15m candles from Angel One, full trade audit
- **Paper trading** — Live Angel quotes/candles, simulated fills, no real orders
- **Live trading** — Configured stubs; blocked until you flip flags
- **Dashboard** — Trades, setups, events, backtest runs at http://127.0.0.1:8787

## Angel One credentials — kahan rakhein

**Project root pe `.env` file** (gitignored). Template:

```bash
cp .env.example .env
```

Fill:

| Variable | Kya hai |
| --- | --- |
| `ANGEL_API_KEY` | SmartAPI app key — https://smartapi.angelone.in/ |
| `ANGEL_CLIENT_CODE` | Angel client id |
| `ANGEL_PASSWORD` | Trading MPIN / PIN |
| `ANGEL_TOTP_SECRET` | TOTP secret from https://smartapi.angelone.in/enable-totp |

Kabhi bhi keys chat / git / dashboard pe paste mat karo. Sirf `.env`.

Risk limits, watchlist, paper/live flags: **`config/trading.json`**.

## Setup

```bash
cd /Users/vipulpatel/Desktop/Projects/intraday-skill
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit credentials
```

### Offline smoke test (no Angel login)

```bash
python scripts/demo_offline.py
```

### Dashboard

```bash
python scripts/run_dashboard.py
# open http://127.0.0.1:8787
```

### Backtest (needs Angel `.env`)

```bash
python scripts/run_backtest.py --days 20
# or fewer symbols:
python scripts/run_backtest.py --days 10 --symbols RELIANCE,TCS,INFY
```

### Paper trading during market hours

```bash
python scripts/run_paper.py          # one scan + manage
python scripts/run_paper.py --loop --every 60
```

Dashboard pe **Run paper tick** / **Run backtest** bhi same kaam karte hain.

## Live trading (abhi band rakho)

`config/trading.json` me:

```json
"paper": true,
"live_enabled": false
```

Real orders tabhi possible jab **dono**: `"paper": false` **aur** `"live_enabled": true`. Uske baad bhi live engine auto-fire nahi karta — explicit `place_bracket` chahiye.

## Entry kaise milta hai (NIFTY Signal v1)

1. Prep 09:15–09:30: ORH/ORL + PDH/PDL/PDC + VWAP
2. Bias: 15m EMA20 bullish/bearish (not flat) + price vs session VWAP
3. 5m close beyond ORH (CE) or ORL (PE); volume > 20-bar avg when volume exists
4. Retest of breakout level holds; then confirmation candle
5. Entry = **next 5m open** after confirmation → ATM CE/PE
6. Stop = retest swing; target ≥ 1:2 toward next S/R
7. Risk gates: 5–7% / trade, max 3 trades, 2-loss streak stop, 10% daily loss
8. Flat by 15:10 IST

Har trade record me milta hai: strategy, why, how, steps, planned levels, fills, exit reason, PnL, R-multiple — backtest, paper, aur (jab on ho) live ke liye.

## Layout

```
config/trading.json     # INR risk, NSE hours, watchlist, paper/live
.env                    # Angel credentials (you create)
src/angelone/           # SmartAPI client + instruments
src/strategies/         # nifty_bos_v1 (default), nifty_signal_v1, exit, risk, pipeline
src/backtest/           # historical replay
src/paper/              # paper engine
src/live/               # live stubs (disabled)
src/db/                 # SQLite trades/setups/events
dashboard/              # FastAPI UI
skills/                 # upstream skill docs + INDIA.md
scripts/                # CLI entrypoints
```

## Disclaimer

Education / tooling only. Angel data can lag; stops are not guarantees; past backtests are not future results.