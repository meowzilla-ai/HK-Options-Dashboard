#!/usr/bin/env python3
"""
HK Stock Options Dashboard Generator
Usage: python3 options-dashboard.py [--code HK.00700] [--host 127.0.0.1] [--port 11111] [--output dashboard.html]
Requires FutuOpenD running locally.
"""

import json
import argparse
import re
from datetime import datetime, timedelta
from html import escape, unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import numpy as np
import pandas as pd
from futu import OpenQuoteContext, RET_OK

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 11111
HKEX_REPORT_URL = 'https://www.hkex.com.hk/eng/stat/dmstat/dayrpt/dqe{date}.htm'
HKEX_CACHE_DIR = Path('.cache') / 'hkex'


def default_output_path(code):
    ticker = ''.join(ch for ch in code if ch.isalnum())
    date = datetime.now().strftime('%Y-%m-%d')
    return f"{ticker}-options-{date}.html"


def require_columns(df, columns, label):
    """Raise a clear error when a Futu response is missing required columns."""
    missing = [col for col in columns if col not in df.columns]
    if missing:
        raise RuntimeError(f"{label} missing required columns: {', '.join(missing)}")


def parse_option_code(code):
    """Parse observed Futu option codes into HKEX class, expiry, type, and strike."""
    text = str(code)
    match = re.match(r'^[A-Z]+\.(?P<class>[A-Z]+)(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d+)$', text)
    if match:
        expiry = datetime.strptime(match.group('date'), '%y%m%d').strftime('%Y-%m-%d')
        strike_raw = int(match.group('strike'))
        strike = strike_raw / 1000 if strike_raw >= 1000 else float(strike_raw)
        return {
            'option_class': match.group('class'),
            'expiry_date': expiry,
            'option_side': 'CALL' if match.group('type') == 'C' else 'PUT',
            'strike_price': float(strike),
        }

    match = re.search(r'\.?(?P<class>\d+)(?P<type>[CP])(?P<date>\d{8})(?P<strike>\d+)$', text)
    if match:
        expiry = datetime.strptime(match.group('date'), '%Y%m%d').strftime('%Y-%m-%d')
        strike_raw = int(match.group('strike'))
        strike = strike_raw / 1000 if strike_raw >= 1000 else float(strike_raw)
        return {
            'option_class': match.group('class'),
            'expiry_date': expiry,
            'option_side': 'CALL' if match.group('type') == 'C' else 'PUT',
            'strike_price': float(strike),
        }

    return {
        'option_class': None,
        'expiry_date': None,
        'option_side': None,
        'strike_price': None,
    }


def add_option_code_fields(df):
    parsed = df['code'].apply(parse_option_code).apply(pd.Series)
    out = df.copy()
    out['option_class'] = parsed['option_class']
    out['parsed_expiry_date'] = parsed['expiry_date']
    out['parsed_option_side'] = parsed['option_side']
    return out


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


def fetch_derivative_commentary(ctx, code, time_range=7, language_id=1):
    if not hasattr(ctx, 'get_derivative_unusual'):
        return {
            'enabled': True,
            'loaded': False,
            'time_range': None,
            'range_label': f"Last {time_range} days",
            'content': None,
            'message': 'get_derivative_unusual is unavailable in this futu-api version',
        }

    try:
        ret, data = ctx.get_derivative_unusual(code, time_range=time_range, language_id=language_id)
    except Exception as exc:
        return {
            'enabled': True,
            'loaded': False,
            'time_range': None,
            'range_label': f"Last {time_range} days",
            'content': None,
            'message': f"Futu derivative commentary unavailable: {exc}",
        }

    if ret != RET_OK or not isinstance(data, dict) or data.get('err_code') != 0:
        return {
            'enabled': True,
            'loaded': False,
            'time_range': None,
            'range_label': f"Last {time_range} days",
            'content': None,
            'message': str(data),
        }

    content = data.get('content')
    return {
        'enabled': True,
        'loaded': bool(content),
        'time_range': data.get('time_range'),
        'range_label': f"Last {time_range} days",
        'content': content,
        'message': data.get('retMsg') or 'success',
    }


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
        print(f"  Warning: snapshot missing {len(missing_codes)} contract(s) "
              f"for {code} expiry {expiry_date}: {sample}{suffix} — filling with NaN")

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
# HKEX previous-day report enrichment
# ---------------------------------------------------------------------------

