#!/usr/bin/env python3
"""Rank Binance USDT-margined perpetual futures with alpha A101_055.

    A101_055 = -1 * correlation(rank((close - ts_min(low, 12)) /
                                     (ts_max(high, 12) - ts_min(low, 12))),
                                rank(volume), 6)

Lists the top 16 coins (BUY) and the bottom 16 coins (SHORT) for the latest
completed daily candle, among that day's top-80-by-quote-volume universe.

Research and paper trading only. The script calls public market-data
endpoints of https://fapi.binance.com, never places orders and never uses
API keys.
"""

import argparse
import json
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE_URL = "https://fapi.binance.com"
USER_AGENT = "a101-055-ranker/1.0 (research, public market data only)"

EXCLUDED_BASES = {
    "USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "USDE", "USD1", "PYUSD",
    "EUR", "AEUR", "PAXG", "XAUT", "BTCDOM", "DEFI", "BLUEBIRD", "FOOTBALL",
}

# Data
KLINE_INTERVAL = "1d"
KLINE_LIMIT = 100

# Rate limiting
PAUSE_SECONDS = 0.1          # pause between calls
HTTP_TIMEOUT = 30
MAX_RETRIES = 6
WEIGHT_SOFT_LIMIT = 2000     # Binance REQUEST_WEIGHT limit is 2400 per minute
MAX_BACKOFF_SECONDS = 900    # longer 418/429 bans are reported, not waited out

# Daily universe
UNIVERSE_DAYS = 10
MIN_HISTORY_CANDLES = 60
ADV_WINDOW = 30
ADV_MIN_DAYS = 15
UNIVERSE_SIZE = 80

# A101_055
POS_WINDOW = 12
POS_MIN_DAYS = 8
CORR_WINDOW = 6
CORR_MIN_DAYS = 4
FLAT_STD = 1e-12
TIE_DECIMALS = 12            # scores equal to 12 decimals count as a tie

# Output
LIST_SIZE = 16
DEFAULT_FUNDING_HOURS = 8
SHORT_FUNDING_FLAG = -0.0010  # -0.10% per day


class ApiError(RuntimeError):
    """A request failed; other requests may still work."""


class FatalApiError(ApiError):
    """The API cannot be used right now (IP ban, blocked location)."""


class ComputeError(RuntimeError):
    """A required value cannot be computed from the data."""


def log(message):
    print(message, file=sys.stderr, flush=True)


