#!/usr/bin/env python3
"""Build demo/dashboard.html: the demo account's money line, long and short
profit, holdings and the daily 80-coin lists.

Reads the demo account with your demo API key (from .env, on this Mac only)
and the history in demo/, and writes one HTML page. The page holds no keys:
when opened, it only fetches public mark prices from demo-fapi.binance.com to
keep the holdings and equity current until the next rebuild.

demo_trade.py rebuilds it after every run. To rebuild it yourself:
    python3 dashboard.py          # then open demo/dashboard.html
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from a101_055 import ApiError, BinancePublic
from demo_trade import DEMO_URL, DemoClient, load_keys

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
LIST_DAYS = 90                    # daily lists kept in the page
HISTORY_COLUMNS = ["time_ms", "equity", "long_upnl", "short_upnl"]


def utc_ms(text, fmt):
    return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)


def account_snapshot(client):
    """Wallet balance, equity and open positions as Binance reports them."""
    try:
        acct = client.request("GET", "/fapi/v3/account")
        risk = client.request("GET", "/fapi/v3/positionRisk")
    except ApiError as err:
        if "HTTP 404" not in str(err):
            raise
        acct = client.request("GET", "/fapi/v2/account")
        risk = client.request("GET", "/fapi/v2/positionRisk")
    positions = [{"symbol": p["symbol"], "qty": float(p["positionAmt"]),
                  "entry": float(p["entryPrice"]), "mark": float(p["markPrice"])}
                 for p in risk if float(p["positionAmt"]) != 0]
    return (float(acct["totalWalletBalance"]), float(acct["totalMarginBalance"]),
            sorted(positions, key=lambda p: (p["qty"] < 0, p["symbol"])))


def unrealized_by_side(positions):
    long = sum(p["qty"] * (p["mark"] - p["entry"]) for p in positions if p["qty"] > 0)
    short = sum(p["qty"] * (p["mark"] - p["entry"]) for p in positions if p["qty"] < 0)
    return long, short


def realized_by_side(folder):
    """[(time_ms, long, short)] realized P&L of each fill: a SELL reduces a
    long position, a BUY reduces a short one (one-way mode)."""
    path = folder / "orders.csv"
    events = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pnl = float(row.get("realized_pnl") or 0)
                if pnl:
                    t = utc_ms(row["run_time_utc"], "%Y-%m-%d %H:%M:%S")
                    events.append((t, pnl, 0.0) if row["side"] == "SELL" else (t, 0.0, pnl))
    return sorted(events)


def read_history(folder, now_ms, equity, long_u, short_u):
    """Snapshots saved by earlier builds, plus this one (appended to the file)."""
    path = folder / "equity_history.csv"
    rows = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    rows.append({"time_ms": now_ms, "equity": f"{equity:.4f}",
                 "long_upnl": f"{long_u:.4f}", "short_upnl": f"{short_u:.4f}"})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in HISTORY_COLUMNS} for r in rows)
    return rows


def equity_points(folder, rows, state):
    """Equity at the start, before and after every trading run, and at every build."""
    points = {}
    if state:
        points[utc_ms(state["created"], "%Y-%m-%d %H:%M UTC")] = state["start_equity"]
    ledger = folder / "ledger.csv"
    if ledger.exists():
        with ledger.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ms = utc_ms(row["run_time_utc"], "%Y-%m-%d %H:%M:%S")
                points.setdefault(ms, float(row["equity_before"]))
                points[ms + 60_000] = float(row["equity_after"])
    for r in rows:
        points[int(r["time_ms"])] = float(r["equity"])
    return sorted([t, round(v, 2)] for t, v in points.items())


def side_points(rows, realized, start_ms):
    """Price profit of each side over time: realized so far + open profit."""
    points = [[start_ms, 0.0, 0.0]]
    for r in rows:
        if r.get("long_upnl") in (None, ""):
            continue                       # snapshots from before sides were saved
        t = int(r["time_ms"])
        rl = sum(e[1] for e in realized if e[0] <= t)
        rs = sum(e[2] for e in realized if e[0] <= t)
        points.append([t, round(rl + float(r["long_upnl"]), 2),
                       round(rs + float(r["short_upnl"]), 2)])
    return points


def btc_line(start_ms, now_ms, start_equity):
    """What the starting money would be worth held in BTC (live market)."""
    span = now_ms - start_ms
    interval = ("5m" if span <= 5 * DAY_MS else "15m" if span <= 15 * DAY_MS else
                "1h" if span <= 60 * DAY_MS else "4h" if span <= 240 * DAY_MS else "1d")
    rows = BinancePublic().get("/fapi/v1/klines", {
        "symbol": "BTCUSDT", "interval": interval, "startTime": start_ms, "limit": 1500})
    if not rows:
        return []
    first = float(rows[0][1])
    return [[int(r[6]) + 1 if int(r[6]) < now_ms else now_ms,
             round(start_equity * float(r[4]) / first, 2)] for r in rows]


def daily_lists(folder):
    """Each day's full ranking with the rank it had the day before."""
    files = sorted((folder / "lists").glob("a101_055_*_all.csv"))[-(LIST_DAYS + 1):]
    traded = set()
    ledger = folder / "ledger.csv"
    if ledger.exists():
        with ledger.open(encoding="utf-8") as f:
            traded = {row["signal_date"] for row in csv.DictReader(f)}
    days, prev_rank, prev_date = [], None, None
    for path in files:
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        date = rows[0]["date"] if rows else path.stem.split("_")[2]
        num = lambda v: round(float(v), 4) if v not in ("", None) else None
        ranks = {r["symbol"]: int(r["rank"]) for r in rows}
        days.append({
            "date": date, "prev": prev_date, "traded": date in traded,
            "rows": [[int(r["rank"]), r["symbol"], r["side"], num(r["a101_055"]),
                      num(r["pos_rank"]), num(r["vol_rank"]),
                      prev_rank.get(r["symbol"]) if prev_rank is not None else None]
                     for r in rows],
        })
        prev_rank, prev_date = ranks, date
    return days[-LIST_DAYS:][::-1]          # newest first