def normalise_hkex_date(value):
    """Return YYYYMMDD for CLI input accepted as YYYYMMDD or YYMMDD."""
    text = str(value).strip()
    if re.fullmatch(r'\d{8}', text):
        return text
    if re.fullmatch(r'\d{6}', text):
        return '20' + text
    raise ValueError("HKEX date must be YYYYMMDD or YYMMDD")


def hkex_url_date(report_date):
    return datetime.strptime(report_date, '%Y%m%d').strftime('%y%m%d')


def hkex_cache_path(report_date):
    return HKEX_CACHE_DIR / f"dqe{hkex_url_date(report_date)}.htm"


def validate_hkex_report_html(html, report_date):
    if not str(html).strip():
        raise ValueError(f"HKEX report {report_date} is empty")


def fetch_hkex_daily_report(report_date):
    cache_path = hkex_cache_path(report_date)
    url = HKEX_REPORT_URL.format(date=hkex_url_date(report_date))
    if cache_path.exists():
        html = cache_path.read_text(encoding='iso-8859-1', errors='replace')
        validate_hkex_report_html(html, report_date)
        return html, url

    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(req, timeout=10) as resp:
        html = resp.read().decode('iso-8859-1', errors='replace')
    validate_hkex_report_html(html, report_date)

    HKEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + '.tmp')
    tmp_path.write_text(html, encoding='iso-8859-1', errors='replace')
    tmp_path.replace(cache_path)
    return html, url


def find_latest_hkex_report(start_date=None, lookback_days=10, option_class=None):
    start = start_date or (datetime.now().date() - timedelta(days=1))
    failures = []
    for offset in range(lookback_days + 1):
        report_date = (start - timedelta(days=offset)).strftime('%Y%m%d')
        try:
            html, url = fetch_hkex_daily_report(report_date)
            if option_class:
                rows = parse_hkex_daily_report(html, option_class)
                if rows.empty:
                    failures.append(f"{report_date}: no {option_class} rows in HKEX report")
                    continue
            return report_date, html, url
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            failures.append(f"{report_date}: {exc}")
    raise RuntimeError(
        f"No HKEX daily report found in last {lookback_days + 1} calendar days\n"
        + "\n".join(f"  {f}" for f in failures)
    )


def parse_hkex_number(value):
    text = str(value).replace(',', '').strip()
    if text in {'', '-', 'NIL'}:
        return None
    return float(text)


def parse_hkex_int(value):
    number = parse_hkex_number(value)
    return None if number is None else int(round(number))


_HKEX_MONTH = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5,  'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}

def hkex_expiry_to_iso(value):
    """Convert HKEX expiry string (e.g. '08MAY26') to ISO date. Locale-independent."""
    text = str(value).strip().upper()
    if len(text) != 7:
        raise ValueError(f"Unexpected HKEX expiry format: {value!r}")
    day   = int(text[:2])
    month = _HKEX_MONTH.get(text[2:5])
    year  = 2000 + int(text[5:7])
    if month is None:
        raise ValueError(f"Unknown month abbreviation {text[2:5]!r} in HKEX date {value!r}")
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_hkex_daily_report(html, option_class):
    """Parse one HKEX option class section from a daily market report."""
    option_class = option_class.upper()
    section_match = re.search(
        rf'<A NAME="{re.escape(option_class)}">\s*(?P<section>.*?)(?=<A NAME="[^"]+">|\Z)',
        html,
        flags=re.S | re.I,
    )
    if not section_match:
        return pd.DataFrame()

    section = unescape(re.sub(r'<[^>]+>', '', section_match.group('section')))
    records = []
    for line in section.splitlines():
        parts = line.split()
        if len(parts) < 12:
            continue
        if not re.fullmatch(r'\d{2}[A-Z]{3}\d{2}', parts[0].upper()):
            continue
        if parts[2] not in {'C', 'P'}:
            continue

        try:
            records.append({
                'option_class': option_class,
                'expiry_date': hkex_expiry_to_iso(parts[0].upper()),
                'strike_price': float(parts[1].replace(',', '')),
                'option_side': 'CALL' if parts[2] == 'C' else 'PUT',
                'prev_volume': parse_hkex_int(parts[9]),
                'prev_oi': parse_hkex_int(parts[10]),
                'prev_oi_change': parse_hkex_int(parts[11]),
            })
        except ValueError:
            continue

    return pd.DataFrame.from_records(records)


