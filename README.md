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
- [x] **`max_pain` is slow** — uses `iterrows()` (O(strikes × contracts) in pure Python). Replace with vectorised NumPy operations.
- [x] **Two separate snapshot round-trips** — underlying spot and option chain snapshots are fetched independently. Batch them into one call.

### Correctness
- [x] **`shortCode` regex breaks on observed Futu codes** — observed codes look like `HK.KST260508C45000`; the current regex expects a numeric underlying prefix and an 8-digit date. Parse `YYMMDD + C/P + scaled strike` formats and display `45000` as `45.00`.
- [x] **Unavailable P/C ratios render as `0.000`** — Python returns `None` when call volume/OI is zero, but JS coerces nullish ratios to zero in the gauge. Render these as `—` / unavailable instead of a real ratio.
- [x] **Replace weak unusual-volume flag with activity / positioning signal** — current live volume alone is not enough to call activity "unusual", especially early in the session when many contracts have zero volume. Compute a backend score for every contract in the fetched expiries using live Futu volume/OI plus HKEX previous trading-day volume, OI, and OI change. Auto-detect the latest previous-day HKEX report by starting from yesterday and searching backward, with optional `--hkex-date YYYYMMDD` and `--no-hkex` controls. Match HKEX rows back to Futu contracts by option class, expiry, strike, and call/put. Add fields such as `prev_volume`, `prev_oi`, `prev_oi_change`, `volume_oi_ratio`, `activity_score`, and `activity_label`. Keep existing Top 5 volume tables sorted by volume, add an `OI Chg` column, and show a fire marker only when `activity_score >= 4` and previous-day OI change is at least 20 contracts.
- [ ] **Add separate Activity Signals section** — add a dedicated table sorted by `activity_hot`, `activity_score`, OI change, and volume so non-Top-5 contracts can surface when positioning activity is meaningful.
- [ ] **Tune activity score by liquidity profile** — current thresholds are global (`live volume >= 100`, `volume >= 2x previous-day volume`, `volume/OI >= 50%`, fire marker when `score >= 4` and `OI Chg >= 20`). Consider different thresholds for liquid names such as `0700` / `9988`, mid-liquidity names such as `1024`, and thinner names such as `293`.
- [ ] **Optional activity threshold CLI controls** — expose thresholds such as `--activity-min-volume`, `--activity-vol-oi-threshold`, `--activity-hot-score`, and `--activity-hot-oi-change` only if manual tuning becomes common.
- [x] **`ticker_display` replacement is a no-op** — `'HK.'.replace('HK.', 'HK.')` does nothing. Fix the intended formatting.
- [x] **`pct` and `sign` JS helpers defined but never used** — dead code, remove.
- [x] **Max pain color class is missing** — HTML assigns class `yellow`, but CSS only defines `--yellow`; add `.yellow { color: var(--yellow); }` or use an existing class.

### Robustness
- [x] **Suspended options distort calculations** — contracts with `suspension=True` should be filtered before max pain and OI chart calculations.
- [x] **Same-day expiries need an expiry-day warning** — keep `option_expiry_date_distance >= 0`, but flag same-day expiries in the dashboard because data can become stale after market close.
- [x] **Empty chain fails silently** — `fetch_chain()` returns an empty DataFrame with no warning; downstream code crashes with an unhelpful error.
- [x] **Missing Futu response columns are not validated** — calculations assume columns like `option_type`, `strike_price`, `volume`, and `option_open_interest` exist. Validate required columns after each API response and raise clear errors.
- [x] **Spot snapshot edge cases are not guarded** — `get_spot()` assumes at least one snapshot row and a non-zero previous close. Handle empty snapshots and `prev_close_price == 0`.
- [x] **Partial option snapshots are silent** — if a batched snapshot returns fewer contracts than requested, the left merge creates missing prices/Greeks without a warning.
- [x] **HKEX parser lacks regression tests** — add unit tests for observed Futu option-code parsing, HKEX class-section parsing, comma numbers, negative OI changes, zero/missing values, and HKEX-to-Futu merge matching.
- [x] **HKEX reports are downloaded repeatedly** — cache downloaded HKEX daily reports under `.cache/hkex` and reuse the cached report before making a network request.

### HTML / Frontend
- [x] **Call wall / put wall / max pain not marked on OI chart** — OI charts now use grouped dotted marker lines with a compact centered legend under the x-axis, avoiding line labels inside the plot.
- [x] **Volume chart x-axis shows rank (1–5) instead of strikes** — replace with actual strike prices so the viewer can see where unusual volume sits relative to spot.
- [x] **Chart.js loaded from CDN** — documented that the dashboard is a single HTML file but requires internet access for Chart.js chart rendering.
- [x] **Generated file URL is not escaped** — `file://{abs_path}` breaks for spaces and special characters. Use `Path(args.output).resolve().as_uri()`.
- [x] **Ticker is inserted into HTML without escaping** — `ticker_display` is derived from CLI input and is interpolated directly into `<title>` and the ticker badge. Escape generated HTML text.
- [x] **Top 5 volume tables include zero-volume contracts** — traded contracts are ranked first by volume, then zero-volume fallback rows are ranked by highest OI.

### Minor
- [x] **Docstring references old filename** — updated from `fetch_options.py` after rename.
- [ ] **`generate_html` is a 370-line f-string** — extract HTML into a separate template or module-level constant for easier editing.