def build(client, folder):
    folder = Path(folder)
    state_path = folder / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    now_ms = int(BinancePublic().get("/fapi/v1/time")["serverTime"])
    wallet, equity, positions = account_snapshot(client)
    long_u, short_u = unrealized_by_side(positions)
    rows = read_history(folder, now_ms, equity, long_u, short_u)
    history = equity_points(folder, rows, state)
    start_equity = state["start_equity"] if state else history[0][1]
    start_ms = history[0][0]
    realized = realized_by_side(folder)
    data = {
        "built_ms": now_ms, "demo_url": DEMO_URL, "created_ms": start_ms,
        "last_signal": state.get("last_signal", "") if state else "",
        "start_equity": start_equity, "wallet": wallet, "equity": equity,
        "positions": positions, "history": history,
        "btc": [[start_ms, start_equity]] + btc_line(start_ms, now_ms, start_equity),
        "sides": side_points(rows, realized, start_ms),
        "realized": [round(sum(e[1] for e in realized), 4), round(sum(e[2] for e in realized), 4)],
        "lists": daily_lists(folder),
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    path = folder / "dashboard.html"
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(PAGE.replace("__DATA__", payload), encoding="utf-8")
    return path


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A101_055 Demo Portfolio</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --long: #1baf7a; --short: #4a3aa7;
  --up: #006300; --down: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --long: #199e70; --short: #9085e9;
    --up: #0ca30c; --down: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --long: #199e70; --short: #9085e9;
  --up: #0ca30c; --down: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 24px 16px 40px; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 24px;
  align-items: baseline; margin-bottom: 12px; }