def infer_option_class(*frames):
    classes = []
    for frame in frames:
        if 'option_class' not in frame.columns:
            continue
        classes.extend(frame['option_class'].dropna().astype(str).unique().tolist())
    if not classes:
        return None
    counts = pd.Series(classes).value_counts()
    return str(counts.index[0])


def merge_hkex_activity(df, hkex_rows):
    out = df.copy()
    defaults = {
        'prev_volume': None,
        'prev_oi': None,
        'prev_oi_change': None,
    }
    if hkex_rows is None or hkex_rows.empty:
        for col, value in defaults.items():
            out[col] = value
        return out

    key_cols = ['option_class', 'parsed_expiry_date', 'strike_price', 'parsed_option_side']
    hkex = hkex_rows.rename(columns={
        'expiry_date': 'parsed_expiry_date',
        'option_side': 'parsed_option_side',
    })
    out = out.merge(
        hkex[key_cols + ['prev_volume', 'prev_oi', 'prev_oi_change']],
        on=key_cols,
        how='left',
    )
    return out


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


def apply_activity_signals(
    df,
    min_live_volume_floor=100,
    p75_volume_floor=100,
    p90_volume_floor=200,
    min_traded_contracts_for_percentiles=5,
    volume_oi_ratio_threshold=0.50,
):
    df = df.copy()
    df['volume_oi_ratio'] = None
    df['activity_score'] = 0
    df['activity_label'] = 'Normal'
    df['activity_hot'] = False
    df['activity_reasons'] = [[] for _ in range(len(df))]

    oi = pd.to_numeric(df['option_open_interest'], errors='coerce')
    volume = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
    ratio_mask = oi > 0
    df.loc[ratio_mask, 'volume_oi_ratio'] = (volume[ratio_mask] / oi[ratio_mask]).round(4)

    for t in ['CALL', 'PUT']:
        mask = df['option_type'] == t
        if not mask.any():
            continue

        side = df.loc[mask].copy()
        score = pd.Series(0, index=side.index, dtype=int)
        reasons = {idx: [] for idx in side.index}

        live_volume = pd.to_numeric(side['volume'], errors='coerce').fillna(0)
        live_volume_signal = live_volume >= min_live_volume_floor
        score += live_volume_signal.astype(int)
        for idx, value in live_volume[live_volume_signal].items():
            reasons[idx].append(f"Live volume {int(value):,} >= {min_live_volume_floor:,}")

        traded_volume = live_volume[live_volume > 0]
        if len(traded_volume) >= min_traded_contracts_for_percentiles:
            p75_threshold = max(p75_volume_floor, float(traded_volume.quantile(0.75)))
            p90_threshold = max(p90_volume_floor, float(traded_volume.quantile(0.90)))

            p75_signal = live_volume >= p75_threshold
            score += p75_signal.astype(int)
            for idx, value in live_volume[p75_signal].items():
                reasons[idx].append(f"Live volume {int(value):,} >= side p75 threshold {p75_threshold:,.0f}")

            p90_signal = live_volume >= p90_threshold
            score += p90_signal.astype(int)
            for idx, value in live_volume[p90_signal].items():
                reasons[idx].append(f"Live volume {int(value):,} >= side p90 threshold {p90_threshold:,.0f}")

        ratio = pd.to_numeric(side['volume_oi_ratio'], errors='coerce')
        ratio_signal = (ratio >= volume_oi_ratio_threshold).fillna(False)
        score += ratio_signal.astype(int)
        for idx, value in ratio[ratio_signal].items():
            reasons[idx].append(f"Volume/OI {value:.0%} >= {volume_oi_ratio_threshold:.0%}")

        if 'prev_oi_change' in side.columns:
            prev_oi_change = pd.to_numeric(side['prev_oi_change'], errors='coerce')
            oi_change_signal = (prev_oi_change > 0).fillna(False)
            score += oi_change_signal.astype(int)
            for idx, value in prev_oi_change[oi_change_signal].items():
                reasons[idx].append(f"Previous-day OI change +{int(value):,}")

        df.loc[side.index, 'activity_score'] = score
        for idx, items in reasons.items():
            df.at[idx, 'activity_reasons'] = items

    df.loc[df['activity_score'] >= 4, 'activity_label'] = 'Strong Positioning'
    df.loc[df['activity_score'] == 3, 'activity_label'] = 'High Activity'
    df.loc[df['activity_score'] == 2, 'activity_label'] = 'Active'
    df['activity_hot'] = df['activity_score'] >= 4
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


