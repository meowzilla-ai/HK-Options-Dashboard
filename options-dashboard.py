#!/usr/bin/env python3
"""
HK Stock Options Dashboard Generator
Usage: python3 options-dashboard.py [--code HK.00700] [--host 127.0.0.1] [--port 11111] [--output dashboard.html]
Requires FutuOpenD running locally.
"""

import json
import argparse
from datetime import datetime
from html import escape
from pathlib import Path
import numpy as np
import pandas as pd
from futu import OpenQuoteContext, RET_OK

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 11111


def default_output_path(code):
    ticker = ''.join(ch for ch in code if ch.isalnum())
    date = datetime.now().strftime('%Y-%m-%d')
    return f"{ticker}-options-{date}.html"


def require_columns(df, columns, label):
    """Raise a clear error when a Futu response is missing required columns."""
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise RuntimeError(f"{label} missing required columns: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_snapshots(ctx, codes, label):
    """Fetch market snapshots in Futu's 200-code batches."""
    codes = list(dict.fromkeys(codes))
    if not codes:
        raise RuntimeError(f"No codes supplied for {label} snapshot")

    snap_parts = []
    for i in range(0, len(codes), 200):
        batch = codes[i:i+200]
        ret, snap = ctx.get_market_snapshot(batch)
        if ret != RET_OK:
            raise RuntimeError(f"Snapshot batch failed for {label}: {snap}")
        if snap.empty:
            raise RuntimeError(f"Empty snapshot batch for {label}")
        require_columns(snap, ['code'], f"Snapshot batch for {label}")
        snap_parts.append(snap)

    return pd.concat(snap_parts, ignore_index=True)


def get_spot_from_snapshot(snapshot, code):
    require_columns(snapshot, ['code', 'last_price', 'prev_close_price'], f"Spot snapshot for {code}")
    rows = snapshot[snapshot['code'] == code]
    if rows.empty:
        raise RuntimeError(f"No spot snapshot returned for {code}")

    row = rows.iloc[0]
    price = pd.to_numeric(row['last_price'], errors='coerce')
    prev = pd.to_numeric(row['prev_close_price'], errors='coerce')
    if pd.isna(price):
        raise RuntimeError(f"Spot snapshot for {code} has no valid last_price")
    if pd.isna(prev):
        raise RuntimeError(f"Spot snapshot for {code} has no valid prev_close_price")
    if prev == 0:
        raise RuntimeError(f"Spot snapshot for {code} has prev_close_price=0; cannot calculate daily change")

    price = float(price)
    prev = float(prev)
    return price, (price - prev) / prev * 100


def get_expiries(ctx, code):
    ret, data = ctx.get_option_expiration_date(code)
    if ret != RET_OK:
        raise RuntimeError(f"Expiry fetch failed: {data}")
    if data.empty:
        raise RuntimeError(f"No option expiries returned for {code}")
    require_columns(data, ['option_expiry_date_distance', 'strike_time'], f"Expiry response for {code}")

    future = data[data['option_expiry_date_distance'] >= 0].sort_values('option_expiry_date_distance')
    if len(future) < 2:
        raise RuntimeError("Fewer than 2 future expiry dates available")

    def to_expiry(row):
        return {
            'date': row['strike_time'],
            'days': int(row['option_expiry_date_distance']),
            'is_expiry_today': int(row['option_expiry_date_distance']) == 0,
        }

    return to_expiry(future.iloc[0]), to_expiry(future.iloc[1])


def fetch_chain(ctx, code, expiry_date):
    """Fetch option chain metadata for one expiry date."""
    ret, chain = ctx.get_option_chain(code, start=expiry_date, end=expiry_date)
    if ret != RET_OK:
        raise RuntimeError(f"Option chain failed: {chain}")
    if chain.empty:
        raise RuntimeError(f"No option chain returned for {code} expiry {expiry_date}")
    require_columns(chain, ['code', 'option_type', 'strike_price'], f"Option chain for {code} expiry {expiry_date}")

    return chain


def merge_chain_snapshot(chain, snapshot, code, expiry_date):
    """Enrich an option chain with already-fetched snapshot data."""
    require_columns(snapshot, ['code', 'volume', 'option_open_interest'], f"Option snapshot for {code} expiry {expiry_date}")

    codes = chain['code'].tolist()
    option_snapshot = snapshot[snapshot['code'].isin(codes)]
    returned_codes = set(option_snapshot['code'])
    missing_codes = [c for c in codes if c not in returned_codes]
    if missing_codes:
        sample = ', '.join(missing_codes[:5])
        suffix = '...' if len(missing_codes) > 5 else ''
        raise RuntimeError(
            f"Option snapshot for {code} expiry {expiry_date} missing "
            f"{len(missing_codes)} contracts: {sample}{suffix}"
        )

    # Keep only the columns we need from snapshot
    want = ['code', 'last_price', 'bid_price', 'ask_price', 'volume',
            'option_open_interest', 'option_implied_volatility',
            'option_delta', 'option_gamma', 'option_vega', 'option_theta']
    want = [c for c in want if c in option_snapshot.columns]
    merged = chain.merge(option_snapshot[want], on='code', how='left')

    # Numeric coercion
    num_cols = ['strike_price', 'volume', 'option_open_interest',
                'last_price', 'bid_price', 'ask_price',
                'option_implied_volatility', 'option_delta',
                'option_gamma', 'option_vega', 'option_theta']
    for col in num_cols:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors='coerce')

    merged['volume'] = merged['volume'].fillna(0)
    merged['option_open_interest'] = merged['option_open_interest'].fillna(0)
    if merged['strike_price'].dropna().empty:
        raise RuntimeError(f"Option chain for {code} expiry {expiry_date} has no valid strike prices")
    return merged


