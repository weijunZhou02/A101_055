#!/usr/bin/env python3
"""Backtest A101_055 on Binance USDT-margined perpetuals.

For each day d the script rebuilds what a101_055.py would have listed right
after d closed: it calls the live script's own functions on the 99 completed
daily candles the live script downloads, among the coins still trading at the
next open. It then BUYs the top 16 and SHORTs the bottom 16 at the open of d+1
(00:00 UTC), half the equity on each side and equal money per coin (1/32 of
equity; a tie at place 16 splits the side's half among more coins), and holds
until the next open.

Costs: fees plus slippage on all traded notional (list changes and the trades
that bring drifted weights back to equal), and the funding actually paid or
received. Annualised figures use 365 days.

Research only. Run get_history.py first.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import a101_055 as live

DAYS_PER_YEAR = 365
LIVE_CANDLES = live.KLINE_LIMIT - 1      # the live script drops the open candle
SIDE_WEIGHT = 0.5                        # half the equity long, half short
IC_LAGS = (1, 2, 3, 5, 10)
QUANTILES = 5
IC_MIN_COINS = 10
DEFAULT_FEE_BPS = 5.0                    # taker fee, regular account
DEFAULT_SLIPPAGE_BPS = 2.0


def load_data(datadir):
    k = pd.read_csv(datadir / "klines_1d.csv.gz", parse_dates=["date"])
    days = pd.date_range(k["date"].min(), k["date"].max(), freq="D")
    panels = {
        name: k.pivot(index="date", columns="symbol", values=column).reindex(days)
        for name, column in (("open", "open"), ("high", "high"), ("low", "low"),
                             ("close", "close"), ("qvol", "quote_volume"))
    }
    f = pd.read_csv(datadir / "funding.csv.gz")
    # A funding event is paid by the position held at that moment, so the
    # 00:00 UTC event belongs to the position opened the day before.
    hour = pd.to_datetime(f["funding_time"], unit="ms").dt.round("h")
    f["date"] = (hour - pd.Timedelta(milliseconds=1)).dt.normalize()
    funding = (f.pivot_table(index="date", columns="symbol", values="funding_rate",
                             aggfunc="sum")
               .reindex(index=days, columns=panels["close"].columns))
    return panels, funding


def live_lists(panels, day, trading):
    """What a101_055.py would have printed right after `day` closed.

    Returns (table, buy, short, problem); problem is None when lists exist.
    """
    columns = trading.index[trading]
    start = day - pd.Timedelta(days=LIVE_CANDLES - 1)
    sliced = {f: panels[f].loc[start:day, columns]
              for f in ("high", "low", "close", "qvol")}
    days = pd.date_range(end=day, periods=live.UNIVERSE_DAYS, freq="D")
    universes, _, _ = live.compute_universes(sliced, days)
    if any(len(u) != live.UNIVERSE_SIZE for u in universes.values()):
        return None, None, None, f"universe under {live.UNIVERSE_SIZE} coins"
    table = live.compute_a101_055(sliced, universes, day)
    try:
        table["rank"], buy, short = live.select_lists(table)
    except live.ComputeError as err:
        return table, None, None, str(err).split(":")[0]
    return table, buy, short, None


def trading_at_open(has_candle, day):
    """Coins that can be traded at the open after `day`: those with a candle
    the next day (the live script sees them as TRADING then)."""
    nxt = day + pd.Timedelta(days=1)
    return has_candle.loc[nxt] if nxt in has_candle.index else has_candle.loc[day]


def run(panels, funding, start, end, cost_rate):
    has_candle = panels["close"].notna()
    ret = panels["close"] / panels["open"] - 1
    index = panels["close"].index
    first = index[0] + pd.Timedelta(days=LIVE_CANDLES - 1)
    signal_days = index[(index >= max(first, start)) & (index <= end)]

    records, positions, scores, skipped = [], [], [], {}
    w_prev = pd.Series(dtype=float)
    n = len(signal_days)
    for i, day in enumerate(signal_days, 1):
        if sys.stderr.isatty() and (i % 25 == 0 or i == n):
            print(f"\rScoring {day:%Y-%m-%d} ({i}/{n})", end="", file=sys.stderr)
        nxt = day + pd.Timedelta(days=1)
        if nxt not in index:
            break
        table, buy, short, problem = live_lists(panels, day,
                                                trading_at_open(has_candle, day))
        if table is not None:
            scores.append(table["a101_055"].rename(day))
        if problem and not records:
            continue                      # still warming up: no trading yet
        if problem:
            skipped[problem] = skipped.get(problem, 0) + 1
            buy, short = [], []
        w = pd.concat([pd.Series(SIDE_WEIGHT / len(buy), index=buy) if buy else None,
                       pd.Series(-SIDE_WEIGHT / len(short), index=short) if short else None])
        w = w if len(w) else pd.Series(dtype=float)

        r = ret.loc[nxt, w.index]
        fr = funding.loc[nxt, w.index]
        missing_ret, missing_funding = int(r.isna().sum()), int(fr.isna().sum())
        r, fr = r.fillna(0.0), fr.fillna(0.0)
        union = w.index.union(w_prev.index)
        turnover = (w.reindex(union, fill_value=0.0)
                    - w_prev.reindex(union, fill_value=0.0)).abs().sum()
        gross = float((w * r).sum())
        long_funding = float(-(w[buy] * fr[buy]).sum()) if buy else 0.0
        short_funding = float(-(w[short] * fr[short]).sum()) if short else 0.0
        trading_cost = turnover * cost_rate
        net = gross - trading_cost + long_funding + short_funding
        universe = table.index if table is not None else []
        records.append({
            "trade_date": nxt, "signal_date": day,
            "n_long": len(buy), "n_short": len(short),
            "long_ret": r[buy].mean() if buy else 0.0,
            "short_ret": -r[short].mean() if short else 0.0,
            "gross_ret": gross, "turnover": turnover, "trading_cost": trading_cost,
            "long_funding": long_funding, "short_funding": short_funding,
            "net_ret": net,
            "ew_universe_ret": ret.loc[nxt, universe].mean() if len(universe) else np.nan,
            "btc_ret": ret.loc[nxt, "BTCUSDT"] if "BTCUSDT" in ret.columns else np.nan,
            "missing_ret": missing_ret, "missing_funding": missing_funding,
            "skipped": problem or "",
        })
        for side, symbols in (("BUY", buy), ("SHORT", short)):
            for s in symbols:
                positions.append((nxt, day, side, s, table.at[s, "a101_055"],
                                  w[s], r[s], fr[s]))
        w_prev = w * (1 + r) / (1 + net)
    if sys.stderr.isatty():
        print(file=sys.stderr)

    daily = pd.DataFrame(records).set_index("trade_date")
    daily["equity_gross"] = (1 + daily["gross_ret"]).cumprod()
    daily["equity_net"] = (1 + daily["net_ret"]).cumprod()
    positions = pd.DataFrame(positions, columns=[
        "trade_date", "signal_date", "side", "symbol", "a101_055", "weight",
        "ret", "funding_rate_sum"])
    score_panel = pd.DataFrame(scores) if scores else pd.DataFrame()
    return daily, positions, score_panel, ret, skipped


def rank_ic(scores, returns):
    """Daily Spearman correlation between scores and returns across coins."""
    mask = scores.notna() & returns.notna()
    a = scores.where(mask).rank(axis=1)
    b = returns.where(mask).rank(axis=1)
    a, b = a.sub(a.mean(axis=1), axis=0), b.sub(b.mean(axis=1), axis=0)
    ic = (a * b).sum(axis=1) / np.sqrt((a ** 2).sum(axis=1) * (b ** 2).sum(axis=1))
    return ic.where(mask.sum(axis=1) >= IC_MIN_COINS)


def perf(r):
    r = r.dropna()
    equity = (1 + r).cumprod()
    years = len(r) / DAYS_PER_YEAR
    std = r.std()
    return {
        "cagr": equity.iloc[-1] ** (1 / years) - 1 if equity.iloc[-1] > 0 else -1.0,
        "vol": std * np.sqrt(DAYS_PER_YEAR),
        "sharpe": r.mean() / std * np.sqrt(DAYS_PER_YEAR) if std > 0 else np.nan,
        "max_dd": (equity / equity.cummax() - 1).min(),
        "worst_day": r.min(), "best_day": r.max(), "up_days": (r > 0).mean(),
    }


def pct(x, digits=1):
    return "n/a" if pd.isna(x) else f"{x * 100:+.{digits}f}%"


def report(daily, positions, scores, ret, skipped, panels, args):
    out = []
    say = out.append
    period = (f"{daily.index[0]:%Y-%m-%d} to {daily.index[-1]:%Y-%m-%d} "
              f"({len(daily)} trading days, {len(daily) / DAYS_PER_YEAR:.1f} years)")
    say("A101_055 backtest: Binance USDT-margined perpetuals, daily long-short")
    say(f"Period: {period}")
    say(f"Entry at the next 00:00 UTC open; costs {args.fee_bps:g} bps fee + "
        f"{args.slippage_bps:g} bps slippage per unit traded, plus actual funding.")

    # Data and survivorship (delisted contracts can keep printing zero-volume
    # candles, so "still trading" means volume on the last day)
    traded = panels["qvol"].where(panels["qvol"] > 0)
    last_traded = traded.apply(lambda c: c.last_valid_index())
    ever = sorted(set(positions["symbol"]) | set(scores.columns))
    gone = [s for s in ever if last_traded[s] < traded.index[-1]]
    say("")
    say("DATA")
    say(f"  Coins ever in the top-80 universe: {len(ever)}; no longer trading today: "
        f"{len(gone)} (kept in the test, so it is not limited to survivors)")
    flat = (daily["skipped"] != "").sum()
    say(f"  Days with no lists (held cash): {flat}"
        + (" - " + ", ".join(f"{k}: {v}" for k, v in skipped.items()) if skipped else ""))
    held = len(positions)
    say(f"  Positions: {held:,}; missing next-day candle: "
        f"{int(daily['missing_ret'].sum())}; missing funding: "
        f"{int(daily['missing_funding'].sum())} (counted as 0)")

    # Signal quality
    scores = scores.loc[scores.index.isin(daily["signal_date"])]
    say("")
    say("SIGNAL: rank IC (does a higher score mean a higher return?)")
    ics = {}
    for lag in IC_LAGS:
        future = ret.shift(-lag).reindex(index=scores.index, columns=scores.columns)
        ics[lag] = rank_ic(scores, future).dropna()
    ic1 = ics[1]
    t_stat = ic1.mean() / ic1.std() * np.sqrt(len(ic1))
    say(f"  Next day:  mean {ic1.mean():+.4f}   t-stat {t_stat:+.2f}   "
        f"positive on {(ic1 > 0).mean():.0%} of days")
    say("  Decay:     " + "   ".join(f"day+{lag} {ic.mean():+.4f}" for lag, ic in ics.items()))
    by_year_ic = ic1.groupby(ic1.index.year).mean()

    # Quintiles
    future = ret.shift(-1).reindex(index=scores.index, columns=scores.columns)
    bucket = np.ceil(scores.rank(axis=1, ascending=False, pct=True) * QUANTILES)
    say("")
    say(f"QUINTILES: average next-day return, {QUANTILES} groups of "
        f"{live.UNIVERSE_SIZE // QUANTILES} coins (Q1 = highest scores = BUY list)")
    q_means = {}
    for q in range(1, QUANTILES + 1):
        q_daily = future.where(bucket == q).mean(axis=1).dropna()
        q_means[q] = q_daily
        say(f"  Q{q}: {q_daily.mean() * 1e4:+7.2f} bps/day   "
            f"{q_daily.mean() * DAYS_PER_YEAR * 100:+7.1f}% a year (simple)")
    spread = (q_means[1] - q_means[QUANTILES]).dropna()
    say(f"  Q1 - Q{QUANTILES}: {spread.mean() * 1e4:+.2f} bps/day with full money on "
        f"each side (the portfolio puts half on each side)")

    # Portfolio
    say("")
    say("PORTFOLIO (half the equity long, half short)")
    say(f"  {'':22}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}{'max DD':>9}"
        f"{'worst day':>11}{'up days':>9}")
    rows = [("Long-short, net", daily["net_ret"]),
            ("Long-short, gross", daily["gross_ret"]),
            ("BTC buy and hold", daily["btc_ret"]),
            ("Equal-weight universe", daily["ew_universe_ret"])]
    for name, series in rows:
        p = perf(series)
        say(f"  {name:<22}{pct(p['cagr']):>8}{pct(p['vol'], 0):>8}"
            f"{p['sharpe']:>8.2f}{pct(p['max_dd']):>9}{pct(p['worst_day']):>11}"
            f"{p['up_days']:>9.0%}")
    yearly = lambda s: s.mean() * DAYS_PER_YEAR
    say("")
    say("  Where the return comes from (average a year, simple sum, % of equity):")
    say(f"    long side price moves   {pct(yearly(SIDE_WEIGHT * daily['long_ret'])):>8}")
    say(f"    short side price moves  {pct(yearly(SIDE_WEIGHT * daily['short_ret'])):>8}")
    say(f"    fees + slippage         {pct(-yearly(daily['trading_cost'])):>8}")
    say(f"    funding, long side      {pct(yearly(daily['long_funding'])):>8}")
    say(f"    funding, short side     {pct(yearly(daily['short_funding'])):>8}")
    say(f"    net                     {pct(yearly(daily['net_ret'])):>8}")
    turnover = daily["turnover"].mean()
    breakeven = ((daily["gross_ret"] + daily["long_funding"] + daily["short_funding"])
                 .mean() / turnover)
    say(f"  Turnover: {turnover:.0%} of equity traded a day. Break-even cost: "
        f"{breakeven * 1e4:.1f} bps per unit traded (you assumed "
        f"{args.fee_bps + args.slippage_bps:g}).")

    # By year
    say("")
    say("BY YEAR (compounded; long/short side = return on that side's money)")
    say(f"  {'year':<6}{'net':>9}{'gross':>9}{'long side':>11}{'short side':>12}"
        f"{'EW univ':>9}{'BTC':>9}{'IC':>8}{'days':>6}")
    compound = lambda s: (1 + s).prod() - 1
    for year, g in daily.groupby(daily.index.year):
        say(f"  {year:<6}{pct(compound(g['net_ret'])):>9}{pct(compound(g['gross_ret'])):>9}"
            f"{pct(compound(g['long_ret'])):>11}{pct(compound(g['short_ret'])):>12}"
            f"{pct(compound(g['ew_universe_ret'])):>9}{pct(compound(g['btc_ret'])):>9}"
            f"{by_year_ic.get(year, np.nan):>+8.3f}{len(g):>6}")
    return out


def check_live(path, panels):
    """Compare the backtest's lists for a day with a saved a101_055.py output."""
    saved = pd.read_csv(path)
    day = pd.Timestamp(saved["date"].iloc[0])
    has_candle = panels["close"].notna()
    table, buy, short, problem = live_lists(panels, day, trading_at_open(has_candle, day))
    lines = ["", f"CHECK against {Path(path).name} (data date {day:%Y-%m-%d})"]
    if problem:
        return lines + [f"  The backtest could not rank this day: {problem}"]
    ok = True
    for side, ours in (("BUY", buy), ("SHORT", short)):
        theirs = set(saved.loc[saved["side"] == side, "symbol"])
        same = len(theirs & set(ours))
        ok &= same == len(theirs) == len(ours)
        extra = ", ".join(sorted(set(ours) - theirs)) or "none"
        lacking = ", ".join(sorted(theirs - set(ours))) or "none"
        lines.append(f"  {side:<5}: {same}/{len(theirs)} same coins "
                     f"(only in backtest: {extra}; only in saved file: {lacking})")
    both = saved.set_index("symbol").join(table, rsuffix="_bt", how="inner")
    for column in ("a101_055", "pos_rank", "vol_rank", "rank"):
        diff = (both[column] - both[column + "_bt"]).abs().max()
        ok &= diff < 1e-6
        lines.append(f"  {column:<9} largest difference: {diff:.2g}")
    lines.append("  RESULT: " + ("MATCH" if ok else "DIFFERENT"))
    return lines