def contract_ref(row):
    if row is None or row.empty:
        return None
    return {
        'option_class': row.get('option_class'),
        'option_type': row.get('option_type'),
        'strike_price': float(row['strike_price']) if pd.notna(row.get('strike_price')) else None,
        'prev_oi_change': int(row['prev_oi_change']) if pd.notna(row.get('prev_oi_change')) else None,
        'volume': int(row['volume']) if pd.notna(row.get('volume')) else None,
        'option_open_interest': int(row['option_open_interest']) if pd.notna(row.get('option_open_interest')) else None,
    }


def oi_change_bias(call_change, put_change):
    threshold = max(50, 0.1 * max(abs(call_change), abs(put_change), 1))
    if abs(call_change - put_change) < threshold:
        return 'Balanced'
    if call_change > 0 and put_change > 0:
        return 'Calls building faster' if call_change > put_change else 'Puts building faster'
    if call_change < 0 and put_change < 0:
        return 'Calls unwinding faster' if abs(call_change) > abs(put_change) else 'Puts unwinding faster'
    if call_change > 0 and put_change < 0:
        return 'Calls building, puts unwinding'
    if call_change < 0 and put_change > 0:
        return 'Puts building, calls unwinding'
    if call_change == 0 and put_change > 0:
        return 'Puts building'
    if call_change == 0 and put_change < 0:
        return 'Puts unwinding'
    if put_change == 0 and call_change > 0:
        return 'Calls building'
    if put_change == 0 and call_change < 0:
        return 'Calls unwinding'
    return 'Mixed'


def oi_change_summary(df):
    out = {
        'call_oi_change': None,
        'put_oi_change': None,
        'bias': 'Unavailable',
        'largest_call_increase': None,
        'largest_put_increase': None,
    }
    if 'prev_oi_change' not in df.columns:
        return out

    work = df.copy()
    work['prev_oi_change_num'] = pd.to_numeric(work['prev_oi_change'], errors='coerce')
    known = work[work['prev_oi_change_num'].notna()].copy()
    if known.empty:
        return out

    calls = known[known['option_type'] == 'CALL']
    puts = known[known['option_type'] == 'PUT']
    call_change = int(calls['prev_oi_change_num'].sum()) if not calls.empty else 0
    put_change = int(puts['prev_oi_change_num'].sum()) if not puts.empty else 0

    def largest_increase(frame):
        positive = frame[frame['prev_oi_change_num'] > 0]
        if positive.empty:
            return None
        return contract_ref(positive.sort_values('prev_oi_change_num', ascending=False).iloc[0])

    out.update({
        'call_oi_change': call_change,
        'put_oi_change': put_change,
        'bias': oi_change_bias(call_change, put_change),
        'largest_call_increase': largest_increase(calls),
        'largest_put_increase': largest_increase(puts),
    })
    return out


def oi_chart_data(df):
    calls = df[df['option_type'] == 'CALL'].groupby('strike_price')['option_open_interest'].sum()
    puts  = df[df['option_type'] == 'PUT'].groupby('strike_price')['option_open_interest'].sum()
    all_strikes = sorted(set(calls.index) | set(puts.index))

    # Trim far-edge zeros: find first and last strike with any OI,
    # keep everything in between (interior zeros preserved as meaningful gaps).
    active = [s for s in all_strikes if calls.get(s, 0) > 0 or puts.get(s, 0) > 0]
    if not active:
        return {'strikes': [], 'call_oi': [], 'put_oi': []}
    trimmed = [s for s in all_strikes if active[0] <= s <= active[-1]]

    return {
        'strikes':  [float(s) for s in trimmed],
        'call_oi':  [float(calls.get(s, 0)) for s in trimmed],
        'put_oi':   [float(puts.get(s, 0))  for s in trimmed],
    }


