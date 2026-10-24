# A101_055 ranker for Binance USDT-margined perpetuals

Ranks Binance USDT-margined perpetual futures with alpha **A101_055** and lists the
top 16 coins (BUY) and the bottom 16 coins (SHORT) for the latest completed day.

```
A101_055 = -1 * correlation(rank((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12))),
                            rank(volume), 6)
```

> **Research and paper trading only.** The script uses only public market-data endpoints
> of `https://fapi.binance.com`. It never places orders and never asks for API keys.

## Usage

```bash
pip install -r requirements.txt
python3 a101_055.py                 # writes a101_055_<date>.csv in the current folder
python3 a101_055.py --outdir runs   # or in another folder
```

Python 3.9+. A run downloads about 520 daily-candle series and takes about 2 minutes.

## Method

**Coins** (`/fapi/v1/exchangeInfo`): contractType `PERPETUAL`, quoteAsset `USDT`, status
`TRADING`, excluding bases USDC BUSD TUSD USDP FDUSD DAI USDE USD1 PYUSD EUR AEUR PAXG XAUT
BTCDOM DEFI BLUEBIRD FOOTBALL.

**Candles** (`/fapi/v1/klines`, interval `1d`, limit 100): the candle that is still open is
dropped. "Volume" is always quote volume (USDT). Calls are paced about 0.1 s apart. HTTP
429/418 responses are retried after `Retry-After`. If Binance asks for a wait longer than
15 minutes (an IP ban), the run stops and reports it.

**Daily universe**, computed for each of the last 10 completed days *d*:
a coin is eligible if it has a candle on *d* with quote volume > 0 and at least 60 daily
candles up to and including *d*. Eligible coins are ranked by average quote volume over the
30 days ending *d* (at least 15 days needed). The top 80 form day *d*'s universe.

**A101_055**
1. `pos` = (close − lowest low of 12 days ending *d*) / (highest high − lowest low); needs at
   least 8 of the 12 days; a zero range gives a missing value.
2. Each day, `pos` and quote volume are ranked as percentiles in (0, 1] (ties averaged)
   among that day's 80 universe coins. Coins outside the universe have no rank that day.
3. Pearson correlation of the two rank series over the 6 days ending today, using days
   where both exist. At least 4 such days are needed. A flat series (std < 1e-12) gives 0.
   The result is clipped to [−1, 1].
4. A101_055 = −correlation. Higher is better (buy).

**Lists** (latest completed day, its 80 universe coins): sorted by score, top 16 = BUY,
bottom 16 = SHORT, equal money per coin (1/16 of each side). If coins tie at the 16th
place, all of them are listed. A missing score is placed in the middle of the ranking and
never makes either list.

**Daily funding** = `lastFundingRate` (`/fapi/v1/premiumIndex`) × 24 / `fundingIntervalHours`
(`/fapi/v1/fundingInfo`, default 8). SHORT coins with daily funding below −0.10% are
flagged but kept.

## Output

The script prints the data date (UTC), the universe size (must be 80), the number of
missing scores and both tables. It then lists the coins that entered or left each list
since the previous day's file (`a101_055_<date-1>.csv`, if present in the output folder),
and saves `a101_055_<date>.csv` with these columns:

| column | meaning |
|---|---|
| `date`, `side` | data date, BUY or SHORT |
| `rank` | rank among the 80 universe coins (1 = highest score) |
| `a101_055` | the score |
| `pos_rank`, `vol_rank` | today's percentile ranks of `pos` and quote volume |
| `adv30_usdt_millions` | 30-day average quote volume, millions of USDT |
| `last_funding_rate`, `funding_interval_h`, `daily_funding_pct` | funding inputs and daily funding in % |
| `weight` | 1/16 |
| `flag` | `FUNDING < -0.10%/day` on SHORT coins with heavy negative funding |

## When something can't be computed

The script reports the problem instead of guessing:

- **Exit code 2, no lists:** a universe on any of the last 10 days has fewer than 80
  coins, fewer than 32 coins have a score, or ties make the BUY and SHORT lists overlap.
- **Exit code 1:** the API is unreachable, blocked from your location (HTTP 451), or has
  banned your IP.
- **Warnings:** coins whose candles failed to download, coins with a missing score (and
  why), a latest candle older than yesterday (UTC), a tie at the 80th place of a universe
  (broken alphabetically), and missing funding rates.
- **Ties at the 16th place:** the extra coins are kept at 1/16 each, and the table notes
  that the side then totals more than 100%.

Scores that agree to 12 decimal places count as a tie.