h1 { font-size: 20px; margin: 0; font-weight: 600; }
.sub { color: var(--ink2); font-size: 13px; }
.status { color: var(--muted); font-size: 12px; }
.status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--muted); margin-right: 6px; }
.status.live .dot { background: var(--up); }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin-bottom: 16px; }
.tile, .card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; }
.tile { padding: 14px 16px; }
.tile .label { color: var(--ink2); font-size: 12px; }
.tile .value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile .note { font-size: 12px; color: var(--ink2); margin-top: 2px; font-variant-numeric: tabular-nums; }
.up { color: var(--up); } .down { color: var(--down); }
.card { padding: 16px; margin-bottom: 16px; scroll-margin-top: 12px; }
.card h2 { font-size: 15px; margin: 0 0 2px; font-weight: 600; }
.chart-box { position: relative; height: 340px; margin-top: 8px; }
.bar { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center;
  justify-content: space-between; }
button, select { font: inherit; font-weight: 600; color: var(--ink); background: var(--surface);
  border: 1px solid var(--axis); border-radius: 8px; padding: 7px 12px; cursor: pointer; }
button:hover, select:hover { border-color: var(--ink2); }
button[aria-expanded="true"], button[aria-pressed="true"] {
  background: var(--ink); color: var(--surface); border-color: var(--ink); }
.seg { display: inline-flex; }
.seg button { border-radius: 0; margin-left: -1px; }
.seg button:first-child { border-radius: 8px 0 0 8px; margin-left: 0; }
.seg button:last-child { border-radius: 0 8px 8px 0; }
.controls { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; margin-top: 12px; }
.table-wrap { overflow-x: auto; margin-top: 12px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 8px 10px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--grid); }
th { color: var(--ink2); font-weight: 600; font-size: 12px; }
#hold-table th { cursor: pointer; user-select: none; }
#hold-table th:nth-child(-n+2), #hold-table td:nth-child(-n+2),
#list-table th:nth-child(3), #list-table td:nth-child(3),
#list-table th:nth-child(4), #list-table td:nth-child(4) { text-align: left; }
tbody tr:hover { background: var(--grid); }
tfoot td { font-weight: 600; border-bottom: none; }
.tag { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--axis); color: var(--ink2); }
#list-table tr.in-list td { font-weight: 600; }
.summary { margin-top: 10px; font-size: 13px; color: var(--ink2); }
.summary b { color: var(--ink); font-weight: 600; }
.hidden { display: none; }
.mini { display: none; font-size: 11px; color: var(--muted); font-weight: 400; }
footer { color: var(--muted); font-size: 12px; margin-top: 8px; }
@media (max-width: 640px) {
  #hold-table th:nth-child(2), #hold-table td:nth-child(2),
  #hold-table th:nth-child(3), #hold-table td:nth-child(3),
  #hold-table th:nth-child(6), #hold-table td:nth-child(6),
  #hold-table th:nth-child(8), #hold-table td:nth-child(8),
  #list-table th:nth-child(6), #list-table td:nth-child(6),
  #list-table th:nth-child(7), #list-table td:nth-child(7) { display: none; }
  th, td { padding: 8px 5px; font-size: 13px; }
  .card { padding: 14px 10px; }
  .mini { display: block; }
}
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>A101_055 demo portfolio</h1>
      <div class="sub">Binance Futures demo account (fake money) · started <span id="created"></span></div>
    </div>
    <div class="status" id="status"><span class="dot"></span><span id="status-text"></span></div>
  </header>

  <nav aria-label="Jump to">
    <button data-jump="sides">Long vs short</button>
    <button data-jump="holdings">What I hold</button>
    <button data-jump="lists">Daily lists</button>
  </nav>

  <section class="tiles">
    <div class="tile"><div class="label">Equity</div><div class="value" id="t-equity"></div>
      <div class="note" id="t-equity-note"></div></div>
    <div class="tile"><div class="label">Since start</div><div class="value" id="t-total"></div>
      <div class="note" id="t-total-note"></div></div>
    <div class="tile"><div class="label">Open profit / loss</div><div class="value" id="t-upnl"></div>
      <div class="note">on current holdings</div></div>
    <div class="tile"><div class="label">Holdings</div><div class="value" id="t-count"></div>
      <div class="note" id="t-count-note"></div></div>
  </section>

  <section class="card" id="chart-card">
    <div class="bar">
      <div><h2 id="chart-title"></h2><div class="sub" id="chart-sub"></div></div>
      <div class="seg" role="group" aria-label="Chart">
        <button data-mode="account" aria-pressed="true">Account</button>
        <button data-mode="sides" aria-pressed="false">Long vs short</button>
      </div>
    </div>
    <div class="chart-box"><canvas id="chart" aria-label="Chart"></canvas></div>
  </section>

  <section class="card" id="hold-card">
    <div class="bar">
      <div><h2>What I hold</h2><div class="sub" id="hold-sub"></div></div>
      <button id="hold-toggle" aria-expanded="false" aria-controls="holdings"></button>
    </div>
    <div class="table-wrap hidden" id="holdings">
      <table id="hold-table">
        <thead><tr>
          <th data-key="symbol">Coin</th><th data-key="side">Side</th><th data-key="qty">Quantity</th>
          <th data-key="entry">My price</th><th data-key="mark">Price now</th>
          <th data-key="value">Value</th><th data-key="pnl">Profit / loss</th><th data-key="pct">%</th>
        </tr></thead>
        <tbody id="hold-rows"></tbody>
        <tfoot><tr><td>Total</td><td></td><td></td><td></td><td></td><td id="f-value"></td><td id="f-pnl"></td><td></td></tr></tfoot>
      </table>
    </div>
  </section>

  <section class="card" id="list-card">
    <div class="bar">
      <div><h2>Daily lists</h2><div class="sub">All 80 ranked coins of each run: top 16 BUY, bottom 16 SHORT, and the rank change since the day before</div></div>
      <button id="list-toggle" aria-expanded="false" aria-controls="lists">Show daily lists</button>
    </div>
    <div class="hidden" id="lists">
      <div class="controls">
        <label class="sub">Lists of <select id="list-date"></select></label>
        <div class="seg" role="group" aria-label="Filter">
          <button data-filter="all" aria-pressed="true">All 80</button>
          <button data-filter="BUY" aria-pressed="false">BUY 16</button>
          <button data-filter="SHORT" aria-pressed="false">SHORT 16</button>
        </div>
      </div>
      <div class="summary" id="list-summary"></div>
      <div class="table-wrap">
        <table id="list-table">
          <thead><tr><th>Rank</th><th>Change</th><th>Coin</th><th>List</th><th>Score</th>
            <th>Price position</th><th>Volume rank</th></tr></thead>
          <tbody id="list-rows"></tbody>
        </table>
      </div>
    </div>
  </section>

  <footer id="footer"></footer>
