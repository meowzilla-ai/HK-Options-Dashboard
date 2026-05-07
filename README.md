# HK Stock Options Dashboard

A local dashboard generator for Hong Kong stock options. Fetches live data from FutuOpenD, calculates key options metrics, and produces a single dark-theme HTML file you can open in any browser. The HTML file loads Chart.js from a CDN, so chart rendering requires internet access.

## Requirements

- [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html) running locally (default port 11111)
- Python 3.9+
- `futu-api`, `pandas`, and `numpy` (`pip install futu-api pandas numpy`)

## Usage

```bash
python3 options-dashboard.py --code HK.00700
```

Open the output in your browser:

```bash
open HK00700-options-YYYY-MM-DD.html
```

By default, the output file is named `{ticker}-options-{date}.html`, for example `HK00700-options-2026-05-06.html`. Use `--output` to choose a specific path.

### Options

| Flag | Default | Description |
|---|---|---|
| `--code` | `HK.00700` | HK stock code |
| `--host` | `127.0.0.1` | FutuOpenD host |
| `--port` | `11111` | FutuOpenD port |
| `--output` | `{ticker}-options-{date}.html` | Output file path |
| `--hkex-date` | latest previous-day report | HKEX daily report date for previous-day OI change, `YYYYMMDD` or `YYMMDD` |
| `--no-hkex` | `False` | Skip HKEX previous-day activity enrichment |
| `--no-futu-commentary` | `False` | Skip Futu Derivative Abnormal Activity commentary |
| `--commentary-language` | `2` | Futu commentary language: `0` simplified Chinese, `1` traditional Chinese, `2` English |
| `--commentary-time-range` | `7` | Futu commentary time range in natural days |

## What It Shows

### Header
- Ticker badge and last updated timestamp

### Key Stats (8 cards)
- Underlying price and daily change %
- Call wall and put wall (current expiry)
- ATM strike and max pain (current expiry)
- P/C ratio by volume and by OI (current expiry)

### Per Expiry (Current + Next)
- **Key levels:** call wall, put wall, ATM, max pain, total call/put OI
- **P/C ratio gauges:** visual bar showing put/call balance by volume and OI
- **OI profile chart:** calls (green, upward) vs puts (red, downward) by strike
- **Volume distribution chart:** top 5 call vs put volumes side by side
- **Top 5 calls table:** strike, volume, OI, OI change, last, bid, ask, IV, Δ, Γ, Θ
- **Top 5 puts table:** same columns, sorted by volume descending

High activity / positioning signals are flagged with 🔥 in the tables when the backend activity score is 4 or higher. The score uses live Futu volume, same-side live-volume percentiles, volume/OI, and HKEX previous-day OI change when available.
Futu Derivative Abnormal Activity is shown as vendor-provided commentary when available.

## How It Works

```
FutuOpenD
    │
    ├── get_option_expiration_date()  → pick nearest 2 future expiries
    ├── get_option_chain()            → list of contracts + strike prices
    └── get_market_snapshot()        → volume, OI, Greeks, bid/ask, last price
            │
            ├── HKEX daily report     → previous-day volume, OI, and OI change
            │
            ▼
    Calculations (Python)
    ├── Max pain          — strike minimising total intrinsic value for buyers
    ├── Call / put wall   — strike with highest OI per side
    ├── ATM strike        — closest strike to spot
    ├── P/C ratios        — put ÷ call for volume and OI separately
    └── Activity signal   — score live volume, side percentiles, volume/OI, and OI change
            │
            ▼
    {ticker}-options-{date}.html — single HTML file, no server needed
```

## Known Limitations & Planned Improvements

### Performance


### Correctness
- [ ] **Skip stock-level Futu financial/technical commentary for now** — `get_financial_unusual()` and `get_technical_unusual()` work, but they are stock-level context and may dilute the options-focused dashboard.


### Robustness


### HTML / Frontend

### Minor
- [ ] **`generate_html` is a large f-string** — extract HTML into a separate template or module-level constant for easier editing.

