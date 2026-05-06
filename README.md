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
- **Top 5 calls table:** strike, volume, OI, last, bid, ask, IV, Δ, Γ, Θ
- **Top 5 puts table:** same columns, sorted by volume descending

Unusual volume (> 2× median for same option type) is flagged with 🔥 in the tables.

## How It Works

```
FutuOpenD
    │
    ├── get_option_expiration_date()  → pick nearest 2 future expiries
    ├── get_option_chain()            → list of contracts + strike prices
    └── get_market_snapshot()        → volume, OI, Greeks, bid/ask, last price
            │
            ▼
    Calculations (Python)
    ├── Max pain          — strike minimising total intrinsic value for buyers
    ├── Call / put wall   — strike with highest OI per side
    ├── ATM strike        — closest strike to spot
    ├── P/C ratios        — put ÷ call for volume and OI separately
    └── Unusual volume    — volume > 2× median for same option type
            │
            ▼
    {ticker}-options-{date}.html — single HTML file, no server needed
```

## Known Limitations & Planned Improvements

### Performance


### Correctness
- [ ] **Unusual volume flag skips chains where median volume = 0** — common in thinly traded expiries. Use an absolute minimum threshold instead of a multiplier on zero.


### Robustness


### HTML / Frontend


### Minor
- [ ] **`generate_html` is a 370-line f-string** — extract HTML into a separate template or module-level constant for easier editing.