</main>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
const D = __DATA__;
const REFRESH_MS = 60000;
const money = (v, d = 2) => Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const fmtUsd = (v, d = 2) => (v < 0 ? "−$" : "$") + money(v, d);
const fmtSigned = (v, d = 2) => (v >= 0 ? "+$" : "−$") + money(v, d);
const fmtPct = v => (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(2) + "%";
const fmtPrice = v => v >= 100 ? v.toFixed(2) : v >= 1 ? v.toFixed(4) : v.toPrecision(4);
const fmtTime = ms => new Date(ms).toLocaleString(undefined,
  {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
const coin = s => s.replace(/USDT$/, "");
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = id => document.getElementById(id);

const positions = D.positions.map(p => ({...p}));
const live = [];                       // points added while the page is open
let liveMs = null, chartMode = "account", sortKey = "pnl", sortDir = -1;

function metrics() {
  let upnl = 0, value = 0, longU = 0, shortU = 0;
  for (const p of positions) {
    p.value = Math.abs(p.qty) * p.mark;
    p.pnl = p.qty * (p.mark - p.entry);
    p.pct = p.entry ? (p.mark / p.entry - 1) * Math.sign(p.qty) : 0;
    p.side = p.qty > 0 ? "Long" : "Short";
    upnl += p.pnl; value += p.value;
    if (p.qty > 0) longU += p.pnl; else shortU += p.pnl;
  }
  return {upnl, value, equity: D.wallet + upnl,
          long: D.realized[0] + longU, short: D.realized[1] + shortU};
}

function renderTiles(m) {
  const total = m.equity - D.start_equity;
  $("t-equity").textContent = fmtUsd(m.equity);
  $("t-equity-note").textContent = "started with " + fmtUsd(D.start_equity, 0);
  $("t-total").textContent = fmtPct(total / D.start_equity); $("t-total").className = "value " + cls(total);
  $("t-total-note").textContent = fmtSigned(total);
  $("t-upnl").textContent = fmtSigned(m.upnl); $("t-upnl").className = "value " + cls(m.upnl);
  const longs = positions.filter(p => p.qty > 0).length;
  $("t-count").textContent = positions.length + " coins";
  $("t-count-note").textContent = longs + " long · " + (positions.length - longs) + " short";
  $("hold-sub").textContent = "Positions on the demo account" + (D.last_signal ? " · lists of " + D.last_signal : "");
}

function renderHoldings(m) {
  const rows = [...positions].sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * sortDir;
  });
  $("hold-rows").innerHTML = rows.map(p => `<tr>
    <td>${coin(p.symbol)}<span class="mini">${p.side}</span></td>
    <td><span class="tag">${p.side}</span></td>
    <td>${Math.abs(p.qty).toLocaleString("en-US", {maximumFractionDigits: 6})}</td>
    <td>${fmtPrice(p.entry)}</td><td>${fmtPrice(p.mark)}</td>
    <td>${fmtUsd(p.value)}</td>
    <td class="${cls(p.pnl)}">${fmtSigned(p.pnl)}<span class="mini">${fmtPct(p.pct)}</span></td>
    <td class="${cls(p.pnl)}">${fmtPct(p.pct)}</td></tr>`).join("");
  $("f-value").textContent = fmtUsd(m.value);
  $("f-pnl").textContent = fmtSigned(m.upnl); $("f-pnl").className = cls(m.upnl);
  document.querySelectorAll("#hold-table th").forEach(th => {
    th.textContent = th.textContent.replace(/ [↓↑]$/, "") +
      (th.dataset.key === sortKey ? (sortDir < 0 ? " ↓" : " ↑") : "");
  });
}

// ----- chart -----
const STEPS = [1, 2, 3, 6, 12, 24, 48, 72, 168, 336, 720].map(h => h * 3600000);
let tickStep = 3600000;
function timeTicks(min, max) {
  tickStep = STEPS.find(s => (max - min) / s <= 6) || STEPS[STEPS.length - 1];
  const zone = new Date(min).getTimezoneOffset() * 60000;   // align to local midnight
  const ticks = [];
  for (let t = Math.ceil((min - zone) / tickStep) * tickStep + zone; t <= max; t += tickStep)
    ticks.push({value: t});
  return ticks;
}
function tickLabel(v) {
  const d = new Date(v);
  const date = d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
  if (tickStep >= 86400000 || (d.getHours() === 0 && d.getMinutes() === 0)) return date;
  return d.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
}

function datasets(m) {
  const line = (label, data, color, width, points) => ({label, data, borderColor: color,
    backgroundColor: color, borderWidth: width, pointRadius: points, pointHoverRadius: 5, tension: 0});
  if (chartMode === "account") {
    const eq = D.history.map(([x, y]) => ({x, y})).concat(live.map(p => ({x: p.x, y: p.equity})));
    return [line("My account", eq, css("--series-1"), 2, 3),
            line("Same money in BTC", D.btc.map(([x, y]) => ({x, y})), css("--series-2"), 1.5, 0)];
  }
  const longs = D.sides.map(([x, l]) => ({x, y: l})).concat(live.map(p => ({x: p.x, y: p.long})));
  const shorts = D.sides.map(([x, , s]) => ({x, y: s})).concat(live.map(p => ({x: p.x, y: p.short})));
  return [line("Long side " + fmtSigned(m.long), longs, css("--long"), 2, 3),
          line("Short side " + fmtSigned(m.short), shorts, css("--short"), 2, 3)];
}

let chart;
function renderChart(m) {
  const account = chartMode === "account";
  $("chart-title").textContent = account ? "Money line" : "Long vs short";
  $("chart-sub").textContent = account
    ? "Account value in USDT, compared with holding the same starting money in BTC"
    : "Price profit of each side since the start (realized + open), before fees and funding";
  if (!window.Chart) {
    document.querySelector(".chart-box").innerHTML =
      '<p class="sub">The chart library could not load (no internet?). The numbers above are still current.</p>';
    return;
  }
  if (chart && chart.$mode === chartMode) {
    const fresh = datasets(m);
    chart.data.datasets.forEach((d, i) => Object.assign(d, fresh[i]));
    chart.update("none"); return;
  }
  if (chart) chart.destroy();
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.color = css("--muted");
  chart = new Chart($("chart"), {
    type: "line",
    data: {datasets: datasets(m)},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: {mode: "nearest", axis: "x", intersect: false},
      scales: {
        x: {type: "linear", grid: {display: false}, border: {color: css("--axis")},
            afterBuildTicks: axis => { axis.ticks = timeTicks(axis.min, axis.max); },
            ticks: {callback: v => tickLabel(v)}},
        y: {border: {display: false},
            grid: {color: c => !account && c.tick.value === 0 ? css("--axis") : css("--grid")},
            ticks: {callback: v => (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US")}},
      },
      plugins: {
        legend: {position: "top", align: "start", labels: {color: css("--ink2"), boxWidth: 14, boxHeight: 2}},
        tooltip: {callbacks: {
          title: items => fmtTime(items[0].parsed.x),
          label: item => " " + item.dataset.label.replace(/ [+−]\$.*$/, "") + ": " +
            (account ? fmtUsd(item.parsed.y) : fmtSigned(item.parsed.y))}},
      },
    },
  });
  chart.$mode = chartMode;
}

function setMode(mode) {
  chartMode = mode;
  document.querySelectorAll("[data-mode]").forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === mode));
  render();
}

// ----- daily lists -----
let listFilter = "all";
function renderLists() {
  const day = D.lists.find(d => d.date === $("list-date").value);
  if (!day) { $("list-summary").textContent = "No daily lists saved yet."; $("list-rows").innerHTML = ""; return; }
  const prevDay = D.lists.find(d => d.date === day.prev);
  const lines = [];
  if (prevDay) {
    for (const side of ["BUY", "SHORT"]) {
      const now = new Set(day.rows.filter(r => r[2] === side).map(r => r[1]));
      const before = new Set(prevDay.rows.filter(r => r[2] === side).map(r => r[1]));
      const entered = [...now].filter(s => !before.has(s)).map(coin);
      const left = [...before].filter(s => !now.has(s)).map(coin);
      lines.push(`<b>${side}</b> entered: ${entered.join(", ") || "none"} · left: ${left.join(", ") || "none"}`);
    }
  } else {
    lines.push("No earlier list to compare with.");
  }
  lines.push(day.traded ? "The demo account traded these lists." : "Reference only: the demo account did not trade these lists.");
  $("list-summary").innerHTML = lines.join("<br>");
  const rows = day.rows.filter(r => listFilter === "all" || r[2] === listFilter);
  $("list-rows").innerHTML = rows.map(([rank, sym, side, score, pos, vol, prev]) => {
    let change = "–";
    if (prevDay) change = prev == null ? "new" : prev === rank ? "–" :
      (prev > rank ? "▲" : "▼") + Math.abs(prev - rank);
    return `<tr class="${side ? "in-list" : ""}"><td>${rank}</td><td>${change}</td>
      <td>${coin(sym)}</td><td>${side ? `<span class="tag">${side}</span>` : ""}</td>
      <td>${score == null ? "no score" : (score >= 0 ? "+" : "−") + Math.abs(score).toFixed(4)}</td>
      <td>${pos == null ? "–" : pos.toFixed(3)}</td><td>${vol == null ? "–" : vol.toFixed(3)}</td></tr>`;
  }).join("");
}

// ----- live prices -----
function render() {
  const m = metrics();
  renderTiles(m); renderHoldings(m); renderChart(m);
}
function setStatus(ok) {
  $("status").className = "status" + (ok ? " live" : "");
  $("status-text").textContent = ok
    ? "Live prices from Binance demo · updated " + new Date(liveMs).toLocaleTimeString()
    : "Showing the snapshot from " + fmtTime(D.built_ms) + " (live prices unavailable)";
}
async function refresh() {
  try {
    const r = await fetch(D.demo_url + "/fapi/v1/premiumIndex");
    if (!r.ok) throw new Error(r.status);
    const marks = Object.fromEntries((await r.json()).map(x => [x.symbol, +x.markPrice]));
    for (const p of positions) if (marks[p.symbol]) p.mark = marks[p.symbol];
    liveMs = Date.now(); setStatus(true);
    const m = metrics();
    live.push({x: liveMs, equity: m.equity, long: m.long, short: m.short});
  } catch (e) { setStatus(false); }
  render();
}

// ----- controls -----
function openSection(toggle, panel, open, labels) {
  toggle.setAttribute("aria-expanded", open);
  toggle.textContent = labels(open);
  panel.classList.toggle("hidden", !open);
}
const holdLabel = open => (open ? "Hide holdings" : "Show holdings") + " (" + positions.length + ")";
const listLabel = open => open ? "Hide daily lists" : "Show daily lists";
$("hold-toggle").textContent = holdLabel(false);
$("hold-toggle").addEventListener("click", e =>
  openSection(e.currentTarget, $("holdings"), e.currentTarget.getAttribute("aria-expanded") !== "true", holdLabel));
$("list-toggle").addEventListener("click", e =>
  openSection(e.currentTarget, $("lists"), e.currentTarget.getAttribute("aria-expanded") !== "true", listLabel));
document.querySelectorAll("#hold-table th").forEach(th => th.addEventListener("click", () => {
  sortDir = th.dataset.key === sortKey ? -sortDir : (["symbol", "side"].includes(th.dataset.key) ? 1 : -1);
  sortKey = th.dataset.key; render();
}));
document.querySelectorAll("[data-mode]").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));
document.querySelectorAll("[data-filter]").forEach(b => b.addEventListener("click", () => {
  listFilter = b.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach(x => x.setAttribute("aria-pressed", x === b));
  renderLists();
}));
$("list-date").innerHTML = D.lists.map(d =>
  `<option value="${d.date}">${d.date}${d.traded ? "" : " (not traded)"}</option>`).join("");
