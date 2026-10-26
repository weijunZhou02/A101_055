#!/usr/bin/env python3
"""Paper-trade A101_055 on Binance USDT-margined perpetuals with live prices.

Run once a day soon after 00:00 UTC (11:00 in Sydney during daylight saving,
10:00 otherwise). Each run:
  1. gets the lists for the day that just closed (runs a101_055.py into
     paper/lists if that day's CSV is not there yet),
  2. settles the positions held since the last run at the current mid prices
     and books the funding actually charged in between,
  3. rebalances to the new lists like the backtest: BUY 16 and SHORT 16, half
     the equity per side, equal money per coin. Each order walks the live
     order book for its size and pays the fee. Quantities are rounded to
     Binance's market step size, and trades under the minimum order size are
     skipped (closing a position is always allowed),
  4. compares the day with the backtest model (00:00-to-00:00 candle returns
     of the same positions) to show how well the backtest holds up live.

Nothing is sent to Binance: public market data only, no orders, no API keys.
Files in paper/: state.json (balance and positions), ledger.csv (one row per
run), trades.csv (one row per simulated fill), lists/ (daily a101_055 CSVs).
"""

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import a101_055 as live
from a101_055 import BinancePublic, ApiError, log

SIDE_WEIGHT = 0.5                # half the equity long, half short
DEFAULT_CAPITAL = 10_000.0
DEFAULT_FEE_BPS = 5.0            # taker fee, regular account
DEPTH_LEVELS = 50                # /fapi/v1/depth weight 2 up to 50 levels
FUNDING_PAUSE = 0.25             # /fapi/v1/fundingRate: 500 requests / 5 min
LATE_HOURS = 2.0                 # entries later than this are flagged
ASSUMED_SLIPPAGE_BPS = 2.0       # what the backtest assumes, for comparison

LEDGER_COLUMNS = [
    "run_time_utc", "signal_date", "entry_delay_h", "hours_since_last",
    "equity_start", "long_pnl", "short_pnl", "funding", "equity_before_trades",
    "paper_gross_ret", "model_gross_ret", "tracking_diff", "prev_entry_delay_h",
    "traded_notional", "turnover", "fees", "slippage", "slippage_bps",
    "skipped_small", "thin_books", "equity_after", "n_long", "n_short",
    "total_return",
]
TRADE_COLUMNS = [
    "run_time_utc", "signal_date", "symbol", "side", "action", "qty", "mid",
    "fill", "notional", "fee", "slippage", "slippage_bps", "levels", "thin_book",
]


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def load_state(path):
    return json.loads(path.read_text()) if path.exists() else None


def save_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(path)


def append_row(path, columns, row):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if new:
            writer.writeheader()
        writer.writerow({k: (f"{v:.10g}" if isinstance(v, float) else v)
                         for k, v in row.items()})


def read_lists(path):
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    buy = [r["symbol"] for r in rows if r["side"] == "BUY"]
    short = [r["symbol"] for r in rows if r["side"] == "SHORT"]
    return buy, short


def order_rules(info):
    rules = {}
    for s in info["symbols"]:
        f = {x["filterType"]: x for x in s["filters"]}
        lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
        rules[s["symbol"]] = {
            "status": s["status"],
            "step": float(lot["stepSize"]),
            "min_qty": float(lot["minQty"]),
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
        }
    return rules


def round_qty(qty, step):
    """Round toward zero to the market step size."""
    decimals = max(0, -math.floor(math.log10(step)))
    n = math.floor(abs(qty) / step + 1e-9)
    return math.copysign(round(n * step, decimals), qty) if n else 0.0


def walk_book(client, symbol, qty, side):
    """Average fill price of a market order of `qty`, from the live book."""
    book = client.get("/fapi/v1/depth", {"symbol": symbol, "limit": DEPTH_LEVELS})
    levels = book["asks"] if side == "BUY" else book["bids"]
    left, cost, used = qty, 0.0, 0
    for price, size in levels:
        take = min(left, float(size))
        cost += take * float(price)
        left -= take
        used += 1
        if left <= 1e-12:
            break
    thin = left > 1e-12
    if thin:                      # deeper than the levels fetched
        cost += left * float(levels[-1][0])
    return cost / qty, used, thin


