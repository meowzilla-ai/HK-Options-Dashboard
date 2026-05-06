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

High activity / positioning signals are flagged with 🔥 in the tables only when the backend activity score is 4 or higher and HKEX previous-day OI change is at least 20 contracts. The score uses live Futu volume/OI plus HKEX previous trading-day volume, OI, and OI change when available.

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
    └── Activity signal   — score live volume, volume/OI, previous volume, and OI change
            │
            ▼
    {ticker}-options-{date}.html — single HTML file, no server needed
```

## Known Limitations & Planned Improvements

### Performance

### Correctness
- [ ] **Add separate Activity Signals section** — add a dedicated table sorted by `activity_hot`, `activity_score`, OI change, and volume so non-Top-5 contracts can surface when positioning activity is meaningful.
- [ ] **Tune activity score by liquidity profile** — current thresholds are global (`live volume >= 100`, `volume >= 2x previous-day volume`, `volume/OI >= 50%`, fire marker when `score >= 4` and `OI Chg >= 20`). Consider different thresholds for liquid names such as `0700` / `9988`, mid-liquidity names such as `1024`, and thinner names such as `293`.
- [ ] **Optional activity threshold CLI controls** — expose thresholds such as `--activity-min-volume`, `--activity-vol-oi-threshold`, `--activity-hot-score`, and `--activity-hot-oi-change` only if manual tuning becomes common.


### Robustness


### HTML / Frontend


### Minor
- [ ] **`generate_html` is a 370-line f-string** — extract HTML into a separate template or module-level constant for easier editing.