def filter_suspended(df, expiry_date):
    """Remove suspended contracts before calculations and display."""
    if 'suspension' not in df.columns:
        return df, 0

    suspended = df['suspension'].fillna(False).astype(bool)
    active = df.loc[~suspended].copy()
    removed = int(suspended.sum())
    if active.empty:
        raise RuntimeError(f"All option contracts are suspended for expiry {expiry_date}")
    if active['strike_price'].dropna().empty:
        raise RuntimeError(f"No active option contracts with valid strikes for expiry {expiry_date}")
    return active, removed


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def max_pain(df):
    """Strike that minimises total intrinsic value held by option buyers."""
    strikes = np.sort(pd.to_numeric(df['strike_price'], errors='coerce').dropna().unique())
    if strikes.size == 0:
        raise RuntimeError("Cannot calculate max pain without valid strikes")

    calls = df[df['option_type'] == 'CALL']
    puts = df[df['option_type'] == 'PUT']

    call_strikes = calls['strike_price'].to_numpy(dtype=float)
    call_oi = calls['option_open_interest'].fillna(0).to_numpy(dtype=float)
    put_strikes = puts['strike_price'].to_numpy(dtype=float)
    put_oi = puts['option_open_interest'].fillna(0).to_numpy(dtype=float)

    totals = np.zeros(strikes.size, dtype=float)
    if call_strikes.size:
        totals += (np.maximum(strikes[:, None] - call_strikes[None, :], 0) * call_oi[None, :]).sum(axis=1)
    if put_strikes.size:
        totals += (np.maximum(put_strikes[None, :] - strikes[:, None], 0) * put_oi[None, :]).sum(axis=1)

    return float(strikes[int(totals.argmin())])


def key_levels(df, spot):
    calls = df[df['option_type'] == 'CALL']
    puts  = df[df['option_type'] == 'PUT']

    call_oi_by_strike = calls.groupby('strike_price')['option_open_interest'].sum()
    put_oi_by_strike  = puts.groupby('strike_price')['option_open_interest'].sum()

    call_wall = float(call_oi_by_strike.idxmax()) if not call_oi_by_strike.empty else None
    put_wall  = float(put_oi_by_strike.idxmax())  if not put_oi_by_strike.empty  else None

    atm = float(df['strike_price'].iloc[
        (df['strike_price'] - spot).abs().argsort().iloc[0]
    ])

    mp = max_pain(df)

    call_vol = float(calls['volume'].sum())
    put_vol  = float(puts['volume'].sum())
    call_oi  = float(calls['option_open_interest'].sum())
    put_oi   = float(puts['option_open_interest'].sum())

    return {
        'call_wall':    call_wall,
        'put_wall':     put_wall,
        'atm_strike':   atm,
        'max_pain':     mp,
        'pc_ratio_vol': round(put_vol / call_vol, 3)  if call_vol > 0 else None,
        'pc_ratio_oi':  round(put_oi  / call_oi,  3)  if call_oi  > 0 else None,
        'call_vol': call_vol, 'put_vol': put_vol,
        'call_oi':  call_oi,  'put_oi':  put_oi,
    }


def flag_unusual(df, multiplier=2.0):
    df = df.copy()
    df['unusual_vol'] = False
    for t in ['CALL', 'PUT']:
        mask = df['option_type'] == t
        median = df.loc[mask, 'volume'].median()
        if pd.notna(median) and median > 0:
            df.loc[mask, 'unusual_vol'] = df.loc[mask, 'volume'] > multiplier * median
    return df