def funding_since(client, symbols, start_ms, end_ms):
    """Funding events per symbol in (start_ms, end_ms]: [(rate, mark price)]."""
    out = {}
    for s in symbols:
        rows = client.get("/fapi/v1/fundingRate", {
            "symbol": s, "startTime": start_ms + 1, "endTime": end_ms, "limit": 1000})
        out[s] = [(float(r["fundingRate"]), float(r["markPrice"]))
                  for r in rows if r.get("markPrice") not in (None, "")]
    return out


def model_return(client, targets, day):
    """Backtest-model return of `targets` over the 00:00-to-00:00 UTC `day`."""
    start_ms = int(day.replace(tzinfo=timezone.utc).timestamp() * 1000)
    total = 0.0
    for s, w in targets.items():
        k = client.get("/fapi/v1/klines", {"symbol": s, "interval": "1d",
                                          "startTime": start_ms, "limit": 1})
        if not k or int(k[0][0]) != start_ms:
            return None
        total += w * (float(k[0][4]) / float(k[0][1]) - 1)
    return total


def mark_to_market(state, mids):
    return state["balance"] + sum(
        p["qty"] * (mids.get(s, p["ref"]) - p["ref"])
        for s, p in state["positions"].items())


def print_summary(ledger_path, capital):
    if not ledger_path.exists():
        return
    with ledger_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    num = lambda r, k: float(r[k]) if r[k] not in ("", None) else None
    equity = num(rows[-1], "equity_after")
    fees = sum(num(r, "fees") for r in rows)
    slip = sum(num(r, "slippage") for r in rows)
    traded = sum(num(r, "traded_notional") for r in rows)
    funding = sum(num(r, "funding") for r in rows)
    print(f"\nSince the start ({rows[0]['run_time_utc'][:10]}, {len(rows)} runs): "
          f"equity ${equity:,.2f} ({equity / capital - 1:+.2%})")
    print(f"  fees ${fees:,.2f}, slippage ${slip:,.2f} "
          f"({slip / traded * 1e4 if traded else 0:.2f} bps of traded notional; "
          f"the backtest assumes {ASSUMED_SLIPPAGE_BPS:g}), funding ${funding:+,.2f}")
    pairs = [(num(r, "paper_gross_ret"), num(r, "model_gross_ret")) for r in rows
             if num(r, "model_gross_ret") is not None
             and (num(r, "prev_entry_delay_h") or 0) <= LATE_HOURS]
    if pairs:
        diffs = [p - m for p, m in pairs]
        mean = sum(diffs) / len(diffs)
        spread = ""
        if len(diffs) > 1:
            sd = (sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)) ** 0.5
            spread = f" (spread {sd * 1e4:.1f} bps)"
        print(f"  paper vs backtest model, before costs ({len(pairs)} comparable days): "
              f"paper {sum(p for p, _ in pairs):+.2%}, model {sum(m for _, m in pairs):+.2%}, "
              f"daily difference {mean * 1e4:+.1f} bps on average{spread}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="paper", help="paper account folder")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL,
                        help="starting capital in USDT (first run only)")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS,
                        help="fee per trade (first run only)")
    parser.add_argument("--status", action="store_true",
                        help="show the account at current prices; no trading")
    args = parser.parse_args(argv)
    folder = Path(args.dir)
    lists_dir = folder / "lists"
    state_path = folder / "state.json"
    ledger_path, trades_path = folder / "ledger.csv", folder / "trades.csv"

    client = BinancePublic()
    try:
        now_ms = int(client.get("/fapi/v1/time")["serverTime"])
    except ApiError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    now = utc(now_ms)
    signal_day = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                   microsecond=0, tzinfo=None)
    signal = f"{signal_day:%Y-%m-%d}"
    entry_delay_h = (now_ms % 86_400_000) / 3_600_000

    state = load_state(state_path)
    if state is None:
        state = {"created": f"{now:%Y-%m-%d %H:%M} UTC", "capital": args.capital,
                 "fee_bps": args.fee_bps, "balance": args.capital, "positions": {},
                 "targets": {}, "equity_after": args.capital, "last_run_ms": None,
                 "last_signal": None, "last_entry_delay_h": None}

    print(f"A101_055 paper trading ({folder}/) at {now:%Y-%m-%d %H:%M} UTC. "
          "Simulation only: no orders are sent.")

    if args.status or state["last_signal"] == signal:
        if state["last_signal"] == signal and not args.status:
            print(f"Already rebalanced to the {signal} lists. Next run after 00:00 UTC.")
        book = {b["symbol"]: b for b in client.get("/fapi/v1/ticker/bookTicker")}
        mids = {s: (float(b["bidPrice"]) + float(b["askPrice"])) / 2
                for s, b in book.items()}
        equity = mark_to_market(state, mids)
        print(f"Equity now ${equity:,.2f} ({equity / state['capital'] - 1:+.2%} since "
              f"{state['created']}), {len(state['positions'])} positions, "
              f"{equity / state['equity_after'] - 1:+.2%} since the last rebalance "
              "(before funding)")
        print_summary(ledger_path, state["capital"])
        return 0

    # 1. Lists for the day that just closed.
    lists_path = lists_dir / f"a101_055_{signal}.csv"
    if not lists_path.exists():
        code = live.main(["--outdir", str(lists_dir)])
        if code != 0 or not lists_path.exists():
            print(f"ERROR: no lists for {signal}; nothing traded. Positions are "
                  "unchanged; try again later.", file=sys.stderr)
            return code or 2
        print()
    buy, short = read_lists(lists_path)

    # 2. Settle the positions held since the last run.
    rules = order_rules(client.get("/fapi/v1/exchangeInfo"))
    book = {b["symbol"]: b for b in client.get("/fapi/v1/ticker/bookTicker")}
    mids = {s: (float(b["bidPrice"]) + float(b["askPrice"])) / 2
            for s, b in book.items()}
    marks = {p["symbol"]: float(p["markPrice"])
             for p in client.get("/fapi/v1/premiumIndex")}
    positions = state["positions"]
    warnings = []
    long_pnl = short_pnl = funding = 0.0
    for s, p in positions.items():
        price = mids.get(s) or marks.get(s)
        if price is None:
            warnings.append(f"{s}: no current price; valued at the last price")
            price = p["ref"]
        pnl = p["qty"] * (price - p["ref"])
        if p["qty"] > 0:
            long_pnl += pnl
        else:
            short_pnl += pnl
        p["ref"] = price
    if positions and state["last_run_ms"]:
        events = funding_since(BinancePublic(pause=FUNDING_PAUSE), list(positions),
                               state["last_run_ms"], now_ms)
        funding = sum(-positions[s]["qty"] * mark * rate
                      for s, rows in events.items() for rate, mark in rows)
    state["balance"] += long_pnl + short_pnl + funding
    equity_start = state["equity_after"]
    equity = state["balance"]
    paper_gross = (long_pnl + short_pnl) / equity_start if positions else None

    model = None
    consecutive = (state["last_signal"] is not None and state["targets"]
                   and state["last_signal"] == f"{signal_day - timedelta(days=1):%Y-%m-%d}")
    if consecutive:
        model = model_return(client, state["targets"], signal_day)

    # 3. Rebalance to the new lists.
    fee_rate = state["fee_bps"] / 1e4
    targets = {s: SIDE_WEIGHT / len(buy) for s in buy}
    targets.update({s: -SIDE_WEIGHT / len(short) for s in short})
    run_time = f"{now:%Y-%m-%d %H:%M:%S}"
    traded = fees = slippage = 0.0
    skipped = thin_books = 0
    actions = {"open": [], "close": [], "resize": 0}
    for s in sorted(set(targets) | set(positions)):
        w = targets.get(s, 0.0)
        current = positions.get(s, {}).get("qty", 0.0)
        rule = rules.get(s)
        if rule is None or rule["status"] != "TRADING" or s not in mids:
            if current:
                price = marks.get(s, positions[s]["ref"])
                warnings.append(f"{s}: no longer trading; closed at mark price {price:g}")
                state["balance"] += current * (price - positions[s]["ref"])
                del positions[s]
            if w:
                warnings.append(f"{s}: on the list but not tradable; left out")
            continue
        mid = mids[s]
        target = round_qty(w * equity / mid, rule["step"]) if w else 0.0
        if abs(target) < rule["min_qty"]:
            target = 0.0
        dq = target - current
        if abs(dq) < 1e-12:
            continue
        if target != 0 and abs(dq) * mid < rule["min_notional"]:
            skipped += 1                  # too small to place; keep the drift
            continue
        side = "BUY" if dq > 0 else "SELL"
        fill, levels, thin = walk_book(client, s, abs(dq), side)
        notional = abs(dq) * fill
        fee = notional * fee_rate
        slip = abs(dq) * (fill - mid) * (1 if side == "BUY" else -1)
        state["balance"] -= fee + slip
        traded, fees, slippage = traded + notional, fees + fee, slippage + slip
        thin_books += thin
        action = ("open" if current == 0 else "close" if target == 0 else "resize")
        if action == "resize":
            actions["resize"] += 1
        else:
            actions[action].append(s)
        append_row(trades_path, TRADE_COLUMNS, {
            "run_time_utc": run_time, "signal_date": signal, "symbol": s, "side": side,
            "action": action, "qty": abs(dq), "mid": mid, "fill": fill,
            "notional": notional, "fee": fee, "slippage": slip,
            "slippage_bps": slip / notional * 1e4, "levels": levels,
            "thin_book": int(thin)})
        if target == 0:
            del positions[s]
        else:
            positions[s] = {"qty": target, "ref": mid}

    equity_after = state["balance"]
    append_row(ledger_path, LEDGER_COLUMNS, {
        "run_time_utc": run_time, "signal_date": signal,
        "entry_delay_h": entry_delay_h,
        "hours_since_last": ((now_ms - state["last_run_ms"]) / 3_600_000
                             if state["last_run_ms"] else ""),
        "equity_start": equity_start, "long_pnl": long_pnl, "short_pnl": short_pnl,
        "funding": funding, "equity_before_trades": equity,
        "paper_gross_ret": "" if paper_gross is None else paper_gross,
        "model_gross_ret": "" if model is None else model,
        "tracking_diff": "" if model is None or paper_gross is None
                         else paper_gross - model,
        "prev_entry_delay_h": "" if state["last_entry_delay_h"] is None
                              else state["last_entry_delay_h"],
        "traded_notional": traded, "turnover": traded / equity, "fees": fees,
        "slippage": slippage, "slippage_bps": slippage / traded * 1e4 if traded else 0.0,
        "skipped_small": skipped, "thin_books": thin_books, "equity_after": equity_after,
        "n_long": sum(p["qty"] > 0 for p in positions.values()),
        "n_short": sum(p["qty"] < 0 for p in positions.values()),
        "total_return": equity_after / state["capital"] - 1,
    })
    state.update({"targets": targets, "equity_after": equity_after,
                  "last_run_ms": now_ms, "last_signal": signal,
                  "last_entry_delay_h": entry_delay_h})
    save_state(state_path, state)

    # 4. Report.
    print(f"Lists: {signal} ({len(buy)} BUY, {len(short)} SHORT); entry "
          f"{entry_delay_h:.1f} h after 00:00 UTC")
    if entry_delay_h > LATE_HOURS:
        print(f"  NOTE: the backtest enters at 00:00 UTC; entries more than "
              f"{LATE_HOURS:g} h late are left out of the paper-vs-model comparison.")
    if paper_gross is not None:
        print(f"Since the last run: long ${long_pnl:+,.2f}, short ${short_pnl:+,.2f}, "
              f"funding ${funding:+,.2f}  ->  equity ${equity:,.2f}")
        line = f"  paper {paper_gross:+.2%} before costs"
        if model is not None:
            line += f", backtest model {model:+.2%} (difference {(paper_gross - model) * 1e4:+.1f} bps)"
        print(line)
    print(f"Rebalance: opened {len(actions['open'])}, closed {len(actions['close'])}, "
          f"resized {actions['resize']}, skipped {skipped} under the minimum order size")
    if actions["open"]:
        print(f"  opened: {', '.join(actions['open'])}")
    if actions["close"]:
        print(f"  closed: {', '.join(actions['close'])}")
    print(f"  traded ${traded:,.2f} ({traded / equity:.0%} of equity), fees ${fees:,.2f}, "
          f"slippage ${slippage:,.2f} ({slippage / traded * 1e4 if traded else 0:.2f} bps)"
          + (f", {thin_books} orders deeper than {DEPTH_LEVELS} book levels" if thin_books else ""))
    print(f"Equity after trading: ${equity_after:,.2f} "
          f"({equity_after / state['capital'] - 1:+.2%} since {state['created']})")
    for message in warnings:
        print(f"WARNING: {message}")
    print_summary(ledger_path, state["capital"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