$("list-date").addEventListener("change", renderLists);
document.querySelectorAll("[data-jump]").forEach(b => b.addEventListener("click", () => {
  const target = b.dataset.jump;
  if (target === "sides") { setMode("sides"); $("chart-card").scrollIntoView({behavior: "smooth"}); }
  if (target === "holdings") { openSection($("hold-toggle"), $("holdings"), true, holdLabel); $("hold-card").scrollIntoView({behavior: "smooth"}); }
  if (target === "lists") { openSection($("list-toggle"), $("lists"), true, listLabel); $("list-card").scrollIntoView({behavior: "smooth"}); }
}));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (chart) { chart.destroy(); chart = null; } render();
});

$("created").textContent = fmtTime(D.created_ms);
$("footer").textContent = "Holdings and entry prices as of " + fmtTime(D.built_ms) +
  ". Prices refresh every minute from the public demo market; the page holds no API keys. " +
  "Rebuilt after every demo_trade.py run, or with: python3 dashboard.py";
const hash = location.hash.slice(1);
if (hash === "holdings") openSection($("hold-toggle"), $("holdings"), true, holdLabel);
if (hash === "lists") openSection($("list-toggle"), $("lists"), true, listLabel);
if (hash === "sides") chartMode = "sides";
document.querySelectorAll("[data-mode]").forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === chartMode));
renderLists(); setStatus(false); render(); refresh(); setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="demo", help="demo ledger folder")
    args = parser.parse_args(argv)
    key, secret = load_keys()
    if not key or not secret:
        print("ERROR: no demo API keys in .env. Run: python3 demo_trade.py --set-keys",
              file=sys.stderr)
        return 1
    try:
        path = build(DemoClient(key, secret), args.dir)
    except ApiError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