def top5_by_vol(df, opt_type):
    side = df[df['option_type'] == opt_type].copy()
    if side.empty:
        return side

    side['has_volume'] = side['volume'] > 0
    return (side.sort_values(
                ['has_volume', 'volume', 'option_open_interest'],
                ascending=[False, False, False]
            )
            .head(5)
            .drop(columns=['has_volume']))


def oi_chart_data(df):
    calls = df[df['option_type'] == 'CALL'].groupby('strike_price')['option_open_interest'].sum()
    puts  = df[df['option_type'] == 'PUT'].groupby('strike_price')['option_open_interest'].sum()
    all_strikes = sorted(set(calls.index) | set(puts.index))
    return {
        'strikes':  [float(s) for s in all_strikes],
        'call_oi':  [float(calls.get(s, 0)) for s in all_strikes],
        'put_oi':   [float(puts.get(s, 0))  for s in all_strikes],
    }


def to_records(df):
    cols = ['code', 'strike_price', 'volume', 'option_open_interest',
            'last_price', 'bid_price', 'ask_price',
            'option_implied_volatility', 'option_delta',
            'option_gamma', 'option_vega', 'option_theta', 'unusual_vol']
    cols = [c for c in cols if c in df.columns]
    records = []
    for _, row in df[cols].iterrows():
        rec = {}
        for c in cols:
            v = row[c]
            if pd.isna(v) if not isinstance(v, bool) else False:
                rec[c] = None
            elif isinstance(v, bool):
                rec[c] = bool(v)
            elif isinstance(v, (int, float)):
                rec[c] = round(float(v), 4)
            else:
                rec[c] = v
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(data: dict) -> str:
    json_data = json.dumps(data, ensure_ascii=False, indent=2).replace('</', '<\\/')
    ticker_display = escape(data['ticker'])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Options Dashboard — {ticker_display}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --text:     #e6edf3;
    --muted:    #8b949e;
    --green:    #3fb950;
    --red:      #f85149;
    --yellow:   #d29922;
    --blue:     #58a6ff;
    --purple:   #bc8cff;
    --card-bg:  #1c2128;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; }}

  /* Header */
  .header {{ background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }}
  .header h1 {{ font-size: 18px; font-weight: 600; letter-spacing: .3px; }}
  .header .meta {{ color: var(--muted); font-size: 12px; }}
  .header .ticker-badge {{ background: #1f6feb33; border: 1px solid #1f6feb; color: var(--blue); padding: 2px 10px; border-radius: 20px; font-size: 12px; margin-left: 12px; }}

  /* Main layout */
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px 24px; }}

  /* Stat cards */
  .cards {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }}
  .card .label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }}
  .card .value {{ font-size: 20px; font-weight: 600; }}
  .card .sub   {{ color: var(--muted); font-size: 11px; margin-top: 4px; }}
  .up   {{ color: var(--green); }}
  .down {{ color: var(--red);   }}
  .yellow {{ color: var(--yellow); }}
  .neutral {{ color: var(--blue); }}

  /* Section headers */
  .section-title {{ font-size: 14px; font-weight: 600; margin: 28px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }}
  .section-title.first {{ margin-top: 0; }}
  .expiry-badge {{ font-size: 11px; color: var(--muted); background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 2px 8px; }}
  .today-badge {{ font-size: 10px; color: var(--yellow); background: #d2992233; border: 1px solid var(--yellow); border-radius: 12px; padding: 2px 8px; text-transform: uppercase; letter-spacing: .3px; }}
  .expiry-notice {{ display: none; color: var(--yellow); background: #d299221a; border: 1px solid #d2992266; border-radius: 8px; padding: 8px 10px; margin: -4px 0 14px; font-size: 12px; }}
  .expiry-notice.show {{ display: block; }}

  /* Charts */
  .charts-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 28px; }}
  .chart-box {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .chart-box h3 {{ font-size: 12px; color: var(--muted); margin-bottom: 12px; text-transform: uppercase; letter-spacing: .4px; }}
  .level-legend {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 6px; margin: 8px 0 0; }}
  .level-legend-item {{ display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; border: 1px solid var(--border); border-radius: 6px; padding: 3px 7px; background: var(--surface); }}
  .level-legend-line {{ width: 18px; border-top: 2px dashed currentColor; }}
  .chart-box canvas {{ max-height: 220px; }}

  /* Levels row */
  .levels-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
  .levels-box {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }}
  .levels-box h3 {{ font-size: 12px; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: .4px; }}
  .levels-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }}
  .level-item .lbl {{ color: var(--muted); font-size: 10px; text-transform: uppercase; margin-bottom: 2px; }}
  .level-item .val {{ font-size: 14px; font-weight: 600; }}

  /* Tables */
  .tables-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 28px; }}
  .table-box {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  .table-box h3 {{ padding: 10px 14px; font-size: 12px; text-transform: uppercase; letter-spacing: .4px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }}
  .call-header {{ color: var(--green); }}
  .put-header  {{ color: var(--red);   }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ padding: 7px 10px; text-align: right; color: var(--muted); font-weight: 500; font-size: 10px; text-transform: uppercase; border-bottom: 1px solid var(--border); background: var(--surface); }}
  th:first-child {{ text-align: left; }}
  td {{ padding: 7px 10px; text-align: right; border-bottom: 1px solid #21262d; }}
  td:first-child {{ text-align: left; font-family: monospace; font-size: 11px; color: var(--muted); }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #21262d; }}
  .unusual {{ color: var(--yellow); }}
  .unusual::after {{ content: ' 🔥'; }}

  /* P/C ratio bar */
  .pc-bar-wrap {{ margin-top: 10px; }}
  .pc-bar-label {{ display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 4px; }}
  .pc-bar {{ height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; }}
  .pc-bar-fill {{ height: 100%; transition: width .3s; }}

  @media (max-width: 900px) {{
    .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .charts-row, .levels-row, .tables-row {{ grid-template-columns: 1fr; }}
  }}
  @media (max-width: 520px) {{
    .cards {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:12px">
    <h1>Stock Options Dashboard</h1>
    <span class="ticker-badge">{ticker_display}</span>
  </div>
  <div class="meta">Last Updated: <strong id="last-updated"></strong></div>
</div>

<div class="container">

  <!-- Key stats -->
  <div class="section-title first">Key Statistics</div>
  <div class="cards" id="stat-cards"></div>

  <!-- Current expiry -->
  <div class="section-title">
    Current Expiry
    <span class="expiry-badge" id="current-expiry-label"></span>
    <span class="today-badge" id="current-expiry-today" style="display:none">Expires Today</span>
  </div>
  <div class="expiry-notice" id="current-expiry-notice"></div>
  <div class="levels-row">
    <div class="levels-box">
      <h3>Key Levels</h3>
      <div class="levels-grid" id="current-levels"></div>
    </div>
    <div class="levels-box">
      <h3>Put / Call Ratios</h3>
      <div id="current-pc"></div>
    </div>
  </div>
  <div class="charts-row">
    <div class="chart-box">
      <h3>OI Profile — Current Expiry</h3>
      <canvas id="chart-current"></canvas>
      <div class="level-legend" id="legend-current"></div>
    </div>
    <div class="chart-box" style="display:flex;flex-direction:column;justify-content:center;">
      <h3>Volume Distribution</h3>
      <canvas id="chart-current-vol"></canvas>
    </div>
  </div>
  <div class="tables-row">
    <div class="table-box">
      <h3><span class="call-header">▲ Top 5 Calls</span></h3>
      <table id="tbl-cur-calls"></table>
    </div>
    <div class="table-box">
      <h3><span class="put-header">▼ Top 5 Puts</span></h3>
      <table id="tbl-cur-puts"></table>
    </div>
  </div>

  <!-- Next expiry -->
  <div class="section-title">
    Next Expiry
    <span class="expiry-badge" id="next-expiry-label"></span>
    <span class="today-badge" id="next-expiry-today" style="display:none">Expires Today</span>
  </div>
  <div class="expiry-notice" id="next-expiry-notice"></div>
  <div class="levels-row">
    <div class="levels-box">
      <h3>Key Levels</h3>
      <div class="levels-grid" id="next-levels"></div>
    </div>
    <div class="levels-box">
      <h3>Put / Call Ratios</h3>
      <div id="next-pc"></div>
    </div>
  </div>
  <div class="charts-row">
    <div class="chart-box">
      <h3>OI Profile — Next Expiry</h3>
      <canvas id="chart-next"></canvas>
      <div class="level-legend" id="legend-next"></div>
    </div>
    <div class="chart-box" style="display:flex;flex-direction:column;justify-content:center;">
      <h3>Volume Distribution</h3>
      <canvas id="chart-next-vol"></canvas>
    </div>
  </div>
  <div class="tables-row">
    <div class="table-box">
      <h3><span class="call-header">▲ Top 5 Calls</span></h3>
      <table id="tbl-nxt-calls"></table>
    </div>
    <div class="table-box">
      <h3><span class="put-header">▼ Top 5 Puts</span></h3>
      <table id="tbl-nxt-puts"></table>
    </div>
  </div>

</div><!-- /container -->

<script>
const DATA = {json_data};

// ── helpers ──────────────────────────────────────────────────────────────────
const fmt  = (v, d=2) => v == null ? '—' : Number(v).toFixed(d);
const fmtK = (v)      => v == null ? '—' : v >= 1000 ? (v/1000).toFixed(1)+'K' : Number(v).toFixed(0);
const ratioClass = (v) => v == null ? 'neutral' : v > 1 ? 'down' : 'up';
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));

function shortCode(code) {{
  // Observed Futu format: HK.KST260508C45000 -> C45.00 08May26.
  const months = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const fmtDate = (date) => {{
    const yy = date.length === 6 ? date.slice(0, 2) : date.slice(2, 4);
    const mm = date.length === 6 ? date.slice(2, 4) : date.slice(4, 6);
    const dd = date.length === 6 ? date.slice(4, 6) : date.slice(6, 8);
    return `${{dd}}${{months[Number(mm)] || mm}}${{yy}}`;
  }};
  const fmtStrike = (strike) => {{
    const n = Number(strike);
    if (!Number.isFinite(n)) return strike;
    return (n >= 1000 ? n / 1000 : n).toFixed(2);
  }};

  let m = code.match(/^[A-Z]+\\.[A-Z]+(\\d{{6}})([CP])(\\d+)$/);
  if (m) {{
    const [, date, type, strike] = m;
    return `${{type}}${{fmtStrike(strike)}} ${{fmtDate(date)}}`;
  }}

  m = code.match(/\\.?(\\d+)([CP])(\\d{{8}})(\\d+)$/);
  if (m) {{
    const [, , type, date, strike] = m;
    return `${{type}}${{fmtStrike(strike)}} ${{fmtDate(date)}}`;
  }}

  return code;
}}

// ── stat cards ────────────────────────────────────────────────────────────────
document.getElementById('last-updated').textContent = DATA.last_updated;

const changeClass = DATA.change_pct >= 0 ? 'up' : 'down';
const cards = [
  {{ label:'Underlying Price', value: fmt(DATA.spot), sub: DATA.ticker, cls: 'neutral' }},
  {{ label:'Daily Change',     value: (DATA.change_pct >= 0 ? '+' : '') + fmt(DATA.change_pct) + '%', sub: 'vs prev close', cls: changeClass }},
  {{ label:'P/C Ratio (Vol)',  value: fmt(DATA.current.levels.pc_ratio_vol, 3), sub: 'put vol ÷ call vol', cls: ratioClass(DATA.current.levels.pc_ratio_vol) }},
  {{ label:'P/C Ratio (OI)',   value: fmt(DATA.current.levels.pc_ratio_oi, 3),  sub: 'put OI ÷ call OI',  cls: ratioClass(DATA.current.levels.pc_ratio_oi) }},
  {{ label:'Call Wall',        value: fmt(DATA.current.levels.call_wall), sub: 'current expiry', cls: 'up' }},
  {{ label:'Put Wall',         value: fmt(DATA.current.levels.put_wall),  sub: 'current expiry', cls: 'down' }},
  {{ label:'Max Pain',         value: fmt(DATA.current.levels.max_pain),  sub: 'current expiry', cls: 'yellow' }},
  {{ label:'ATM Strike',       value: fmt(DATA.current.levels.atm_strike), sub: 'current expiry', cls: 'neutral' }},
];

document.getElementById('stat-cards').innerHTML = cards.map(c => `
  <div class="card">
    <div class="label">${{esc(c.label)}}</div>
    <div class="value ${{c.cls}}">${{esc(c.value)}}</div>
    <div class="sub">${{esc(c.sub)}}</div>
  </div>`).join('');

// ── key levels block ──────────────────────────────────────────────────────────
function renderLevels(containerId, levels) {{
  const items = [
    {{ lbl:'Call Wall', val: fmt(levels.call_wall), cls:'up'      }},
    {{ lbl:'Put Wall',  val: fmt(levels.put_wall),  cls:'down'    }},
    {{ lbl:'ATM',       val: fmt(levels.atm_strike),cls:'neutral' }},
    {{ lbl:'Max Pain',  val: fmt(levels.max_pain),  cls:'yellow'  }},
    {{ lbl:'Call OI',   val: fmtK(levels.call_oi),  cls:''        }},
    {{ lbl:'Put OI',    val: fmtK(levels.put_oi),   cls:''        }},
  ];
  document.getElementById(containerId).innerHTML = items.map(i => `
    <div class="level-item">
      <div class="lbl">${{i.lbl}}</div>
      <div class="val ${{i.cls}}">${{i.val}}</div>
    </div>`).join('');
}}

function renderPC(containerId, levels) {{
  const volRatio = levels.pc_ratio_vol;
  const oiRatio  = levels.pc_ratio_oi;
  const volPct   = volRatio == null ? 0 : Math.min(volRatio / (volRatio + 1) * 100, 100);
  const oiPct    = oiRatio  == null ? 0 : Math.min(oiRatio  / (oiRatio  + 1) * 100, 100);
  const volColor = volRatio == null ? 'var(--muted)' : volRatio > 1 ? 'var(--red)' : 'var(--green)';
  const oiColor  = oiRatio  == null ? 'var(--muted)' : oiRatio  > 1 ? 'var(--red)' : 'var(--green)';
  const el = document.getElementById(containerId);
  el.innerHTML = `
    <div style="margin-bottom:14px">
      <div class="pc-bar-label"><span>By Volume</span><span class="${{ratioClass(volRatio)}}">${{fmt(volRatio,3)}}</span></div>
      <div class="pc-bar"><div class="pc-bar-fill" style="width:${{volPct}}%;background:${{volColor}}"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:3px">
        <span>Call ${{fmtK(levels.call_vol)}}</span><span>Put ${{fmtK(levels.put_vol)}}</span>
      </div>
    </div>
    <div>
      <div class="pc-bar-label"><span>By OI</span><span class="${{ratioClass(oiRatio)}}">${{fmt(oiRatio,3)}}</span></div>
      <div class="pc-bar"><div class="pc-bar-fill" style="width:${{oiPct}}%;background:${{oiColor}}"></div></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:3px">
        <span>Call OI ${{fmtK(levels.call_oi)}}</span><span>Put OI ${{fmtK(levels.put_oi)}}</span>
      </div>
    </div>`;
}}

function renderExpiryMeta(prefix, section) {{
  document.getElementById(`${{prefix}}-expiry-label`).textContent = section.expiry;

  const badge = document.getElementById(`${{prefix}}-expiry-today`);
  const notice = document.getElementById(`${{prefix}}-expiry-notice`);
  if (section.is_expiry_today) {{
    badge.style.display = 'inline-block';
    notice.textContent = 'This expiry is today; option prices, open interest, and volume may become stale after market close.';
    notice.classList.add('show');
  }}
}}

renderLevels('current-levels', DATA.current.levels);
renderPC('current-pc', DATA.current.levels);
renderLevels('next-levels', DATA.next.levels);
renderPC('next-pc', DATA.next.levels);

renderExpiryMeta('current', DATA.current);
renderExpiryMeta('next', DATA.next);

// ── OI profile charts ─────────────────────────────────────────────────────────
function levelMarkers(chartData, levels) {{
  const markerIndex = strike => chartData.strikes.findIndex(s => Number(s) === Number(strike));
  const rawMarkers = [
    {{ label: 'Call Wall', strike: levels.call_wall, idx: markerIndex(levels.call_wall), color: '#3fb950' }},
    {{ label: 'Put Wall',  strike: levels.put_wall,  idx: markerIndex(levels.put_wall),  color: '#f85149' }},
    {{ label: 'Max Pain',  strike: levels.max_pain,  idx: markerIndex(levels.max_pain),  color: '#d29922' }},
  ].filter(marker => marker.strike != null && marker.idx >= 0);

  const grouped = new Map();
  rawMarkers.forEach(marker => {{
    const key = String(marker.idx);
    if (!grouped.has(key)) {{
      grouped.set(key, {{ ...marker, labels: [marker.label] }});
      return;
    }}

    const existing = grouped.get(key);
    existing.labels.push(marker.label);
    existing.label = existing.labels.join(' + ');
    existing.color = '#bc8cff';
  }});
  return Array.from(grouped.values());
}}

function renderLevelLegend(containerId, markers) {{
  document.getElementById(containerId).innerHTML = markers.map(marker => `
    <span class="level-legend-item" style="color:${{marker.color}}">
      <span class="level-legend-line"></span>
      <span>${{marker.label}} ${{fmt(marker.strike)}}</span>
    </span>
  `).join('');
}}

const levelLinePlugin = {{
  id: 'levelLines',
  afterDatasetsDraw(chart, args, pluginOptions) {{
    const markers = pluginOptions?.markers || [];
    if (!markers.length) return;

    const {{ ctx, chartArea, scales }} = chart;
    const xScale = scales.x;

    ctx.save();
    markers.forEach(marker => {{
      const x = xScale.getPixelForValue(marker.idx);
      if (!Number.isFinite(x)) return;

      ctx.strokeStyle = marker.color;
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
    }});
    ctx.restore();
  }}
}};
Chart.register(levelLinePlugin);

function makeOIChart(canvasId, chartData, levels) {{
  const markers = levelMarkers(chartData, levels);
  new Chart(document.getElementById(canvasId), {{
    type: 'bar',
    data: {{
      labels: chartData.strikes,
      datasets: [
        {{ label: 'Call OI', data: chartData.call_oi, backgroundColor: '#3fb95066', borderColor: '#3fb950', borderWidth: 1 }},
        {{ label: 'Put OI',  data: chartData.put_oi.map(v => -v), backgroundColor: '#f8514966', borderColor: '#f85149', borderWidth: 1 }},
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: true,
      interaction: {{ mode: 'index' }},
      plugins: {{
        legend: {{ labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            label: ctx => `${{ctx.dataset.label}}: ${{Math.abs(ctx.raw).toLocaleString()}}`,
          }}
        }},
        levelLines: {{ markers }},
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e', maxRotation: 45, font: {{ size: 10 }} }}, grid: {{ color: '#21262d' }} }},
        y: {{ ticks: {{ color: '#8b949e', callback: v => fmtK(Math.abs(v)), font: {{ size: 10 }} }}, grid: {{ color: '#21262d' }} }},
      }}
    }}
  }});
}}

function makeVolChart(canvasId, top5calls, top5puts) {{
  const labels   = [];
  const callVols = [];
  const putVols  = [];
  const maxLen   = Math.max(top5calls.length, top5puts.length);
  for (let i = 0; i < maxLen; i++) {{
    const c = top5calls[i], p = top5puts[i];
    const callStrike = c ? `C${{fmt(c.strike_price)}}` : '—';
    const putStrike = p ? `P${{fmt(p.strike_price)}}` : '—';
    labels.push(`${{callStrike}} / ${{putStrike}}`);
    callVols.push(c ? c.volume : 0);
    putVols.push(p  ? p.volume  : 0);
  }}
  new Chart(document.getElementById(canvasId), {{
    type: 'bar',
    data: {{
      labels,
      datasets: [
        {{ label: 'Call Vol', data: callVols, backgroundColor: '#3fb95088' }},
        {{ label: 'Put Vol',  data: putVols,  backgroundColor: '#f8514988' }},
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: true,
      plugins: {{
        legend: {{ labels: {{ color: '#8b949e', font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            title: items => items[0]?.label || '',
            label: ctx => `${{ctx.dataset.label}}: ${{fmtK(ctx.raw)}}`,
          }}
        }},
      }},
      scales: {{
        x: {{ ticks: {{ color: '#8b949e', maxRotation: 45, font: {{ size: 10 }} }}, grid: {{ color: '#21262d' }} }},
        y: {{ ticks: {{ color: '#8b949e', callback: v => fmtK(v), font: {{ size: 10 }} }}, grid: {{ color: '#21262d' }} }},
      }}
    }}
  }});
}}

renderLevelLegend('legend-current', levelMarkers(DATA.current.oi_chart, DATA.current.levels));
renderLevelLegend('legend-next', levelMarkers(DATA.next.oi_chart, DATA.next.levels));
makeOIChart('chart-current', DATA.current.oi_chart, DATA.current.levels);
makeVolChart('chart-current-vol', DATA.current.top5_calls, DATA.current.top5_puts);
makeOIChart('chart-next', DATA.next.oi_chart, DATA.next.levels);
makeVolChart('chart-next-vol', DATA.next.top5_calls, DATA.next.top5_puts);

// ── option tables ─────────────────────────────────────────────────────────────
const TABLE_HEAD = `<thead><tr>
  <th>Code</th><th>Strike</th><th>Vol</th><th>OI</th>
  <th>Last</th><th>Bid</th><th>Ask</th>
  <th>IV%</th><th>Δ</th><th>Γ</th><th>Θ</th>
</tr></thead>`;

function renderTable(tableId, rows) {{
  if (!rows.length) {{
    document.getElementById(tableId).innerHTML = TABLE_HEAD + `<tbody><tr><td colspan="11" style="text-align:center;color:var(--muted);font-family:inherit;padding:18px">No contracts</td></tr></tbody>`;
    return;
  }}

  const tbody = rows.map(r => `<tr>
    <td class="${{r.unusual_vol ? 'unusual' : ''}}">${{shortCode(r.code)}}</td>
    <td>${{fmt(r.strike_price)}}</td>
    <td>${{fmtK(r.volume)}}</td>
    <td>${{fmtK(r.option_open_interest)}}</td>
    <td>${{fmt(r.last_price)}}</td>
    <td>${{fmt(r.bid_price)}}</td>
    <td>${{fmt(r.ask_price)}}</td>
    <td>${{r.option_implied_volatility != null ? fmt(r.option_implied_volatility, 1) : '—'}}</td>
    <td>${{fmt(r.option_delta, 3)}}</td>
    <td>${{fmt(r.option_gamma, 4)}}</td>
    <td>${{fmt(r.option_theta, 3)}}</td>
  </tr>`).join('');
  document.getElementById(tableId).innerHTML = TABLE_HEAD + `<tbody>${{tbody}}</tbody>`;
}}

renderTable('tbl-cur-calls', DATA.current.top5_calls);
renderTable('tbl-cur-puts',  DATA.current.top5_puts);
renderTable('tbl-nxt-calls', DATA.next.top5_calls);
renderTable('tbl-nxt-puts',  DATA.next.top5_puts);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='HK Options Dashboard Generator')
    parser.add_argument('--code',   default='HK.00700')
    parser.add_argument('--host',   default=DEFAULT_HOST)
    parser.add_argument('--port',   type=int, default=DEFAULT_PORT)
    parser.add_argument('--output', default=None)
    args = parser.parse_args()
    output_path = args.output or default_output_path(args.code)

    print(f"Connecting to FutuOpenD at {args.host}:{args.port} ...")
    ctx = OpenQuoteContext(host=args.host, port=args.port)

    try:
        print(f"Fetching data for {args.code}")

        current_exp, next_exp = get_expiries(ctx, args.code)
        current_exp_date = current_exp['date']
        next_exp_date = next_exp['date']
        print(f"  Current expiry: {current_exp_date}  |  Next expiry: {next_exp_date}")

        print("  Fetching current expiry chain ...")
        chain_cur = fetch_chain(ctx, args.code, current_exp_date)

        print("  Fetching next expiry chain ...")
        chain_nxt = fetch_chain(ctx, args.code, next_exp_date)

        snapshot_codes = [args.code] + chain_cur['code'].tolist() + chain_nxt['code'].tolist()
        print("  Fetching market snapshots ...")
        snapshot = fetch_snapshots(ctx, snapshot_codes, f"{args.code} spot and options")

        spot, change_pct = get_spot_from_snapshot(snapshot, args.code)
        print(f"  Spot: {spot:.2f}  ({change_pct:+.2f}%)")

        df_cur = merge_chain_snapshot(chain_cur, snapshot, args.code, current_exp_date)
        df_cur, suspended_cur = filter_suspended(df_cur, current_exp_date)
        if suspended_cur:
            print(f"  Filtered {suspended_cur} suspended contracts for {current_exp_date}")
        df_cur = flag_unusual(df_cur)

        df_nxt = merge_chain_snapshot(chain_nxt, snapshot, args.code, next_exp_date)
        df_nxt, suspended_nxt = filter_suspended(df_nxt, next_exp_date)
        if suspended_nxt:
            print(f"  Filtered {suspended_nxt} suspended contracts for {next_exp_date}")
        df_nxt = flag_unusual(df_nxt)

        print("  Calculating key levels ...")
        lvl_cur = key_levels(df_cur, spot)
        lvl_nxt = key_levels(df_nxt, spot)

        payload = {
            'ticker':      args.code,
            'spot':        round(spot, 4),
            'change_pct':  round(change_pct, 2),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'current': {
                'expiry':     current_exp_date,
                'expiry_days': current_exp['days'],
                'is_expiry_today': current_exp['is_expiry_today'],
                'levels':     lvl_cur,
                'top5_calls': to_records(top5_by_vol(df_cur, 'CALL')),
                'top5_puts':  to_records(top5_by_vol(df_cur, 'PUT')),
                'oi_chart':   oi_chart_data(df_cur),
            },
            'next': {
                'expiry':     next_exp_date,
                'expiry_days': next_exp['days'],
                'is_expiry_today': next_exp['is_expiry_today'],
                'levels':     lvl_nxt,
                'top5_calls': to_records(top5_by_vol(df_nxt, 'CALL')),
                'top5_puts':  to_records(top5_by_vol(df_nxt, 'PUT')),
                'oi_chart':   oi_chart_data(df_nxt),
            },
        }

        print("  Generating HTML ...")
        html = generate_html(payload)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)

        print(f"\nDone — dashboard written to: {output_path}")
        print(f"  Open in browser: {Path(output_path).resolve().as_uri()}")

    except RuntimeError as exc:
        print(f"\nError: {exc}")
        raise SystemExit(1) from None

    finally:
        ctx.close()


if __name__ == '__main__':
    main()
