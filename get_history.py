#!/usr/bin/env python3
"""Download daily candles and funding rates for the A101_055 backtest.

Covers every USDT-margined perpetual Binance has listed, delisted coins
included, so the backtest is not limited to the coins that survived.

Sources (public, no API key):
  * https://data.binance.vision bulk files: monthly 1d klines and monthly
    funding rates, plus daily 1d kline files for months that do not have a
    monthly file yet.
  * https://fapi.binance.com/fapi/v1/fundingRate for funding after the last
    monthly funding file (bulk funding files are monthly only).

Downloaded zips are cached in data/raw and never downloaded twice. Every run
rebuilds data/klines_1d.csv.gz and data/funding.csv.gz from that cache.
"""

import argparse
import csv
import html
import io
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from a101_055 import EXCLUDED_BASES, BinancePublic, log

S3_LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
S3_FILE_URL = "https://data.binance.vision/"
UM = "data/futures/um"
USER_AGENT = "a101-055-backtest/1.0 (research, public market data only)"

THREADS = 48
HTTP_TIMEOUT = 60
MAX_RETRIES = 5

FUNDING_WINDOW_HOURS = 4     # one funding round per window stays under the limit
FUNDING_LIMIT = 1000
FUNDING_PAUSE = 0.25         # /fapi/v1/fundingRate: 500 requests / 5 min / IP

HOUR_MS = 3_600_000


def http_get(url):
    delay = 2.0
    for attempt in range(MAX_RETRIES):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code < 500 and err.code != 429:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if attempt < MAX_RETRIES - 1:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"giving up on {url}")


def s3_list(prefix, folders=False):
    """Keys (or sub-folders) under a data.binance.vision prefix."""
    out, marker = [], ""
    while True:
        query = {"prefix": prefix, "marker": marker}
        if folders:
            query["delimiter"] = "/"
        xml = http_get(S3_LIST_URL + "?" + urllib.parse.urlencode(query)).decode()
        tag = "Prefix" if folders else "Key"
        found = [html.unescape(x) for x in re.findall(f"<{tag}>([^<]*)</{tag}>", xml)]
        if folders:
            found = [x for x in found if x != prefix]
        out += found
        if "<IsTruncated>true</IsTruncated>" not in xml or not found:
            return out
        next_marker = re.findall(r"<NextMarker>([^<]*)</NextMarker>", xml)
        marker = html.unescape(next_marker[0]) if next_marker else found[-1]


def zip_keys(prefix):
    return sorted(k for k in s3_list(prefix) if k.endswith(".zip"))


def month_of(key):
    """'2026-09' from ...-2026-09.zip or ...-2026-09-30.zip."""
    return re.search(r"(\d{4}-\d{2})(?:-\d{2})?\.zip$", key).group(1)


def months_after(month, until):
    out, (y, m) = [], map(int, month.split("-"))
    while True:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if f"{y:04d}-{m:02d}" > until:
            return out
        out.append(f"{y:04d}-{m:02d}")


