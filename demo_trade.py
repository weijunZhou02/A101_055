#!/usr/bin/env python3
"""Trade A101_055 on a Binance Futures DEMO account (fake money).

Run once a day soon after 00:00 UTC. Each run:
  1. gets the lists for the day that just closed from LIVE market data (runs
     a101_055.py into demo/lists if that day's CSV is not there yet),
  2. reads the demo account: equity, positions, and the funding and fees
     Binance charged since the last run,
  3. rebalances on the demo exchange: BUY 16 and SHORT 16, half the equity per
     side, equal money per coin, sized at the live price. Orders are LIMIT IOC
     capped at --max-slippage-bps from the live mid, so the thinner demo book
     cannot fill far from the real price; whatever does not fill inside the cap
     is reported. Reductions go first and use reduceOnly,
  4. compares the day with the backtest model (00:00-to-00:00 candle returns).

The demo exchange has its own order book: its prices and spreads differ from
the live market, so demo fills are worse than real ones would be. Each order
logs the live mid next to the demo fill to show that gap.

Only https://demo-fapi.binance.com is ever called with keys; there is no way to
point this script at a real account. Demo API keys (BINANCE_DEMO_API_KEY,
BINANCE_DEMO_API_SECRET) are read from the environment or from the .env file
next to this script; --set-keys types them into .env without showing them.

Files in demo/: state.json, ledger.csv (one row per run), orders.csv (one row
per order), lists/ (daily a101_055 CSVs).
"""

import argparse
import csv
import hashlib
import hmac
import json
import math
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import a101_055 as live
from a101_055 import ApiError, BinancePublic

DEMO_URL = "https://demo-fapi.binance.com"      # fixed on purpose: demo only
ENV_FILE = Path(__file__).resolve().with_name(".env")
KEY_NAMES = ("BINANCE_DEMO_API_KEY", "BINANCE_DEMO_API_SECRET")
USER_AGENT = "a101-055-demo/1.0 (demo trading)"
RECV_WINDOW = 5000
ORDER_PAUSE = 0.15

SIDE_WEIGHT = 0.5
LEVERAGE = 3      # per coin; exposure stays 1x equity, this only sets margin and
                  # position limits (high default leverage caps the position size)
DEFAULT_MAX_SLIPPAGE_BPS = 100.0
LATE_HOURS = 2.0

LEDGER_COLUMNS = [
    "run_time_utc", "signal_date", "entry_delay_h", "equity_before", "equity_change",
    "funding", "commission", "price_pnl", "demo_gross_ret", "model_gross_ret",
    "tracking_diff", "prev_entry_delay_h", "orders", "filled_notional",
    "unfilled_orders", "skipped_small", "left_out", "slip_vs_live_bps",
    "equity_after", "n_long", "n_short", "total_return",
]
ORDER_COLUMNS = [
    "run_time_utc", "signal_date", "symbol", "side", "reduce_only", "qty",
    "executed_qty", "limit_price", "avg_price", "live_mid", "slip_vs_live_bps",
    "fee", "order_id", "status", "error",
]


class DemoClient:
    """Signed client for the Binance Futures demo exchange."""

    def __init__(self, api_key, api_secret):
        self.api_key, self.api_secret = api_key, api_secret.encode()
        self.offset = 0
        server = self.request("GET", "/fapi/v1/time", signed=False)["serverTime"]
        self.offset = int(server) - int(time.time() * 1000)

    def request(self, method, path, params=None, signed=True):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = RECV_WINDOW
        query = urllib.parse.urlencode(params)
        if signed:
            query += "&signature=" + sign(self.api_secret, query)
        url = DEMO_URL + path + ("?" + query if query else "")
        headers = {"User-Agent": USER_AGENT}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        retries = 3 if method == "GET" else 1        # never resend an order
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as err:
                body = err.read()[:300].decode("utf-8", "replace")
                if err.code >= 500 and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(f"HTTP {err.code} on {path}: {body}") from err
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(f"network error on {path}: {err}") from err


def sign(secret, query):
    return hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and not name.strip().startswith("#"):
                values[name.strip()] = value.strip().strip("'\"")
    return values