def vol_chart_data(df, top5_calls_records, top5_puts_records):
    """Volume chart data: union of top-5 strikes, both sides looked up from full chain."""
    strikes = sorted(set(
        [r['strike_price'] for r in top5_calls_records if r.get('strike_price') is not None] +
        [r['strike_price'] for r in top5_puts_records  if r.get('strike_price') is not None]
    ))
    call_vol = df[df['option_type'] == 'CALL'].groupby('strike_price')['volume'].sum()
    put_vol  = df[df['option_type'] == 'PUT'].groupby('strike_price')['volume'].sum()
    return {
        'strikes':  [float(s) for s in strikes],
        'call_vol': [float(call_vol.get(s, 0)) for s in strikes],
        'put_vol':  [float(put_vol.get(s, 0))  for s in strikes],
    }


def to_records(df):
    cols = ['code', 'option_class', 'option_type', 'strike_price', 'volume', 'option_open_interest',
            'prev_volume', 'prev_oi', 'prev_oi_change',
            'volume_oi_ratio', 'activity_score', 'activity_label', 'activity_hot', 'activity_reasons',
            'last_price', 'bid_price', 'ask_price',
            'option_implied_volatility', 'option_delta',
            'option_gamma', 'option_vega', 'option_theta']
    cols = [c for c in cols if c in df.columns]
    records = []
    for _, row in df[cols].iterrows():
        rec = {}
        for c in cols:
            v = row[c]
            if isinstance(v, list):
                rec[c] = v
            elif pd.isna(v) if not isinstance(v, bool) else False:
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
  .activity-source {{ color: var(--muted); background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin: -10px 0 18px; font-size: 12px; }}
  .commentary-box {{ display: none; background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 24px; }}
  .commentary-box.show {{ display: block; }}
  .commentary-box h3 {{ font-size: 12px; color: var(--purple); margin-bottom: 6px; text-transform: uppercase; letter-spacing: .4px; }}
  .commentary-box .meta {{ color: var(--muted); font-size: 11px; margin-bottom: 8px; }}
  .commentary-box ul {{ list-style: disc; padding-left: 18px; }}
  .commentary-box li {{ color: var(--text); font-size: 12px; line-height: 1.55; margin: 5px 0; }}

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
  .summary-row {{ display: grid; grid-template-columns: 1fr; gap: 16px; margin-bottom: 24px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }}
  .summary-item .lbl {{ color: var(--muted); font-size: 10px; text-transform: uppercase; margin-bottom: 2px; }}
  .summary-item .val {{ font-size: 13px; font-weight: 600; }}
  .summary-item .sub {{ color: var(--muted); font-size: 10px; margin-top: 2px; }}

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
  .activity-hot {{ color: var(--yellow); }}

  /* P/C ratio bar */
  .pc-bar-wrap {{ margin-top: 10px; }}
  .pc-bar-label {{ display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 4px; }}
  .pc-bar {{ height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; }}
  .pc-bar-fill {{ height: 100%; transition: width .3s; }}

  @media (max-width: 900px) {{
    .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .charts-row, .levels-row, .tables-row {{ grid-template-columns: 1fr; }}
  }}
  @media (max-width: 520px) {{
    .cards {{ grid-template-columns: 1fr; }}
    .summary-grid {{ grid-template-columns: 1fr; }}
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
  <div class="activity-source" id="activity-source"></div>
  <div class="commentary-box" id="futu-commentary-box">
    <h3>Futu Derivative Abnormal Activity</h3>
    <div class="meta" id="futu-commentary-meta"></div>
    <ul id="futu-commentary-content"></ul>
  </div>

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
  <div class="summary-row">
    <div class="levels-box">
      <h3>Previous-Day OI Change</h3>
      <div class="summary-grid" id="current-oi-change-summary"></div>
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
  <div class="summary-row">
    <div class="levels-box">
      <h3>Previous-Day OI Change</h3>
      <div class="summary-grid" id="next-oi-change-summary"></div>
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
const fmtSignedK = (v) => v == null ? '—' : `${{Number(v) > 0 ? '+' : Number(v) < 0 ? '-' : ''}}${{fmtK(Math.abs(Number(v)))}}`;
const ratioClass = (v) => v == null ? 'neutral' : v > 1 ? 'down' : 'up';
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));