class BinancePublic:
    """Minimal GET client for public endpoints, with pacing and back-off."""

    def __init__(self, pause=PAUSE_SECONDS, max_retries=MAX_RETRIES):
        self.pause = pause
        self.max_retries = max_retries
        self._last_call = 0.0

    def get(self, path, params=None):
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        delay = 1.0
        for _ in range(self.max_retries):
            self._pace()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                    payload = json.loads(resp.read())
                    self._respect_weight(resp.headers.get("X-MBX-USED-WEIGHT-1M"))
                    return payload
            except urllib.error.HTTPError as err:
                if err.code in (418, 429):
                    wait = _retry_after(err, default=max(delay, 5.0))
                    if wait > MAX_BACKOFF_SECONDS:
                        raise FatalApiError(
                            f"HTTP {err.code}: Binance asks to wait {wait:.0f}s "
                            "(rate-limit ban). Stopping; try again later."
                        ) from err
                    log(f"HTTP {err.code} on {path}: backing off {wait:.0f}s")
                    time.sleep(wait)
                    delay = min(delay * 2, 60.0)
                    continue
                if err.code == 451:
                    raise FatalApiError(
                        "HTTP 451: the Binance futures API is not available "
                        "from this location."
                    ) from err
                if err.code >= 500:
                    log(f"HTTP {err.code} on {path}: retrying in {delay:.0f}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                body = err.read()[:200].decode("utf-8", "replace")
                raise ApiError(f"HTTP {err.code} on {path}: {body}") from err
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
                    raise FatalApiError(_cert_help(err.reason)) from err
                log(f"Network error on {path} ({err}): retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
        raise ApiError(f"Giving up on {path} after {self.max_retries} attempts")

    def _pace(self):
        wait = self.pause - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _respect_weight(used_weight):
        if used_weight and int(used_weight) >= WEIGHT_SOFT_LIMIT:
            wait = 61 - (time.time() % 60)
            log(f"Used weight {used_weight}/2400 this minute: pausing {wait:.0f}s")
            time.sleep(wait)


def _cert_help(reason):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        f"HTTPS certificate check failed ({reason.verify_message}).\n"
        f"This Python ({sys.executable}) cannot verify Binance's certificate,\n"
        "usually because it has no CA certificates installed. Fixes:\n"
        f'  - Python from python.org on macOS: open "/Applications/Python {version}/'
        'Install Certificates.command"\n'
        "  - otherwise: pip install certifi, then\n"
        '      export SSL_CERT_FILE="$(python3 -m certifi)"\n'
        "If a proxy, VPN or antivirus inspects HTTPS on your network, its root\n"
        "certificate must be trusted as well."
    )


def _retry_after(err, default):
    try:
        return float(err.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return default


@dataclass
class MarketData:
    now_ms: int
    symbols: list          # coins that passed the exchangeInfo filters
    klines: dict           # symbol -> [(open_time_ms, high, low, close, quote_volume)]
    failed: dict           # symbol -> error message
    last_funding: dict     # symbol -> lastFundingRate
    funding_hours: dict    # symbol -> fundingIntervalHours (adjusted symbols only)


def fetch_market_data(client):
    now_ms = int(client.get("/fapi/v1/time")["serverTime"])

    info = client.get("/fapi/v1/exchangeInfo")
    symbols = sorted(
        s["symbol"] for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
        and s.get("baseAsset") not in EXCLUDED_BASES
    )

    klines, failed = {}, {}
    for i, symbol in enumerate(symbols, 1):
        try:
            raw = client.get("/fapi/v1/klines", {
                "symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT,
            })
        except FatalApiError:
            raise
        except ApiError as err:
            failed[symbol] = str(err)
            continue
        # Kline: [open time, open, high, low, close, volume, close time, quote volume, ...]
        # Drop the candle that is still open (close time later than now).
        klines[symbol] = [
            (int(k[0]), float(k[2]), float(k[3]), float(k[4]), float(k[7]))
            for k in raw if int(k[6]) <= now_ms
        ]
        if sys.stderr.isatty():
            print(f"\rFetching daily candles {i}/{len(symbols)}", end="",
                  file=sys.stderr, flush=True)
    if sys.stderr.isatty():
        print(file=sys.stderr)

    last_funding = {
        p["symbol"]: float(p["lastFundingRate"])
        for p in client.get("/fapi/v1/premiumIndex")
        if p.get("lastFundingRate") not in (None, "")
    }
    funding_hours = {
        f["symbol"]: int(f["fundingIntervalHours"])
        for f in client.get("/fapi/v1/fundingInfo")
        if f.get("fundingIntervalHours")
    }
    return MarketData(now_ms, symbols, klines, failed, last_funding, funding_hours)


def build_panels(klines):
    """Date x symbol frames of high, low, close and quote volume.

    The index has every calendar day, so a day without a candle is NaN and
    rolling windows count calendar days.
    """
    frames = {}
    for symbol, rows in klines.items():
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["open_time", "high", "low", "close", "qvol"])
        df.index = pd.to_datetime(df.pop("open_time"), unit="ms").dt.normalize()
        frames[symbol] = df[~df.index.duplicated(keep="last")]
    if not frames:
        raise ComputeError("no completed daily candles were downloaded")
    start = min(f.index.min() for f in frames.values())
    end = max(f.index.max() for f in frames.values())
    days = pd.date_range(start, end, freq="D")
    return {
        field: pd.DataFrame({s: f[field] for s, f in frames.items()}).reindex(days)
        for field in ("high", "low", "close", "qvol")
    }


def compute_universes(panels, days):
    """Top 80 eligible coins by 30-day average quote volume, for each day."""
    close, qvol = panels["close"], panels["qvol"]
    has_candle = close.notna()
    n_candles = has_candle.cumsum()
    adv = qvol.rolling(ADV_WINDOW, min_periods=ADV_MIN_DAYS).mean()

    universes, notes = {}, []
    for d in days:
        eligible = (has_candle.loc[d]
                    & (qvol.loc[d] > 0)
                    & (n_candles.loc[d] >= MIN_HISTORY_CANDLES))
        ranked = sorted(adv.loc[d][eligible].dropna().items(),
                        key=lambda kv: (-kv[1], kv[0]))
        universes[d] = [symbol for symbol, _ in ranked[:UNIVERSE_SIZE]]
        if (len(ranked) > UNIVERSE_SIZE
                and ranked[UNIVERSE_SIZE - 1][1] == ranked[UNIVERSE_SIZE][1]):
            notes.append(f"{d:%Y-%m-%d}: tie in 30-day volume at place "
                         f"{UNIVERSE_SIZE}, broken alphabetically")
    return universes, adv, notes


def correlation(x, y):
    """Pearson correlation of A101_055, or NaN with fewer than 4 days."""
    if len(x) < CORR_MIN_DAYS:
        return np.nan
    sx, sy = x.std(), y.std()
    if sx < FLAT_STD or sy < FLAT_STD:
        return 0.0
    c = np.mean((x - x.mean()) * (y - y.mean())) / (sx * sy)
    return float(np.clip(c, -1.0, 1.0))


def compute_a101_055(panels, universes, today):
    high, low, close, qvol = (panels[k] for k in ("high", "low", "close", "qvol"))

    # Step 1: position of the close inside the 12-day range (>= 8 candles).
    ts_min_low = low.rolling(POS_WINDOW, min_periods=POS_MIN_DAYS).min()
    ts_max_high = high.rolling(POS_WINDOW, min_periods=POS_MIN_DAYS).max()
    price_range = ts_max_high - ts_min_low
    pos = (close - ts_min_low) / price_range.where(price_range > 0)

    # Step 2: cross-sectional percentile ranks within each day's universe.
    days = sorted(universes)
    pos_rank = pd.DataFrame(np.nan, index=days, columns=close.columns)
    vol_rank = pd.DataFrame(np.nan, index=days, columns=close.columns)
    for d in days:
        members = universes[d]
        pos_rank.loc[d, members] = pos.loc[d, members].rank(method="average", pct=True)
        vol_rank.loc[d, members] = qvol.loc[d, members].rank(method="average", pct=True)

    # Steps 3-4: 6-day correlation of the two ranks, negated.
    window = pd.date_range(end=today, periods=CORR_WINDOW, freq="D")
    rows = {}
    for symbol in universes[today]:
        x = pos_rank.loc[window, symbol].to_numpy(dtype=float)
        y = vol_rank.loc[window, symbol].to_numpy(dtype=float)
        both = ~np.isnan(x) & ~np.isnan(y)
        rows[symbol] = {
            "a101_055": -correlation(x[both], y[both]) + 0.0,  # + 0.0 turns -0.0 into 0.0
            "corr_days": int(both.sum()),
            "pos_rank": pos_rank.at[today, symbol],
            "vol_rank": vol_rank.at[today, symbol],
        }
    return pd.DataFrame.from_dict(rows, orient="index")


def select_lists(table):
    """Overall rank plus the BUY and SHORT lists (ties at the 16th place kept)."""
    score = table["a101_055"].round(TIE_DECIMALS)
    valid = score.dropna()
    if len(valid) < 2 * LIST_SIZE:
        raise ComputeError(
            f"only {len(valid)} coins have a score; {2 * LIST_SIZE} are needed "
            "for two separate lists of 16")
    # A missing score sits in the middle of the ranking and never makes a list.
    rank = score.fillna(valid.median()).rank(ascending=False, method="min").astype(int)

    ordered = valid.sort_values(ascending=False)
    buy_cut = ordered.iloc[LIST_SIZE - 1]
    short_cut = ordered.iloc[-LIST_SIZE]
    buy = valid[valid >= buy_cut]
    short = valid[valid <= short_cut]
    overlap = sorted(set(buy.index) & set(short.index))
    if overlap:
        raise ComputeError(
            "ties make the BUY and SHORT lists overlap: " + ", ".join(overlap))
    buy = sorted(buy.index, key=lambda s: (-buy[s], s))
    short = sorted(short.index, key=lambda s: (short[s], s))
    return rank, buy, short


def pad(text, width):
    """Left-align text for a terminal, counting wide (e.g. CJK) characters as 2."""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(width - shown, 0)


def fmt_rank(value):
    return "n/a" if pd.isna(value) else f"{value:.3f}"


def fmt_pct(value):
    return "n/a" if pd.isna(value) else f"{value * 100:+.4f}%"


def print_table(title, table, symbols):
    weight = 1 / LIST_SIZE
    print(f"\n{title}: {len(symbols)} coins, equal money per coin "
          f"(1/{LIST_SIZE} = {weight:.2%} of the side)")
    if len(symbols) > LIST_SIZE:
        print(f"  NOTE: a tie at place {LIST_SIZE} puts {len(symbols)} coins on this "
              f"list, so the side totals {len(symbols)}/{LIST_SIZE} = "
              f"{len(symbols) * weight:.2%} at 1/{LIST_SIZE} each.")
    print(f"{'rank':>4}  {'symbol':<18}{'A101_055':>9}  {'pos_rank':>8}  "
          f"{'vol_rank':>8}  {'30d avg $M':>10}  {'funding/day':>12}  flag")
    for symbol in symbols:
        r = table.loc[symbol]
        print(f"{r['rank']:>4}  {pad(symbol, 18)}{r['a101_055']:>+9.4f}  "
              f"{fmt_rank(r['pos_rank']):>8}  {fmt_rank(r['vol_rank']):>8}  "
              f"{r['adv30_usdt'] / 1e6:>10.1f}  {fmt_pct(r['daily_funding']):>12}  "
              f"{r['flag']}")


def print_changes(previous_path, current):
    if not previous_path.exists():
        print(f"\nNo previous-day file ({previous_path.name}); no list changes to show.")
        return
    previous = pd.read_csv(previous_path)
    print(f"\nChanges since {previous_path.name}:")
    for side in ("BUY", "SHORT"):
        before = set(previous.loc[previous["side"] == side, "symbol"])
        after = set(current.loc[current["side"] == side, "symbol"])
        entered = ", ".join(sorted(after - before)) or "none"
        left = ", ".join(sorted(before - after)) or "none"
        print(f"  {side:<5} entered: {entered}")
        print(f"  {side:<5} left:    {left}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rank Binance USDT-margined perpetuals with A101_055 "
                    "(research and paper trading only; public data, no orders).")
    parser.add_argument("--outdir", default=".",
                        help="folder for a101_055_<date>.csv (default: current folder)")
    parser.add_argument("--all", action="store_true",
                        help="also save all universe coins to a101_055_<date>_all.csv")
    args = parser.parse_args(argv)
    outdir = Path(args.outdir)

    print("A101_055 ranking of Binance USDT-margined perpetuals")
    print("Research and paper trading only: public market data, no orders, no API keys.")

    try:
        data = fetch_market_data(BinancePublic())
    except ApiError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    try:
        panels = build_panels(data.klines)
    except ComputeError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    warnings = []
    if data.failed:
        warnings.append(f"daily candles could not be downloaded for {len(data.failed)} "
                        f"coin(s), which are left out: {', '.join(sorted(data.failed))}")

    today = panels["close"].index.max()
    expected = (pd.Timestamp(data.now_ms, unit="ms").normalize()
                - pd.Timedelta(days=1))
    if today != expected:
        warnings.append(f"latest completed candle is {today:%Y-%m-%d}, "
                        f"expected {expected:%Y-%m-%d}")

    days = pd.date_range(end=today, periods=UNIVERSE_DAYS, freq="D")
    universes, adv, notes = compute_universes(panels, days)
    warnings.extend(notes)

    print(f"\nData date (UTC): {today:%Y-%m-%d}  "
          f"(latest completed daily candle; run at "
          f"{datetime.fromtimestamp(data.now_ms / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC)")
    print(f"Universe size: {len(universes[today])} (must be {UNIVERSE_SIZE})")

    short_days = {d: len(u) for d, u in universes.items() if len(u) != UNIVERSE_SIZE}
    if short_days:
        sizes = ", ".join(f"{d:%Y-%m-%d}={n}" for d, n in short_days.items())
        print(f"ERROR: the universe must have {UNIVERSE_SIZE} coins on each of the "
              f"last {UNIVERSE_DAYS} days, but: {sizes}. Not ranking.", file=sys.stderr)
        return 2

    table = compute_a101_055(panels, universes, today)
    missing = table.index[table["a101_055"].isna()]
    print(f"Missing scores: {len(missing)}")
    for symbol in missing:
        warnings.append(f"{symbol} has no score: {table.at[symbol, 'corr_days']} of "
                        f"{CORR_WINDOW} days have both ranks (needs {CORR_MIN_DAYS})")

    try:
        table["rank"], buy, short = select_lists(table)
    except ComputeError as err:
        print(f"ERROR: {err}. Not ranking.", file=sys.stderr)
        return 2

    table["adv30_usdt"] = adv.loc[today, table.index]
    table["funding_interval_h"] = [data.funding_hours.get(s, DEFAULT_FUNDING_HOURS)
                                   for s in table.index]
    table["last_funding_rate"] = [data.last_funding.get(s, np.nan) for s in table.index]
    table["daily_funding"] = table["last_funding_rate"] * 24 / table["funding_interval_h"]
    table["flag"] = ""
    for symbol in short:
        if table.at[symbol, "daily_funding"] < SHORT_FUNDING_FLAG:
            table.at[symbol, "flag"] = "FUNDING < -0.10%/day"
    no_funding = [s for s in buy + short if pd.isna(table.at[s, "last_funding_rate"])]
    if no_funding:
        warnings.append("no lastFundingRate in premiumIndex for: " + ", ".join(no_funding))

    for message in warnings:
        print(f"WARNING: {message}")

    print_table("BUY (top 16)", table, buy)
    print_table("SHORT (bottom 16)", table, short)

    out = pd.concat([table.loc[buy].assign(side="BUY"),
                     table.loc[short].assign(side="SHORT")])
    out = out.rename_axis("symbol").reset_index()
    out["date"] = f"{today:%Y-%m-%d}"
    out["weight"] = 1 / LIST_SIZE
    out["adv30_usdt_millions"] = out["adv30_usdt"] / 1e6
    out["daily_funding_pct"] = out["daily_funding"] * 100
    out = out[["date", "side", "rank", "symbol", "a101_055", "pos_rank", "vol_rank",
               "adv30_usdt_millions", "last_funding_rate", "funding_interval_h",
               "daily_funding_pct", "weight", "flag"]]

    previous_path = outdir / f"a101_055_{today - pd.Timedelta(days=1):%Y-%m-%d}.csv"
    print_changes(previous_path, out)

    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"a101_055_{today:%Y-%m-%d}.csv"
    out.to_csv(path, index=False, float_format="%.8g")
    print(f"\nSaved {path}")

    if args.all:
        full = table.rename_axis("symbol").reset_index()
        full["side"] = full["symbol"].map({**{s: "BUY" for s in buy},
                                           **{s: "SHORT" for s in short}}).fillna("")
        full["date"] = f"{today:%Y-%m-%d}"
        full["adv30_usdt_millions"] = full["adv30_usdt"] / 1e6
        full = full.sort_values(["rank", "symbol"])[[
            "date", "rank", "symbol", "side", "a101_055", "pos_rank", "vol_rank",
            "adv30_usdt_millions"]]
        all_path = outdir / f"a101_055_{today:%Y-%m-%d}_all.csv"
        full.to_csv(all_path, index=False, float_format="%.8g")
        print(f"Saved {all_path} (all {len(full)} universe coins)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