def excluded_symbols(client):
    """Symbols Binance lists today that are not crypto USDT perpetuals
    (stock, ETF and commodity contracts are TRADIFI_PERPETUAL)."""
    info = client.get("/fapi/v1/exchangeInfo")
    return {s["symbol"] for s in info["symbols"]
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT"}


def keep_symbol(symbol, non_crypto):
    return (symbol.endswith("USDT") and "_" not in symbol
            and symbol[:-4] not in EXCLUDED_BASES and symbol not in non_crypto)


def list_symbol_files(symbol, this_month):
    klines = zip_keys(f"{UM}/monthly/klines/{symbol}/1d/")
    funding = zip_keys(f"{UM}/monthly/fundingRate/{symbol}/")
    daily = []
    if klines:
        recent = months_after(month_of(klines[-1]), this_month)
        if len(recent) <= 2:          # still listed recently: fetch daily files
            for month in recent:
                daily += zip_keys(f"{UM}/daily/klines/{symbol}/1d/{symbol}-1d-{month}")
    return symbol, klines, funding, daily


def download(key, raw_dir):
    path = raw_dir / key
    if path.exists():
        return False
    data = http_get(S3_FILE_URL + urllib.parse.quote(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


def zip_rows(path):
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode()
    for row in csv.reader(io.StringIO(text)):
        if row and row[0][:1].isdigit():   # skip the header newer files have
            yield row


def to_ms(value):
    t = int(value)
    return t // 1000 if t > 10**14 else t   # microsecond timestamps, just in case


def api_funding(client, start_ms, end_ms):
    """All symbols' funding events in [start_ms, end_ms), in 4-hour windows."""
    rows = []
    step = FUNDING_WINDOW_HOURS * HOUR_MS
    for lo in range(start_ms, end_ms, step):
        hi = min(lo + step, end_ms) - 1
        batch = client.get("/fapi/v1/fundingRate",
                           {"startTime": lo, "endTime": hi, "limit": FUNDING_LIMIT})
        if len(batch) >= FUNDING_LIMIT:
            raise RuntimeError(f"funding window at {lo} hit the {FUNDING_LIMIT}-row "
                               "limit; make FUNDING_WINDOW_HOURS smaller")
        rows += [(b["symbol"], int(b["fundingTime"]), float(b["fundingRate"]))
                 for b in batch]
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--datadir", default="data")
    args = parser.parse_args(argv)
    datadir = Path(args.datadir)
    raw_dir = datadir / "raw"

    client = BinancePublic()
    non_crypto = excluded_symbols(client)
    all_symbols = [p.rstrip("/").split("/")[-1]
                   for p in s3_list(f"{UM}/monthly/klines/", folders=True)]
    symbols = sorted(s for s in all_symbols if keep_symbol(s, non_crypto))
    log(f"{len(all_symbols)} futures symbols in bulk data, {len(symbols)} crypto USDT "
        f"perpetuals kept ({len(non_crypto)} non-crypto or non-USDT contracts skipped)")

    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    files = {}
    with ThreadPoolExecutor(THREADS) as pool:
        for i, (symbol, *keys) in enumerate(
                pool.map(lambda s: list_symbol_files(s, this_month), symbols), 1):
            files[symbol] = keys
            if sys.stderr.isatty():
                print(f"\rListing files {i}/{len(symbols)}", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print(file=sys.stderr)

    keys = [k for kl, fu, da in files.values() for k in kl + fu + da]
    missing = [k for k in keys if not (raw_dir / k).exists()]
    log(f"{len(keys)} files listed, {len(missing)} to download")
    done = 0
    with ThreadPoolExecutor(THREADS) as pool:
        for _ in pool.map(lambda k: download(k, raw_dir), missing):
            done += 1
            if sys.stderr.isatty() and (done % 200 == 0 or done == len(missing)):
                print(f"\rDownloading {done}/{len(missing)}", end="", file=sys.stderr)
    if sys.stderr.isatty() and missing:
        print(file=sys.stderr)

    # Candles: monthly files first, daily files fill the months not yet published.
    candles = []
    for symbol, (monthly, _, daily) in files.items():
        for key in monthly + daily:
            for r in zip_rows(raw_dir / key):
                candles.append((symbol, to_ms(r[0]), float(r[1]), float(r[2]),
                                float(r[3]), float(r[4]), float(r[7])))
    k = pd.DataFrame(candles, columns=["symbol", "open_time", "open", "high", "low",
                                       "close", "quote_volume"])
    k["date"] = pd.to_datetime(k.pop("open_time"), unit="ms").dt.normalize()
    k = (k.drop_duplicates(["symbol", "date"], keep="last")
          .sort_values(["date", "symbol"])
          [["symbol", "date", "open", "high", "low", "close", "quote_volume"]])
    k.to_csv(datadir / "klines_1d.csv.gz", index=False, date_format="%Y-%m-%d")

    # Funding: monthly bulk files, then the API after the last published month.
    funding = []
    for symbol, (_, monthly_funding, _) in files.items():
        for key in monthly_funding:
            funding += [(symbol, to_ms(r[0]), float(r[2]))
                        for r in zip_rows(raw_dir / key)]
    last_month = max(month_of(key) for _, fu, _ in files.values() for key in fu)
    api_start = pd.Timestamp(last_month + "-01", tz="UTC") + pd.offsets.MonthBegin(1)
    api_start_ms = int(api_start.timestamp() * 1000)
    now_ms = int(client.get("/fapi/v1/time")["serverTime"])
    log(f"bulk funding files end {last_month}; fetching funding since "
        f"{api_start:%Y-%m-%d} from the API")
    kept = set(symbols)
    funding += [row for row in api_funding(BinancePublic(pause=FUNDING_PAUSE),
                                           api_start_ms, now_ms)
                if row[0] in kept]
    f = pd.DataFrame(funding, columns=["symbol", "funding_time", "funding_rate"])
    f["hour"] = (f["funding_time"] + HOUR_MS // 2) // HOUR_MS
    f = (f.drop_duplicates(["symbol", "hour"], keep="last")
          .drop(columns="hour").sort_values(["funding_time", "symbol"]))
    f.to_csv(datadir / "funding.csv.gz", index=False)

    print(f"Candles: {len(k):,} rows, {k['symbol'].nunique()} symbols, "
          f"{k['date'].min():%Y-%m-%d} to {k['date'].max():%Y-%m-%d} "
          f"-> {datadir / 'klines_1d.csv.gz'}")
    print(f"Funding: {len(f):,} rows, {f['symbol'].nunique()} symbols "
          f"-> {datadir / 'funding.csv.gz'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