function shortCode(row) {{
  if (row.option_class) return row.option_class;

  let m = String(row.code || '').match(/^[A-Z]+\\.([A-Z]+)\\d{{6}}[CP]\\d+$/);
  if (m) return m[1];

  return row.code;
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

function renderActivitySource() {{
  const hkex = DATA.hkex || {{}};
  const el = document.getElementById('activity-source');
  if (hkex.loaded && hkex.matched_contracts > 0) {{
    const coverage = hkex.match_coverage_pct != null ? `, ${{hkex.match_coverage_pct}}% coverage` : '';
    el.textContent = `Activity signal uses live Futu volume/OI plus HKEX previous trading-day OI change from ${{hkex.date}} (${{hkex.option_class}}), matched ${{hkex.matched_contracts}} of ${{hkex.fetched_contracts}} fetched contracts${{coverage}}. Rows without HKEX matches use live Futu fields only.`;
  }} else if (hkex.enabled) {{
    el.textContent = hkex.message || 'HKEX activity data is unavailable; activity markers use live Futu data only.';
  }} else {{
    el.textContent = 'HKEX activity enrichment disabled; activity markers use live Futu data only.';
  }}
}}

renderActivitySource();

function renderFutuCommentary() {{
  const commentary = DATA.futu_commentary || {{}};
  if (!commentary.loaded || !commentary.content) return;

  document.getElementById('futu-commentary-meta').textContent = commentary.range_label || '';
  const lines = String(commentary.content)
    .split(/\\n+/)
    .map(line => line.trim())
    .filter(Boolean);
  document.getElementById('futu-commentary-content').innerHTML = lines.map(line => `<li>${{esc(line)}}</li>`).join('');
  document.getElementById('futu-commentary-box').classList.add('show');
}}

renderFutuCommentary();

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

function contractLabel(c) {{
  if (!c) return '—';
  const type = c.option_type === 'CALL' ? 'C' : c.option_type === 'PUT' ? 'P' : '';
  return `${{type}}${{fmt(c.strike_price)}}`;
}}

function contractSub(c) {{
  if (!c) return '';
  return `Vol ${{fmtK(c.volume)}} | OI ${{fmtK(c.option_open_interest)}}`;
}}

function renderOIChangeSummary(containerId, summary) {{
  summary = summary || {{}};
  const items = [
    {{ lbl:'Call OI Chg', val: fmtSignedK(summary.call_oi_change), cls: summary.call_oi_change > 0 ? 'up' : summary.call_oi_change < 0 ? 'down' : '', sub:'previous day' }},
    {{ lbl:'Put OI Chg', val: fmtSignedK(summary.put_oi_change), cls: summary.put_oi_change > 0 ? 'up' : summary.put_oi_change < 0 ? 'down' : '', sub:'previous day' }},
    {{ lbl:'Bias', val: summary.bias || 'Unavailable', cls:'neutral', sub:'by OI change' }},
    {{ lbl:'Largest Call Add', val: contractLabel(summary.largest_call_increase), cls:'up', sub: summary.largest_call_increase ? `${{fmtSignedK(summary.largest_call_increase.prev_oi_change)}} | ${{contractSub(summary.largest_call_increase)}}` : '' }},
    {{ lbl:'Largest Put Add', val: contractLabel(summary.largest_put_increase), cls:'down', sub: summary.largest_put_increase ? `${{fmtSignedK(summary.largest_put_increase.prev_oi_change)}} | ${{contractSub(summary.largest_put_increase)}}` : '' }},
  ];
  document.getElementById(containerId).innerHTML = items.map(i => `
    <div class="summary-item">
      <div class="lbl">${{esc(i.lbl)}}</div>
      <div class="val ${{i.cls}}">${{esc(i.val)}}</div>
      <div class="sub">${{esc(i.sub)}}</div>
    </div>`).join('');
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
renderOIChangeSummary('current-oi-change-summary', DATA.current.oi_change_summary);
renderLevels('next-levels', DATA.next.levels);
renderPC('next-pc', DATA.next.levels);
renderOIChangeSummary('next-oi-change-summary', DATA.next.oi_change_summary);

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

function makeVolChart(canvasId, volChart) {{
  const labels   = volChart.strikes.map(s => fmt(s));
  const callVols = volChart.call_vol;
  const putVols  = volChart.put_vol;
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
makeVolChart('chart-current-vol', DATA.current.vol_chart);
makeOIChart('chart-next', DATA.next.oi_chart, DATA.next.levels);
makeVolChart('chart-next-vol', DATA.next.vol_chart);

// ── option tables ─────────────────────────────────────────────────────────────
const TABLE_HEAD = `<thead><tr>
  <th>Code</th><th>Strike</th><th>Vol</th><th>OI</th><th>OI Chg</th>
  <th>Last</th><th>Bid</th><th>Ask</th>
  <th>IV%</th><th>Δ</th><th>Γ</th><th>Θ</th>
</tr></thead>`;

function renderTable(tableId, rows) {{
  if (!rows.length) {{
    document.getElementById(tableId).innerHTML = TABLE_HEAD + `<tbody><tr><td colspan="12" style="text-align:center;color:var(--muted);font-family:inherit;padding:18px">No contracts</td></tr></tbody>`;
    return;
  }}

  const activityTitle = (r) => [
    r.activity_label || 'Normal',
    ...((r.activity_reasons || []).map(reason => `- ${{reason}}`)),
  ].join('\\n');

  const tbody = rows.map(r => `<tr>
    <td class="${{r.activity_hot ? 'activity-hot' : ''}}" title="${{esc(activityTitle(r))}}">${{esc(shortCode(r))}}${{r.activity_hot ? ' &#128293;' : ''}}</td>
    <td>${{fmt(r.strike_price)}}</td>
    <td>${{fmtK(r.volume)}}</td>
    <td>${{fmtK(r.option_open_interest)}}</td>
    <td class="${{r.prev_oi_change > 0 ? 'up' : r.prev_oi_change < 0 ? 'down' : ''}}">${{fmtSignedK(r.prev_oi_change)}}</td>
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
    parser.add_argument('--hkex-date', default=None, help='HKEX daily report date, YYYYMMDD or YYMMDD; defaults to latest available')
    parser.add_argument('--no-hkex', action='store_true', help='Disable HKEX previous-day activity enrichment')
    parser.add_argument('--no-futu-commentary', action='store_true', help='Disable Futu derivative abnormal activity commentary')
    parser.add_argument('--commentary-language', type=int, default=2, help='Futu commentary language: 0 simplified Chinese, 1 traditional Chinese, 2 English')
    parser.add_argument('--commentary-time-range', type=int, default=7, help='Futu commentary time range in natural days')
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

        if args.no_futu_commentary:
            futu_commentary = {
                'enabled': False,
                'loaded': False,
                'time_range': None,
                'range_label': f"Last {args.commentary_time_range} days",
                'content': None,
                'message': 'Futu derivative commentary disabled',
            }
        else:
            print("  Fetching Futu derivative abnormal activity ...")
            futu_commentary = fetch_derivative_commentary(
                ctx,
                args.code,
                time_range=args.commentary_time_range,
                language_id=args.commentary_language,
            )
            if not futu_commentary['loaded']:
                print(f"  Futu commentary unavailable: {futu_commentary['message']}")

        df_cur = merge_chain_snapshot(chain_cur, snapshot, args.code, current_exp_date)
        df_cur, suspended_cur = filter_suspended(df_cur, current_exp_date)
        if suspended_cur:
            print(f"  Filtered {suspended_cur} suspended contracts for {current_exp_date}")
        df_cur = add_option_code_fields(df_cur)

        df_nxt = merge_chain_snapshot(chain_nxt, snapshot, args.code, next_exp_date)
        df_nxt, suspended_nxt = filter_suspended(df_nxt, next_exp_date)
        if suspended_nxt:
            print(f"  Filtered {suspended_nxt} suspended contracts for {next_exp_date}")
        df_nxt = add_option_code_fields(df_nxt)

        option_class = infer_option_class(df_cur, df_nxt)
        hkex_meta = {
            'enabled': not args.no_hkex,
            'loaded': False,
            'date': None,
            'url': None,
            'option_class': option_class,
            'matched_contracts': 0,
            'fetched_contracts': int(len(df_cur) + len(df_nxt)),
            'match_coverage_pct': None,
            'message': 'HKEX enrichment disabled' if args.no_hkex else '',
        }

        if args.no_hkex:
            df_cur = merge_hkex_activity(df_cur, pd.DataFrame())
            df_nxt = merge_hkex_activity(df_nxt, pd.DataFrame())
        elif not option_class:
            hkex_meta['message'] = 'Could not infer HKEX option class from Futu option codes'
            df_cur = merge_hkex_activity(df_cur, pd.DataFrame())
            df_nxt = merge_hkex_activity(df_nxt, pd.DataFrame())
        else:
            try:
                if args.hkex_date:
                    report_date = normalise_hkex_date(args.hkex_date)
                    hkex_html, hkex_url = fetch_hkex_daily_report(report_date)
                else:
                    report_date, hkex_html, hkex_url = find_latest_hkex_report(option_class=option_class)

                hkex_rows = parse_hkex_daily_report(hkex_html, option_class)
                df_cur = merge_hkex_activity(df_cur, hkex_rows)
                df_nxt = merge_hkex_activity(df_nxt, hkex_rows)
                matched = int(df_cur['prev_oi_change'].notna().sum() + df_nxt['prev_oi_change'].notna().sum())
                fetched = hkex_meta['fetched_contracts']
                coverage = round(matched / fetched * 100, 1) if fetched else None
                hkex_meta.update({
                    'loaded': not hkex_rows.empty,
                    'date': report_date,
                    'url': hkex_url,
                    'matched_contracts': matched,
                    'match_coverage_pct': coverage,
                    'message': (
                        f"HKEX report loaded; matched {matched} of {fetched} fetched contracts"
                        if matched else f"HKEX report loaded but no fetched contracts matched out of {fetched}"
                    ),
                })
                print(f"  HKEX activity: {hkex_meta['message']}")
            except (RuntimeError, HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                hkex_meta['message'] = f"HKEX activity unavailable: {exc}"
                print(f"  {hkex_meta['message']}")
                df_cur = merge_hkex_activity(df_cur, pd.DataFrame())
                df_nxt = merge_hkex_activity(df_nxt, pd.DataFrame())

        df_cur = apply_activity_signals(df_cur)
        df_nxt = apply_activity_signals(df_nxt)

        print("  Calculating key levels ...")
        lvl_cur = key_levels(df_cur, spot)
        lvl_nxt = key_levels(df_nxt, spot)

        payload = {
            'ticker':      args.code,
            'spot':        round(spot, 4),
            'change_pct':  round(change_pct, 2),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'hkex':        hkex_meta,
            'futu_commentary': futu_commentary,
            'current': {
                'expiry':     current_exp_date,
                'expiry_days': current_exp['days'],
                'is_expiry_today': current_exp['is_expiry_today'],
                'levels':     lvl_cur,
                'top5_calls': (cur_calls := to_records(top5_by_vol(df_cur, 'CALL'))),
                'top5_puts':  (cur_puts  := to_records(top5_by_vol(df_cur, 'PUT'))),
                'oi_change_summary': oi_change_summary(df_cur),
                'oi_chart':   oi_chart_data(df_cur),
                'vol_chart':  vol_chart_data(df_cur, cur_calls, cur_puts),
            },
            'next': {
                'expiry':     next_exp_date,
                'expiry_days': next_exp['days'],
                'is_expiry_today': next_exp['is_expiry_today'],
                'levels':     lvl_nxt,
                'top5_calls': (nxt_calls := to_records(top5_by_vol(df_nxt, 'CALL'))),
                'top5_puts':  (nxt_puts  := to_records(top5_by_vol(df_nxt, 'PUT'))),
                'oi_change_summary': oi_change_summary(df_nxt),
                'oi_chart':   oi_chart_data(df_nxt),
                'vol_chart':  vol_chart_data(df_nxt, nxt_calls, nxt_puts),
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