def load_keys():
    if all(os.environ.get(n) for n in KEY_NAMES):
        return tuple(os.environ[n] for n in KEY_NAMES)
    if ENV_FILE.exists() and ENV_FILE.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"WARNING: {ENV_FILE} can be read by other users; run: chmod 600 \"{ENV_FILE}\"")
    env = read_env(ENV_FILE)
    return tuple(env.get(n) or None for n in KEY_NAMES)


def save_keys(key, secret):
    """Set the demo key lines in .env, keeping every other line."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    new = {"BINANCE_DEMO_API_KEY": key, "BINANCE_DEMO_API_SECRET": secret}
    out = []
    for line in lines:
        name = line.partition("=")[0].strip()
        out.append(f"{name}={new.pop(name)}" if name in new else line)
    out += [f"{n}={v}" for n, v in new.items()]
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    ENV_FILE.chmod(0o600)


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


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
    return ([r["symbol"] for r in rows if r["side"] == "BUY"],
            [r["symbol"] for r in rows if r["side"] == "SHORT"])


def demo_rules(info):
    rules = {}
    for s in info["symbols"]:
        f = {x["filterType"]: x for x in s["filters"]}
        rules[s["symbol"]] = {
            "status": s["status"],
            "step": float(f["LOT_SIZE"]["stepSize"]),
            "min_qty": float(f["LOT_SIZE"]["minQty"]),
            "tick": float(f["PRICE_FILTER"]["tickSize"]),
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
        }
    return rules


def decimals(step):
    return max(0, -math.floor(math.log10(step) + 1e-9))


def round_step(value, step, mode="down"):
    """Round a positive value to a multiple of step (down, up)."""
    n = value / step
    n = math.floor(n + 1e-9) if mode == "down" else math.ceil(n - 1e-9)
    return round(n * step, decimals(step))


def fmt(value, step):
    return f"{value:.{decimals(step)}f}"


def account(client):
    try:
        acct = client.request("GET", "/fapi/v3/account")
        risk = client.request("GET", "/fapi/v3/positionRisk")
    except ApiError as err:
        if "HTTP 404" not in str(err):
            raise
        acct = client.request("GET", "/fapi/v2/account")
        risk = client.request("GET", "/fapi/v2/positionRisk")
    positions = {p["symbol"]: float(p["positionAmt"]) for p in risk
                 if float(p["positionAmt"]) != 0}
    return float(acct["totalMarginBalance"]), positions


def income_since(client, start_ms, end_ms):
    totals = {}
    start = start_ms
    while True:
        rows = client.request("GET", "/fapi/v1/income",
                              {"startTime": start, "endTime": end_ms, "limit": 1000})
        for r in rows:
            totals[r["incomeType"]] = totals.get(r["incomeType"], 0.0) + float(r["income"])
        if len(rows) < 1000:
            return totals
        start = int(rows[-1]["time"]) + 1


def order_fills(client, symbol, order_id):
    """(quantity, quote amount, fee) of an order, from the trade history."""
    for attempt in range(3):
        trades = client.request("GET", "/fapi/v1/userTrades",
                                {"symbol": symbol, "orderId": order_id})
        if trades:
            return (sum(float(t["qty"]) for t in trades),
                    sum(float(t["quoteQty"]) for t in trades),
                    sum(float(t["commission"]) for t in trades))
        time.sleep(0.5 * (attempt + 1))
    return 0.0, 0.0, 0.0


def model_return(public, targets, day):
    start_ms = int(day.replace(tzinfo=timezone.utc).timestamp() * 1000)
    total = 0.0
    for s, w in targets.items():
        k = public.get("/fapi/v1/klines", {"symbol": s, "interval": "1d",
                                          "startTime": start_ms, "limit": 1})
        if not k or int(k[0][0]) != start_ms:
            return None
        total += w * (float(k[0][4]) / float(k[0][1]) - 1)
    return total


def plan_orders(targets, positions, equity, live_mids, rules, cap):
    """Orders that move positions to targets; reductions first."""
    orders, skipped, left_out = [], 0, []
    for s in sorted(set(targets) | set(positions)):
        w = targets.get(s, 0.0)
        current = positions.get(s, 0.0)
        rule = rules.get(s)
        mid = live_mids.get(s)
        if rule is None or rule["status"] != "TRADING" or mid is None:
            if w:
                left_out.append(s)
            if current:
                print(f"WARNING: {s} is held on demo but cannot be traded now")
            continue
        target = (math.copysign(round_step(abs(w) * equity / mid, rule["step"]), w)
                  if w else 0.0)
        if abs(target) < rule["min_qty"]:
            target = 0.0
        dq = round(target - current, decimals(rule["step"]))
        if dq == 0:
            continue
        reduce_only = target == 0 or (target * current > 0 and abs(target) < abs(current))
        if target != 0 and abs(dq) * mid < rule["min_notional"]:
            skipped += 1
            continue
        side = "BUY" if dq > 0 else "SELL"
        limit = (round_step(mid * (1 + cap), rule["tick"], "down") if side == "BUY"
                 else round_step(mid * (1 - cap), rule["tick"], "up"))
        orders.append({"symbol": s, "side": side, "qty": abs(dq), "reduce_only": reduce_only,
                       "opening": current == 0, "limit": limit, "mid": mid, "rule": rule})
    orders.sort(key=lambda o: not o["reduce_only"])
    return orders, skipped, left_out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="demo", help="folder for the demo ledger")
    parser.add_argument("--max-slippage-bps", type=float, default=DEFAULT_MAX_SLIPPAGE_BPS,
                        help="limit price distance from the live mid (default 100)")
    parser.add_argument("--status", action="store_true", help="show the demo account; no trading")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the orders for a $10,000 empty account; no keys needed")
    parser.add_argument("--close-existing", action="store_true",
                        help="first run only: allow closing positions already on the account")
    parser.add_argument("--close-all", action="store_true",
                        help="close every demo position (stop the strategy)")
    parser.add_argument("--again", action="store_true",
                        help="rebalance again to today's lists (e.g. after a failed order)")
    parser.add_argument("--set-keys", action="store_true",
                        help="type your demo API key and secret (hidden) into .env")
    args = parser.parse_args(argv)

    if args.set_keys:
        import getpass
        key = getpass.getpass("Demo API key (hidden, paste and press Enter): ").strip()
        secret = getpass.getpass("Demo API secret (hidden, paste and press Enter): ").strip()
        if not key or not secret:
            print("ERROR: both are needed; nothing saved.", file=sys.stderr)
            return 1
        save_keys(key, secret)
        print(f"Saved to {ENV_FILE} (readable only by you).")
        return 0
    folder = Path(args.dir)
    lists_dir, state_path = folder / "lists", folder / "state.json"
    ledger_path, orders_path = folder / "ledger.csv", folder / "orders.csv"
    cap = args.max_slippage_bps / 1e4

    public = BinancePublic()
    now_ms = int(public.get("/fapi/v1/time")["serverTime"])
    now = utc(now_ms)
    signal_day = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                   microsecond=0, tzinfo=None)
    signal = f"{signal_day:%Y-%m-%d}"
    entry_delay_h = (now_ms % 86_400_000) / 3_600_000
    print(f"A101_055 on the Binance Futures DEMO exchange at {now:%Y-%m-%d %H:%M} UTC "
          "(fake money).")

    client = None
    if not args.dry_run:
        key, secret = load_keys()
        if not key or not secret:
            print(f"ERROR: no demo API keys in {ENV_FILE}. Run: python3 demo_trade.py "
                  "--set-keys", file=sys.stderr)
            return 1
        try:
            client = DemoClient(key, secret)
            if client.request("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"]:
                print("ERROR: the demo account is in Hedge mode; switch it to One-way "
                      "mode in the futures settings.", file=sys.stderr)
                return 1
            equity, positions = account(client)
        except ApiError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
    else:
        equity, positions = 10_000.0, {}

    state = json.loads(state_path.read_text()) if state_path.exists() else None

    if args.status:
        print(f"Demo equity ${equity:,.2f}, {len(positions)} positions "
              f"({sum(v > 0 for v in positions.values())} long, "
              f"{sum(v < 0 for v in positions.values())} short)")
        if state:
            print(f"  {equity / state['start_equity'] - 1:+.2%} since {state['created']}; "
                  f"last rebalance to the {state['last_signal']} lists")
        return 0

    live_book = {b["symbol"]: b for b in public.get("/fapi/v1/ticker/bookTicker")}
    live_mids = {s: (float(b["bidPrice"]) + float(b["askPrice"])) / 2
                 for s, b in live_book.items()}
    request = urllib.request.Request(DEMO_URL + "/fapi/v1/exchangeInfo",
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as resp:
        rules = demo_rules(json.loads(resp.read()))

    if args.close_all:
        targets = {}
    else:
        if state and state["last_signal"] == signal and not args.again:
            print(f"Already rebalanced to the {signal} lists. Demo equity ${equity:,.2f}. "
                  "Next run after 00:00 UTC.")
            return 0
        if state is None and positions and not args.close_existing and not args.dry_run:
            print(f"ERROR: the demo account already has {len(positions)} positions "
                  f"({', '.join(sorted(positions))}). Rerun with --close-existing to let "
                  "the strategy close or resize them.", file=sys.stderr)
            return 1
        lists_path = lists_dir / f"a101_055_{signal}.csv"
        if not lists_path.exists():
            code = live.main(["--outdir", str(lists_dir)])
            if code != 0 or not lists_path.exists():
                print(f"ERROR: no lists for {signal}; nothing traded.", file=sys.stderr)
                return code or 2
            print()
        buy, short = read_lists(lists_path)
        targets = {s: SIDE_WEIGHT / len(buy) for s in buy}
        targets.update({s: -SIDE_WEIGHT / len(short) for s in short})

    # What happened since the last run.
    funding = commission = 0.0
    model = None
    if state and client and not args.close_all:
        income = income_since(client, state["last_run_ms"], now_ms)
        funding = income.get("FUNDING_FEE", 0.0)
        commission = income.get("COMMISSION", 0.0)
        if state["last_signal"] == f"{signal_day - timedelta(days=1):%Y-%m-%d}":
            model = model_return(public, state["targets"], signal_day)

    orders, skipped, left_out = plan_orders(targets, positions, equity, live_mids, rules, cap)
    if args.dry_run:
        print(f"Dry run for a ${equity:,.0f} empty account: {len(orders)} orders, "
              f"{skipped} skipped under the minimum, left out (not on demo): "
              f"{', '.join(left_out) or 'none'}")
        for o in orders:
            print(f"  {o['side']:<4} {fmt(o['qty'], o['rule']['step']):>14} {o['symbol']:<14} "
                  f"limit {fmt(o['limit'], o['rule']['tick'])} (live mid {o['mid']:.6g})"
                  f"{'  reduceOnly' if o['reduce_only'] else ''}")
        return 0

    run_time = f"{now:%Y-%m-%d %H:%M:%S}"
    filled_notional = slip_weighted = 0.0
    unfilled = 0
    for o in orders:
        params = {"symbol": o["symbol"], "side": o["side"], "type": "LIMIT",
                  "timeInForce": "IOC", "quantity": fmt(o["qty"], o["rule"]["step"]),
                  "price": fmt(o["limit"], o["rule"]["tick"]),
                  "newOrderRespType": "RESULT"}
        if o["reduce_only"]:
            params["reduceOnly"] = "true"
        row = {"run_time_utc": run_time, "signal_date": signal, "symbol": o["symbol"],
               "side": o["side"], "reduce_only": int(o["reduce_only"]), "qty": o["qty"],
               "executed_qty": 0.0, "limit_price": o["limit"], "avg_price": "",
               "live_mid": o["mid"], "slip_vs_live_bps": "", "fee": "", "order_id": "",
               "status": "", "error": ""}
        try:
            if o["opening"]:
                try:
                    client.request("POST", "/fapi/v1/leverage",
                                   {"symbol": o["symbol"], "leverage": LEVERAGE})
                except ApiError as err:
                    print(f"WARNING: could not set {LEVERAGE}x leverage on {o['symbol']}: {err}")
            r = client.request("POST", "/fapi/v1/order", params)
            row.update(order_id=r.get("orderId", ""), status=r.get("status", ""))
            # The order reply may not carry fill prices; the trade history does.
            executed, quote, fee = order_fills(client, o["symbol"], r["orderId"])
            row.update(executed_qty=executed, fee=fee)
            if executed > 0:
                avg = quote / executed
                slip = (avg / o["mid"] - 1) * 1e4 * (1 if o["side"] == "BUY" else -1)
                row.update(avg_price=avg, slip_vs_live_bps=slip)
                filled_notional += quote
                slip_weighted += slip * quote
            if executed < o["qty"] - 1e-12:
                unfilled += 1
        except ApiError as err:
            row.update(status="ERROR", error=str(err)[:200])
            unfilled += 1
        append_row(orders_path, ORDER_COLUMNS, row)
        time.sleep(ORDER_PAUSE)

    equity_after, positions_after = account(client)
    if state is None:
        state = {"created": f"{now:%Y-%m-%d %H:%M} UTC", "start_equity": equity,
                 "last_equity_before": None, "last_entry_delay_h": None}
    change = equity - state["last_equity_before"] if state["last_equity_before"] else None
    prev = state["last_equity_before"]
    price_pnl = change - funding - commission if change is not None else None
    demo_gross = price_pnl / prev if price_pnl is not None else None
    slip_avg = slip_weighted / filled_notional if filled_notional else 0.0
    if not args.close_all:
        append_row(ledger_path, LEDGER_COLUMNS, {
            "run_time_utc": run_time, "signal_date": signal, "entry_delay_h": entry_delay_h,
            "equity_before": equity, "equity_change": "" if change is None else change,
            "funding": funding, "commission": commission,
            "price_pnl": "" if price_pnl is None else price_pnl,
            "demo_gross_ret": "" if demo_gross is None else demo_gross,
            "model_gross_ret": "" if model is None else model,
            "tracking_diff": "" if model is None or demo_gross is None else demo_gross - model,
            "prev_entry_delay_h": "" if state["last_entry_delay_h"] is None
                                  else state["last_entry_delay_h"],
            "orders": len(orders), "filled_notional": filled_notional,
            "unfilled_orders": unfilled, "skipped_small": skipped,
            "left_out": " ".join(left_out), "slip_vs_live_bps": slip_avg,
            "equity_after": equity_after,
            "n_long": sum(v > 0 for v in positions_after.values()),
            "n_short": sum(v < 0 for v in positions_after.values()),
            "total_return": equity_after / state["start_equity"] - 1,
        })
        state.update({"last_signal": signal, "last_run_ms": now_ms, "targets": targets,
                      "last_equity_before": equity, "last_entry_delay_h": entry_delay_h})
        folder.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    # Report.
    if args.close_all:
        print(f"Closed positions: {len(orders)} orders, {unfilled} not fully filled. "
              f"Demo equity ${equity_after:,.2f}, {len(positions_after)} positions left.")
        return 0
    print(f"Lists: {signal}; entry {entry_delay_h:.1f} h after 00:00 UTC"
          + (" (late: left out of the model comparison)" if entry_delay_h > LATE_HOURS else ""))
    if change is not None:
        print(f"Since the last run: equity {change:+,.2f} (price {price_pnl:+,.2f}, "
              f"funding {funding:+,.2f}, fees {commission:+,.2f})")
        if model is not None:
            print(f"  demo {demo_gross:+.2%} before funding and fees, backtest model "
                  f"{model:+.2%} (difference {(demo_gross - model) * 1e4:+.1f} bps)")
    print(f"Orders: {len(orders)} sent, {unfilled} not fully filled inside the "
          f"{args.max_slippage_bps:g} bps cap, {skipped} skipped under the minimum; "
          f"filled ${filled_notional:,.2f}, demo fills {slip_avg:+.1f} bps vs the live mid")
    if left_out:
        print(f"  left out (not tradable on demo): {', '.join(left_out)}")
    print(f"Demo equity: ${equity_after:,.2f} ({len(positions_after)} positions; "
          f"{equity_after / state['start_equity'] - 1:+.2%} since {state['created']})")
    if unfilled:
        print(f"  See {orders_path} for the orders that did not fill completely.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
