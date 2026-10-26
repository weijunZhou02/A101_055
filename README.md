# A101_055 ranker for Binance USDT-margined perpetuals

Ranks Binance USDT-margined perpetual futures with alpha **A101_055** and lists the
top 16 coins (BUY) and the bottom 16 coins (SHORT) for the latest completed day.

```
A101_055 = -1 * correlation(rank((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12))),
                            rank(volume), 6)
```

> **Research and paper trading only.** The script uses only public market-data endpoints
> of `https://fapi.binance.com`. It never places orders and never asks for API keys.

| File | What it does |
|---|---|
| `a101_055.py` | Today's BUY and SHORT lists (public data, no keys) |
| `get_history.py`, `backtest.py` | Download the full history and backtest the lists ([Backtest](#backtest)) |
| `paper_trade.py` | A simulated account on live prices; nothing is sent to Binance ([Paper trading](#paper-trading)) |
| `demo_trade.py` | Trades the lists on a Binance Futures **demo** account, fake money, demo API key ([Demo trading](#demo-trading-binance-futures-demo-account-fake-money)) |
| `schedule_mac.sh` | Runs `demo_trade.py` every day on a Mac ([Daily schedule](#daily-schedule-on-a-mac)) |
| `dashboard.py` | Builds `demo/dashboard.html`: money line, long vs short profit, holdings and daily lists ([Dashboard](#dashboard)) |

Only `demo_trade.py` places orders, and only on Binance's demo exchange.

## Usage

```bash
pip install -r requirements.txt
python3 a101_055.py                 # writes a101_055_<date>.csv in the current folder
python3 a101_055.py --outdir runs   # or in another folder
python3 a101_055.py --all           # also save all 80 ranked coins to a101_055_<date>_all.csv
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
- **Exit code 1:** the API is unreachable, blocked from your location (HTTP 451), has
  banned your IP, or the HTTPS certificate can't be verified. The last one usually means
  your Python has no CA certificates: with Python from python.org on macOS, open
  `/Applications/Python 3.x/Install Certificates.command` once.
- **Warnings:** coins whose candles failed to download, coins with a missing score (and
  why), a latest candle older than yesterday (UTC), a tie at the 80th place of a universe
  (broken alphabetically), and missing funding rates.
- **Ties at the 16th place:** the extra coins are kept at 1/16 each, and the table notes
  that the side then totals more than 100%.

Scores that agree to 12 decimal places count as a tie.

## Backtest

```bash
python3 get_history.py          # downloads history into data/ (first run ~30 min, then minutes)
python3 backtest.py             # writes backtest_out/
python3 backtest.py --check a101_055_2026-10-24.csv   # also compare with a saved live list
```

**Data** (`get_history.py`): daily candles and funding for every crypto USDT perpetual
Binance has listed, **delisted coins included**, from the bulk files at
[data.binance.vision](https://github.com/binance/binance-public-data/) (monthly files, plus
daily candle files for the current month) and `/fapi/v1/fundingRate` for funding in months
without a bulk file. Stock, ETF and commodity contracts (`TRADIFI_PERPETUAL`) and the
excluded bases are left out. Zips are cached in `data/raw`, so later runs only fetch new files.

**Lists**: for each day *d*, `backtest.py` calls this script's own `compute_universes`,
`compute_a101_055` and `select_lists` on the 99 completed candles the live script downloads,
among the coins still trading at the next open. So each day gets the lists `a101_055.py`
would have printed that night. Days where the live script would refuse (a universe under
80, too few scores, overlapping lists) are held in cash.

**Trading**: BUY the top 16 and SHORT the bottom 16 at the open of *d*+1 (00:00 UTC), half
the equity per side and equal money per coin, held until the next open. Day return =
half × long-side return + half × short-side return − costs + funding. Days are chained by
multiplying.

**Costs**: `--fee-bps` (default 5, taker) plus `--slippage-bps` (default 2) on all traded
notional, including the trades that bring drifted weights back to equal. Funding is the
actual history: a funding event is paid by the position held at that moment (the 00:00 UTC
event belongs to the previous day's position).

**Report** (`backtest_out/summary.txt`): rank IC (next day, decay over 1–10 days, by year),
quintile returns, net and gross performance against BTC and the equal-weight universe
(Sharpe uses 365 days), where the return comes from (long side, short side, fees, funding),
turnover and break-even cost, and a by-year table. `daily.csv` has one row per day,
`positions.csv` one row per coin per day, and `equity.png` is the chart.

`--start` and `--end` limit the signal dates. The parameters are the stock-market
Alpha101 values; if you start tuning them, keep a recent period untouched for a final test.

## Paper trading

```bash
python3 paper_trade.py            # once a day, soon after 00:00 UTC (11:00 Sydney in summer)
python3 paper_trade.py --status   # the account at current prices, no trading
```

A simulated account (default $10,000, `--capital` on the first run) trades the same lists
as the backtest, with live Binance prices. Nothing is sent to Binance and no API key is
used. Each run:

1. gets the lists for the day that just closed (runs `a101_055.py` into `paper/lists/`),
2. settles the positions at current mid prices and books the funding actually charged
   since the last run (`/fapi/v1/fundingRate`, with Binance's mark price at each event),
3. rebalances: half the equity per side, equal money per coin. Each order walks the live
   order book (`/fapi/v1/depth`, 50 levels) for its size and pays the fee (`--fee-bps`,
   default 5). Quantities are rounded to the market step size, and trades under the minimum
   order size are skipped (closing is always allowed), as on the real exchange,
4. compares the day's result before costs with the backtest model: the same positions'
   00:00-to-00:00 candle returns. Runs more than 2 hours after 00:00 UTC are left out of
   that comparison.

A second run for the same lists is refused, so running it twice is harmless. If a day is
missed, the positions are simply held until the next run.

Files in `paper/`: `state.json` (balance, positions), `ledger.csv` (one row per run:
P&L by side, funding, fees, slippage in bps, turnover, paper vs model), `trades.csv` (one
row per simulated fill, with the mid price, fill price and book levels used).

What to judge it by: the daily paper-vs-model difference and the real slippage in bps
(the backtest assumes 2), not the profit. A few months of P&L cannot separate a Sharpe
of about 0.4 from luck.

## Demo trading (Binance Futures demo account, fake money)

`demo_trade.py` trades the same lists on your Binance Futures **demo** account, so you can
see the positions in Binance's own interface (demo.binance.com). It only ever calls
`https://demo-fapi.binance.com`; real-account keys do not work there.

One-time setup:
1. Log in to Binance, open **Binance Demo Trading**, and create an API key on its **API Key
   Management** page (system-generated / HMAC, with futures trading allowed).
2. In the futures settings of the demo account, use **One-way** position mode.
3. Save the key in `.env` (private, git-ignored, readable only by you). Either open `.env` and
   fill in `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET`, or paste them hidden with:
   ```bash
   python3 demo_trade.py --set-keys
   ```

Daily, soon after 00:00 UTC:
```bash
python3 demo_trade.py              # rebalance the demo account
python3 demo_trade.py --status     # equity and positions, no trading
python3 demo_trade.py --dry-run    # the orders for a $10,000 empty account, no keys needed
python3 demo_trade.py --again      # rebalance again to today's lists (after a failed order)
python3 demo_trade.py --close-all  # close every demo position (stop)
```

The lists come from live market data, exactly as in `a101_055.py`. Orders are sized at the
live price and sent as LIMIT IOC orders capped at `--max-slippage-bps` (default 100) from
the live mid, because the demo order book is thinner and its prices differ from the live
market (spreads of 50–75 bps on some coins in a check on 2026-10-26). Anything that does
not fill inside the cap is reported. Coins that are not listed on demo are left out.
Before opening a coin the script sets its leverage to 3x: exposure stays 1x of equity, but
a high default leverage caps the allowed position size. Fill prices and fees come from the
trade history (`/fapi/v1/userTrades`), and funding from `/fapi/v1/income`. On the first run
it refuses to touch positions already on the account unless you pass `--close-existing`.

Files in `demo/`: `state.json`, `ledger.csv` (equity, funding, fees, demo vs backtest model,
average demo fill vs the live price), `orders.csv` (every order with the live mid next to
the demo fill), `demo_trade.log` (output of scheduled runs).

### Dashboard

`demo/dashboard.html` shows the demo account:

- **Tiles:** equity, return since the start, open profit or loss, number of holdings.
- **Money line** (button **Account**): account value over time next to the same starting
  money held in BTC.
- **Long vs short:** each side's price profit since the start (realized from closed or
  reduced positions plus open profit), before fees and funding. Realized profit comes from
  the `realized_pnl` of each fill in `orders.csv`: a SELL reduces a long, a BUY a short.
- **What I hold** (**Show holdings**): each coin's side, quantity, entry price (**my price**,
  as Binance reports it), price now, value and profit or loss. Click a column to sort.
- **Daily lists** (**Show daily lists**): for each run, all 80 ranked coins with the score,
  the BUY / SHORT tag, price position and volume rank, and the rank change since the day
  before (▲ up, ▼ down, *new* = not in the previous universe), plus which coins entered or
  left each list. Pick the day from the menu; filter to the 16 BUY or 16 SHORT coins. It
  reads the `a101_055_<date>_all.csv` files that `demo_trade.py` saves in `demo/lists/`.

The buttons at the top jump to a section. Adding `#sides`, `#holdings` or `#lists` to the
address opens that view straight away.

`demo_trade.py` rebuilds the page after every run, including the scheduled runs that don't
trade, so the money line gets three points a day. To rebuild it yourself:

```bash
python3 dashboard.py && open demo/dashboard.html
```

Building the page uses your demo key (on your Mac). The page itself holds no keys: while it
is open it fetches public mark prices from `demo-fapi.binance.com` every minute, so prices,
profit and equity stay current until the next rebuild. Positions change only when
`demo_trade.py` runs, and that rebuilds the page.

### Daily schedule on a Mac

```bash
./schedule_mac.sh            # install: runs demo_trade.py at 10:05, 11:05 and 12:05 local time
./schedule_mac.sh --status   # loaded? how many runs, last exit code
./schedule_mac.sh --remove   # remove the schedule
```

It uses launchd, the scheduler built into macOS (a file in `~/Library/LaunchAgents`). Only the
first run after 00:00 UTC trades (11:00 in Sydney during daylight saving, 10:00 otherwise);
the later ones see the lists are done and exit. The Mac must be on: if it is asleep, the run
happens when it wakes (more than 2 hours after 00:00 UTC counts as late), and if it is off
all day, that day is skipped and the positions are held. `caffeinate` keeps it awake while
a run is going. To wake the Mac automatically every day (asks for your password; works best
plugged in; undo with `sudo pmset repeat cancel`):

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 11:04:00
```