def chart(daily, path, args):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError:
        return "matplotlib is not installed; no chart."

    ink, ink2, muted, grid, axis = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
    blue, orange, aqua, surface = "#2a78d6", "#eb6834", "#1baf7a", "#fcfcfb"
    plt.rcParams.update({
        "font.family": ["Helvetica Neue", "Arial", "DejaVu Sans"], "font.size": 10,
        "axes.edgecolor": axis, "axes.labelcolor": ink2, "xtick.color": muted,
        "ytick.color": muted, "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": grid,
        "grid.linewidth": 0.6, "figure.facecolor": surface, "axes.facecolor": surface,
    })
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1.3, 2]})
    x = daily.index

    ax = axes[0]
    ax.plot(x, daily["equity_net"], color=blue, lw=1.8, label="After costs and funding")
    ax.plot(x, daily["equity_gross"], color=muted, lw=1.3, ls="--",
            label="Before costs and funding")
    ax.set_yscale("log")
    low, high = daily[["equity_net", "equity_gross"]].min().min(), \
        daily[["equity_net", "equity_gross"]].max().max()
    ax.yaxis.set_major_locator(ticker.LogLocator(subs=(1, 2, 5)) if high / low > 5
                               else ticker.MaxNLocator(6))
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"${v:g}"))
    ax.yaxis.set_minor_formatter(ticker.NullFormatter())
    ax.set_ylabel("Value of $1 (log scale)")
    for col, color in (("equity_net", blue), ("equity_gross", muted)):
        ax.annotate(f"${daily[col].iloc[-1]:.2f}", (x[-1], daily[col].iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center",
                    color=ink, fontsize=9)
    ax.axhline(1, color=axis, lw=0.8)
    ax.legend(frameon=False, loc="upper left", labelcolor=ink2)
    ax.set_title("A101_055 long-short backtest, Binance USDT perpetuals",
                 loc="left", color=ink, fontsize=13, pad=22)
    ax.text(0, 1.02, f"Top 16 long, bottom 16 short, rebalanced daily at 00:00 UTC. "
            f"Costs: {args.fee_bps:g} bps fee + {args.slippage_bps:g} bps slippage.",
            transform=ax.transAxes, color=ink2, fontsize=9)

    ax = axes[1]
    dd = daily["equity_net"] / daily["equity_net"].cummax() - 1
    ax.fill_between(x, dd * 100, 0, color=blue, alpha=0.15, lw=0)
    ax.plot(x, dd * 100, color=blue, lw=1.2)
    ax.set_ylabel("Drawdown after costs (%)")

    ax = axes[2]
    long_c = (SIDE_WEIGHT * daily["long_ret"]).cumsum() * 100
    short_c = (SIDE_WEIGHT * daily["short_ret"]).cumsum() * 100
    ax.plot(x, long_c, color=orange, lw=1.6, label="Long side")
    ax.plot(x, short_c, color=aqua, lw=1.6, label="Short side")
    ax.axhline(0, color=axis, lw=0.8)
    for series, color, name in ((long_c, orange, "Long"), (short_c, aqua, "Short")):
        ax.annotate(f"{name} {series.iloc[-1]:+.0f}%", (x[-1], series.iloc[-1]),
                    xytext=(6, 0), textcoords="offset points", va="center",
                    color=ink, fontsize=9)
    ax.set_ylabel("Price-move return by side,\nsum of daily % of equity")
    ax.legend(frameon=False, loc="upper left", labelcolor=ink2)

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return f"Saved {path}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--datadir", default="data")
    parser.add_argument("--outdir", default="backtest_out")
    parser.add_argument("--start", default="2000-01-01",
                        help="first signal date (default: as early as the data allows)")
    parser.add_argument("--end", default="2100-01-01", help="last signal date")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--check", metavar="CSV",
                        help="compare with a saved a101_055_<date>.csv")
    args = parser.parse_args(argv)
    datadir, outdir = Path(args.datadir), Path(args.outdir)

    panels, funding = load_data(datadir)
    cost_rate = (args.fee_bps + args.slippage_bps) / 1e4
    daily, positions, scores, ret, skipped = run(
        panels, funding, pd.Timestamp(args.start), pd.Timestamp(args.end), cost_rate)
    if daily.empty:
        print("ERROR: no day could be traded.", file=sys.stderr)
        return 2

    lines = report(daily, positions, scores, ret, skipped, panels, args)
    if args.check:
        lines += check_live(args.check, panels)

    outdir.mkdir(parents=True, exist_ok=True)
    daily.to_csv(outdir / "daily.csv", float_format="%.8g", date_format="%Y-%m-%d")
    positions.to_csv(outdir / "positions.csv", index=False, float_format="%.8g",
                     date_format="%Y-%m-%d")
    lines.append("")
    lines.append(chart(daily, outdir / "equity.png", args))
    lines.append(f"Saved {outdir / 'daily.csv'}, {outdir / 'positions.csv'}, "
                 f"{outdir / 'summary.txt'}")
    text = "\n".join(lines)
    (outdir / "summary.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
