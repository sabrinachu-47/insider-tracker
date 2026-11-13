#!/usr/bin/env python3
"""
Insider Tracker - see what a company's directors and officers have been
buying and selling, straight from SEC Form 4 filings (free, no API key).

Usage
-----
  python insider_tracker.py                 # opens a search page in your browser
  python insider_tracker.py AAPL            # prints a table in the terminal
  python insider_tracker.py NVDA --days 180 --directors-only --open-market

When the PORT environment variable is set (e.g. on Render) it runs as a web server.

Requirements: Python 3.8+ only. No packages to install.

The SEC asks every program that downloads its data to identify itself with a
name and contact email (sent in the User-Agent header). On first run you'll be
asked for it once; it's saved to ~/.insider_tracker.json and only sent to sec.gov.
"""

import argparse
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # very old Python or no time-zone data
    EASTERN = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.path.expanduser("~/.insider_tracker.json")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

TRANSACTION_CODES = {
    "P": "Open-market buy",
    "S": "Open-market sale",
    "A": "Grant / award",
    "M": "Option exercise",
    "X": "Option exercise",
    "F": "Shares withheld for tax",
    "G": "Gift",
    "D": "Disposed to issuer",
    "C": "Conversion",
    "J": "Other",
    "W": "Inheritance",
    "I": "Discretionary",
    "V": "Voluntary report",
}

# Plain-English groups shown as filter buttons on the page
CATEGORY = {"P": "buy", "S": "sell", "A": "award", "M": "award", "X": "award", "C": "award",
            "F": "tax"}  # everything else -> "other" (gifts, inheritance, etc.)


def accepted_et(accepted):
    """SEC acceptance time is UTC ("2026-11-12T23:58:12.000Z"). Return (date, 'HH:MM ET')."""
    try:
        t = datetime.strptime(accepted[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None, ""
    if EASTERN is None:
        return t.date().isoformat(), t.strftime("%H:%M UTC")
    t = t.astimezone(EASTERN)
    return t.date().isoformat(), t.strftime("%I:%M %p ET").lstrip("0").replace("AM", "am").replace("PM", "pm")

# --------------------------------------------------------------------------
# SEC access
# --------------------------------------------------------------------------

_user_agent = None
_last_request = 0.0
_lock = threading.Lock()
_cache = {}


def get_user_agent():
    global _user_agent
    if _user_agent:
        return _user_agent
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua and os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                ua = json.load(f).get("user_agent")
        except Exception:
            ua = None
    if not ua and not sys.stdin.isatty():
        raise RuntimeError("SEC_USER_AGENT is not set. Add it as an environment variable, "
                           "e.g. 'Insider Tracker you@gmail.com'.")
    if not ua:
        print("The SEC requires a name and contact email for data downloads.")
        name = input("  Your name: ").strip() or "Insider Tracker"
        email = input("  Your email: ").strip()
        while "@" not in email:
            email = input("  Please enter a valid email: ").strip()
        ua = f"{name} {email}"
        with open(CONFIG_PATH, "w") as f:
            json.dump({"user_agent": ua}, f)
        print(f"Saved to {CONFIG_PATH}\n")
    _user_agent = ua
    return ua


def sec_get(url, as_json=False):
    """GET from SEC with polite rate limiting (<10 requests/sec) and caching."""
    global _last_request
    if url in _cache:
        return _cache[url]
    with _lock:
        wait = 0.12 - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
    req = urllib.request.Request(url, headers={
        "User-Agent": get_user_agent(),
        "Accept-Encoding": "gzip",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    text = data.decode("utf-8", errors="replace")
    result = json.loads(text) if as_json else text
    if len(_cache) > 3000:  # keep memory small on a free server
        _cache.clear()
    _cache[url] = result
    return result


_ticker_map = None


def lookup_company(query):
    """Return (cik, ticker, company name) for a ticker symbol or company name."""
    global _ticker_map
    if _ticker_map is None:
        raw = sec_get(TICKERS_URL, as_json=True)
        _ticker_map = list(raw.values())
    q = query.strip().upper()
    for row in _ticker_map:
        if row["ticker"].upper() == q:
            return int(row["cik_str"]), row["ticker"], row["title"]
    # fall back to company-name match
    for row in _ticker_map:
        if q in row["title"].upper():
            return int(row["cik_str"]), row["ticker"], row["title"]
    return None


def ticker_list():
    """[[ticker, company name], ...] for search suggestions."""
    lookup_company("AAPL")  # makes sure the list is loaded
    return [[r["ticker"], r["title"]] for r in _ticker_map]


# --------------------------------------------------------------------------
# Form 4 parsing
# --------------------------------------------------------------------------

def _val(node, path):
    if node is None:
        return None
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    text = (v.text if v is not None else el.text) or ""
    return text.strip() or None


def _num(s):
    try:
        return float(s.replace(",", "")) if s is not None else None
    except ValueError:
        return None


def parse_form4(xml_text):
    """Parse a Form 4 XML document into a list of transaction dicts."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)

    owners = []
    for ro in root.findall("reportingOwner"):
        name = _val(ro, "reportingOwnerId/rptOwnerName") or "Unknown"
        rel = ro.find("reportingOwnerRelationship")
        flag = lambda tag: (_val(rel, tag) or "").lower() in ("1", "true")
        roles = []
        if flag("isDirector"):
            roles.append("Director")
        if flag("isOfficer"):
            roles.append(_val(rel, "officerTitle") or "Officer")
        if flag("isTenPercentOwner"):
            roles.append("10% Owner")
        if flag("isOther"):
            roles.append(_val(rel, "otherText") or "Other")
        owners.append({
            "name": name,
            "roles": roles,
            "is_director": flag("isDirector"),
        })

    footnotes = {fn.get("id"): " ".join((fn.text or "").split())
                 for fn in root.findall("footnotes/footnote")}

    out = []
    tables = [("nonDerivativeTable/nonDerivativeTransaction", False),
              ("derivativeTable/derivativeTransaction", True)]
    for path, is_deriv in tables:
        for tx in root.findall(path):
            shares = _num(_val(tx, "transactionAmounts/transactionShares"))
            price = _num(_val(tx, "transactionAmounts/transactionPricePerShare"))
            code = _val(tx, "transactionCoding/transactionCode") or ""
            ad = _val(tx, "transactionAmounts/transactionAcquiredDisposedCode")
            notes = [footnotes.get(f.get("id"), "") for f in tx.iter("footnoteId")]
            out.append({
                "security": _val(tx, "securityTitle") or "",
                "derivative": is_deriv,
                "date": (_val(tx, "transactionDate") or "")[:10] or None,
                "code": code,
                "type": TRANSACTION_CODES.get(code, code or "?"),
                "acq_disp": ad,  # A = acquired, D = disposed
                "shares": shares,
                "price": price,
                "value": round(shares * price, 2) if shares and price else None,
                "owned_after": _num(_val(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                "plan_10b5_1": any("10b5-1" in n for n in notes),
                "footnotes": [n for n in notes if n],
            })

    return owners, out


# --------------------------------------------------------------------------
# Main lookup
# --------------------------------------------------------------------------

def get_insider_trades(query, days=90, include_directors=True, include_others=True,
                       open_market_only=False, include_derivative=False,
                       max_filings=400, progress=None):
    """include_directors: board members (incl. executives who also sit on the board).
    include_others: executives and 10% owners who are not on the board."""
    found = lookup_company(query)
    if not found:
        raise ValueError(f"No SEC-registered company found for '{query}'.")
    cik, ticker, company = found

    subs = sec_get(SUBMISSIONS_URL.format(cik=cik), as_json=True)
    recent = subs["filings"]["recent"]
    cutoff = date.today() - timedelta(days=days)

    filings = []
    for i, form in enumerate(recent["form"]):
        if form not in ("4", "4/A"):
            continue
        filed = recent["filingDate"][i]
        if date.fromisoformat(filed) < cutoff:
            continue
        filings.append({
            "form": form,
            "filed": filed,
            "accepted": recent.get("acceptanceDateTime", [""] * len(recent["form"]))[i],
            "accession": recent["accessionNumber"][i],
            "doc": recent["primaryDocument"][i],
        })
    filings = filings[:max_filings]

    # Download filings several at a time; sec_get() keeps us under the SEC's
    # limit of 10 requests per second.
    def load(f):
        acc = f["accession"].replace("-", "")
        doc = f["doc"].split("/")[-1]  # strip the xsl rendering prefix -> raw XML
        url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc)
        try:
            return parse_form4(sec_get(url))
        except Exception as e:  # skip a broken filing, keep going
            print(f"  ! skipped {url}: {e}", file=sys.stderr)
            return None

    done = [0]

    def load_with_progress(f):
        res = load(f)
        done[0] += 1
        if progress:
            progress(done[0], len(filings))
        return res

    with ThreadPoolExecutor(max_workers=6) as pool:
        parsed = list(pool.map(load_with_progress, filings))

    rows = []
    for f, result in zip(filings, parsed):
        if not result:
            continue
        owners, txs = result
        acc = f["accession"].replace("-", "")
        on_board = any(o["is_director"] for o in owners)
        if (on_board and not include_directors) or (not on_board and not include_others):
            continue
        group = "Board director" if on_board else "Other insider"
        name = " / ".join(o["name"] for o in owners)
        roles = sorted({r for o in owners for r in o["roles"]})
        rep_date, rep_time = accepted_et(f["accepted"])
        for tx in txs:
            if tx["derivative"] and not include_derivative:
                continue
            if open_market_only and tx["code"] not in ("P", "S"):
                continue
            if tx["date"] and date.fromisoformat(tx["date"][:10]) < cutoff:
                continue
            rows.append({
                **tx,
                "category": CATEGORY.get(tx["code"], "other"),
                "insider": name,
                "roles": ", ".join(roles),
                "group": group,
                "filed": rep_date or f["filed"],
                "filed_time": rep_time,
                "form": f["form"],
                "filing_url": FILING_INDEX_URL.format(cik=cik, acc=acc),
            })

    rows.sort(key=lambda r: (r["date"] or "", r["filed"]), reverse=True)

    buys = [r for r in rows if r["code"] == "P"]
    sells = [r for r in rows if r["code"] == "S"]
    summary = {
        "buy_count": len(buys),
        "buy_shares": sum(r["shares"] or 0 for r in buys),
        "buy_value": sum(r["value"] or 0 for r in buys),
        "sell_count": len(sells),
        "sell_shares": sum(r["shares"] or 0 for r in sells),
        "sell_value": sum(r["value"] or 0 for r in sells),
    }
    return {
        "ticker": ticker, "company": company, "cik": cik, "days": days,
        "filings_checked": len(filings), "rows": rows, "summary": summary,
    }


# --------------------------------------------------------------------------
# Terminal output
# --------------------------------------------------------------------------

def fmt_money(v):
    if v is None:
        return "-"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:,.2f}{unit}"
    return f"${v:,.2f}"


def print_report(res):
    s = res["summary"]
    print(f"\n{res['company']} ({res['ticker']}) - insider trades, last {res['days']} days")
    print(f"{res['filings_checked']} Form 4 filings checked\n")
    print(f"  Open-market BUYS : {s['buy_count']:>3} trades  {s['buy_shares']:>14,.0f} sh  {fmt_money(s['buy_value'])}")
    print(f"  Open-market SELLS: {s['sell_count']:>3} trades  {s['sell_shares']:>14,.0f} sh  {fmt_money(s['sell_value'])}\n")
    if not res["rows"]:
        print("  No matching transactions.")
        return
    hdr = f"{'Trade date':<11} {'Filed (ET)':<23} {'Insider':<26} {'Role':<22} {'Type':<26} {'Shares':>12} {'Price':>10} {'Value':>12}"
    print(hdr)
    print("-" * len(hdr))
    for r in res["rows"]:
        sign = "-" if r["acq_disp"] == "D" else "+"
        shares = f"{sign}{r['shares']:,.0f}" if r["shares"] is not None else "-"
        price = f"${r['price']:,.2f}" if r["price"] else "-"
        typ = r["type"] + (" (10b5-1)" if r["plan_10b5_1"] else "")
        print(f"{(r['date'] or '')[:10]:<11} {(r['filed'] + ' ' + r['filed_time']).strip():<23} "
              f"{r['insider'][:26]:<26} {r['roles'][:22]:<22} {typ[:26]:<26} "
              f"{shares:>12} {price:>10} {fmt_money(r['value']):>12}")


# --------------------------------------------------------------------------
# Browser UI
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insider Tracker</title>
<style>
  :root { --bg:#f6f6f3; --card:#fff; --ink:#1c1c1e; --muted:#6b6b70; --line:#e3e3de;
          --buy:#0f7b4f; --buy-bg:#e5f3eb; --sell:#b42318; --sell-bg:#fbe9e7; --accent:#2f4fd8; --accent-bg:#eef0fb; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:var(--bg); color:var(--ink); }
  .wrap { max-width:1180px; margin:0 auto; padding:28px 24px 56px; }
  h1 { font-size:24px; margin:0 0 4px; letter-spacing:-.01em; }
  h2 { font-size:16px; margin:28px 0 10px; }
  .sub { color:var(--muted); }
  form { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; margin:20px 0 8px; }
  .qwrap { position:relative; }
  input[type=text] { font-size:16px; padding:11px 14px; border:1px solid var(--line); border-radius:9px; width:260px; background:#fff; }
  select { font-size:14px; padding:10px 12px; border-radius:9px; border:1px solid var(--line); background:#fff; }
  button { font:inherit; cursor:pointer; }
  .primary { font-size:15px; padding:11px 22px; border-radius:9px; border:none; background:var(--accent); color:#fff; font-weight:600; }
  .primary:disabled { opacity:.55; cursor:wait; }
  .ghost { padding:6px 12px; border-radius:8px; border:1px solid var(--line); background:#fff; color:var(--ink); font-size:13px; }
  .ghost:hover { border-color:#c9c9c2; }
  label.chk { display:flex; gap:6px; align-items:center; color:var(--ink); cursor:pointer; }
  .hint { color:var(--muted); font-size:12px; }
  .sugg { position:absolute; top:100%; left:0; right:0; margin-top:4px; background:#fff; border:1px solid var(--line); border-radius:10px; box-shadow:0 8px 24px rgba(0,0,0,.08); z-index:10; overflow:hidden; display:none; min-width:300px; }
  .sugg div { padding:9px 12px; cursor:pointer; display:flex; gap:10px; }
  .sugg div b { min-width:56px; }
  .sugg div span { color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sugg div.on, .sugg div:hover { background:var(--accent-bg); }
  .status { color:var(--muted); padding:10px 0; }
  .err { color:var(--sell); }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid #ccc; border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; vertical-align:-2px; margin-right:8px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .rhead { display:flex; flex-wrap:wrap; justify-content:space-between; align-items:flex-end; gap:10px; margin-top:14px; }
  .title { font-size:22px; font-weight:700; }
  .actions { display:flex; gap:8px; }
  .insights { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 18px; margin:16px 0; }
  .insights div { padding:4px 0 4px 22px; position:relative; }
  .insights div::before { content:""; position:absolute; left:4px; top:11px; width:8px; height:8px; border-radius:50%; background:#c9c9c2; }
  .insights div.g::before { background:var(--buy); } .insights div.r::before { background:var(--sell); }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
  .card .v { font-size:24px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
  .card .s { color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }
  .card.buy .v { color:var(--buy); } .card.sell .v { color:var(--sell); }
  .chart { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px 6px; }
  .chart svg { width:100%; height:auto; display:block; }
  .legend { display:flex; flex-wrap:wrap; gap:4px 16px; white-space:nowrap; color:var(--muted); font-size:12px; padding:0 2px 6px; }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 12px; }
  .chip { padding:7px 12px; border-radius:999px; border:1px solid var(--line); background:#fff; font-size:13px; color:var(--muted); }
  .chip.on { background:var(--ink); border-color:var(--ink); color:#fff; }
  .chip:disabled { opacity:.45; cursor:default; }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); vertical-align:top; }
  th { font-size:11.5px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.04em; background:#fafaf7; white-space:nowrap; }
  th.s { cursor:pointer; user-select:none; }
  td { white-space:nowrap; }
  td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
  tr:last-child td { border-bottom:none; }
  .pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; background:#efefeb; color:#444; }
  .pill.buy { background:var(--buy-bg); color:var(--buy); }
  .pill.sell { background:var(--sell-bg); color:var(--sell); }
  .tag { font-size:11px; color:var(--muted); margin-left:4px; }
  .role { color:var(--muted); font-size:12.5px; white-space:normal; max-width:260px; }
  .grp { display:inline-block; font-size:11px; padding:1px 7px; border-radius:6px; background:var(--accent-bg); color:var(--accent); margin-right:4px; }
  .grp.o { background:#f1efe8; color:#6b5d3a; }
  .pos { color:var(--buy); } .neg { color:var(--sell); }
  a { color:var(--accent); text-decoration:none; }
  .more { margin-top:8px; }
  .foot { color:var(--muted); font-size:12px; margin-top:22px; line-height:1.7; }
  .toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:var(--ink); color:#fff; padding:9px 16px; border-radius:9px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; }
  .toast.show { opacity:1; }
  @media (max-width:700px) {
    .wrap { padding:20px 14px 40px; }
    .qwrap, input[type=text] { width:100%; }
    form > * { flex:1 1 100%; }
    .primary { width:100%; }
    .chart { overflow-x:auto; }
    .chart svg { min-width:680px; }
    table.resp, table.resp tbody, table.resp tr, table.resp td { display:block; width:100%; border:none; }
    table.resp thead { display:none; }
    table.resp { background:none; }
    table.resp tr { background:var(--card); border:1px solid var(--line); border-radius:12px; margin-bottom:10px; padding:6px 4px; }
    table.resp td { display:flex; justify-content:space-between; gap:12px; padding:5px 12px; border:none; white-space:normal; text-align:right; }
    table.resp td::before { content:attr(data-l); color:var(--muted); font-size:12px; text-align:left; }
    .role { max-width:none; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Insider Tracker</h1>
  <div class="sub">See when a company's directors, executives and big shareholders buy or sell its stock, from SEC Form 4 filings.</div>

  <form id="f" autocomplete="off">
    <div class="qwrap">
      <input id="q" type="text" placeholder="Ticker or company, e.g. AAPL or Apple" autofocus>
      <div id="sugg" class="sugg"></div>
    </div>
    <select id="days">
      <option value="30">Last 30 days</option>
      <option value="90" selected>Last 90 days</option>
      <option value="180">Last 6 months</option>
      <option value="365">Last 12 months</option>
    </select>
    <label class="chk"><input type="checkbox" id="dir" checked> Board directors</label>
    <label class="chk"><input type="checkbox" id="oth" checked> Other insiders <span class="hint">(executives &amp; 10% owners not on the board)</span></label>
    <button id="go" class="primary">Search</button>
  </form>

  <div id="status" class="status"></div>
  <div id="out"></div>
</div>
<div id="toast" class="toast"></div>

<script>
var $ = function (id) { return document.getElementById(id); };
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function money(v) {
  if (v == null) return '–';
  var a = Math.abs(v);
  if (a >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
  if (a >= 1e4) return '$' + Math.round(v / 1e3) + 'K';
  return '$' + Math.round(v).toLocaleString();
}
function num(v) { return v == null ? '–' : Math.round(v).toLocaleString(); }
function sum(list, f) { return list.reduce(function (s, r) { return s + (f(r) || 0); }, 0); }
function niceDate(s) { if (!s) return ''; var d = new Date(s + 'T12:00:00'); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); }
function toast(msg) { var t = $('toast'); t.textContent = msg; t.className = 'toast show'; setTimeout(function () { t.className = 'toast'; }, 1800); }

/* Percent of the person's holdings that a trade represents */
function pctHoldings(r) {
  if (r.shares == null || r.owned_after == null) return null;
  if (r.acq_disp === 'D') { var before = r.owned_after + r.shares; return before > 0 ? -r.shares / before : null; }
  var prior = r.owned_after - r.shares; return prior > 0 ? r.shares / prior : null;
}
function pctText(p) {
  if (p == null) return '–';
  if (p > 9.99) return 'new stake';
  var v = Math.abs(p * 100); return (p < 0 ? '−' : '+') + (v < 1 ? v.toFixed(1) : Math.round(v)) + '%';
}

var CATS = [['buy','Buys'],['sell','Sales'],['award','Awards & option exercises'],['tax','Tax withholding'],['other','Gifts & other']];
var data = null, rows = [], shown = {}, sortKey = 'date', sortDir = -1, showAllPeople = false;

/* ---------- search suggestions ---------- */
var tickers = null, suggIdx = -1, suggItems = [];
function loadTickers() {
  if (tickers) return;
  tickers = [];
  fetch('api/tickers').then(function (r) { return r.json(); }).then(function (d) { if (Array.isArray(d)) tickers = d; }).catch(function () {});
}
function showSugg() {
  var q = $('q').value.trim().toUpperCase(), box = $('sugg');
  if (!q || !tickers || !tickers.length) { box.style.display = 'none'; return; }
  var starts = [], name = [];
  for (var i = 0; i < tickers.length && starts.length < 8; i++) if (tickers[i][0].toUpperCase().indexOf(q) === 0) starts.push(tickers[i]);
  for (var j = 0; j < tickers.length && starts.length + name.length < 8; j++) {
    var t = tickers[j]; if (t[1].toUpperCase().indexOf(q) >= 0 && starts.indexOf(t) < 0) name.push(t);
  }
  suggItems = starts.concat(name).slice(0, 8); suggIdx = -1;
  if (!suggItems.length) { box.style.display = 'none'; return; }
  box.innerHTML = suggItems.map(function (t, k) { return '<div data-k="' + k + '"><b>' + esc(t[0]) + '</b><span>' + esc(t[1]) + '</span></div>'; }).join('');
  box.style.display = 'block';
  Array.prototype.forEach.call(box.children, function (el) {
    el.onmousedown = function (e) { e.preventDefault(); pick(+el.getAttribute('data-k')); };
  });
}
function pick(k) { $('q').value = suggItems[k][0]; $('sugg').style.display = 'none'; search(); }
$('q').onfocus = loadTickers;
$('q').oninput = showSugg;
$('q').onblur = function () { setTimeout(function () { $('sugg').style.display = 'none'; }, 100); };
$('q').onkeydown = function (e) {
  var box = $('sugg'); if (box.style.display !== 'block') return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); suggIdx = (suggIdx + (e.key === 'ArrowDown' ? 1 : -1) + suggItems.length) % suggItems.length;
    Array.prototype.forEach.call(box.children, function (el, k) { el.className = k === suggIdx ? 'on' : ''; });
  } else if (e.key === 'Enter' && suggIdx >= 0) { e.preventDefault(); pick(suggIdx); }
  else if (e.key === 'Escape') box.style.display = 'none';
};

/* ---------- search ---------- */
$('f').onsubmit = function (e) { e.preventDefault(); $('sugg').style.display = 'none'; search(); };

function search() {
  var q = $('q').value.trim();
  if (!q) return;
  $('out').innerHTML = '';
  if (!$('dir').checked && !$('oth').checked) {
    $('status').className = 'status err'; $('status').textContent = 'Tick "Board directors", "Other insiders", or both.'; return;
  }
  $('go').disabled = true;
  $('status').className = 'status';
  $('status').innerHTML = '<span class="spinner"></span>Reading SEC filings… big companies can take up to 30 seconds (up to a minute if the site was asleep).';
  var params = { q: q, days: $('days').value, dir: $('dir').checked ? 1 : 0, oth: $('oth').checked ? 1 : 0 };
  fetch('api?' + new URLSearchParams(params)).then(function (r) { return r.json(); }).then(function (d) {
    if (d.error) throw new Error(d.error);
    $('status').textContent = ''; $('go').disabled = false;
    params.q = d.ticker;
    try { history.replaceState(null, '', '?' + new URLSearchParams(params)); } catch (e) {}
    render(d);
  }).catch(function (err) {
    $('status').className = 'status err'; $('status').textContent = err.message || String(err); $('go').disabled = false;
  });
}

/* ---------- results ---------- */
function render(d) {
  data = d; rows = d.rows; showAllPeople = false;
  var counts = {}; CATS.forEach(function (c) { counts[c[0]] = 0; });
  rows.forEach(function (r) { counts[r.category] = (counts[r.category] || 0) + 1; });
  shown = { buy: true, sell: true };
  if (!counts.buy && !counts.sell) CATS.forEach(function (c) { shown[c[0]] = true; });
  sortKey = 'date'; sortDir = -1;

  var buys = rows.filter(function (r) { return r.category === 'buy'; });
  var sells = rows.filter(function (r) { return r.category === 'sell'; });
  var bv = sum(buys, function (r) { return r.value; }), sv = sum(sells, function (r) { return r.value; });
  var planned = sum(sells, function (r) { return r.plan_10b5_1 ? r.value : 0; });
  var net = bv - sv;
  var period = { 30: 'the last 30 days', 90: 'the last 90 days', 180: 'the last 6 months', 365: 'the last 12 months' }[d.days] || ('the last ' + d.days + ' days');

  $('out').innerHTML =
    '<div class="rhead"><div><div class="title">' + esc(d.company) + ' (' + esc(d.ticker) + ')</div>' +
      '<div class="sub">' + d.filings_checked + ' insider filings in ' + period + ' · <a target="_blank" href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=' + d.cik + '&type=4&owner=include&count=40">view on SEC website</a></div></div>' +
      '<div class="actions"><button class="ghost" id="copy">Copy link</button><button class="ghost" id="csv">Download CSV</button></div></div>' +
    '<div class="insights">' + insights(buys, sells, bv, sv, planned, period) + '</div>' +
    '<div class="cards">' +
      card('buy', 'Bought on the market', money(bv), buys.length + ' trades · ' + num(sum(buys, function (r) { return r.shares; })) + ' shares · ' + people(buys) + ' people') +
      card('sell', 'Sold on the market', money(sv), sells.length + ' trades · ' + num(sum(sells, function (r) { return r.shares; })) + ' shares' +
        (sv ? '<br>' + money(planned) + ' pre-planned (10b5-1) · ' + money(sv - planned) + ' by choice' : '')) +
      card(net >= 0 ? 'buy' : 'sell', 'Net', (net < 0 ? '−' : '') + money(Math.abs(net)), net > 0 ? 'More buying than selling' : net < 0 ? 'More selling than buying' : 'No market trades') +
    '</div>' +
    ((buys.length || sells.length) ? '<h2>Activity over time</h2><div class="chart">' + chart(buys, sells, d.days) + '</div>' : '') +
    ((buys.length || sells.length) ? '<h2>Who\'s trading</h2><div id="people"></div>' : '') +
    '<h2>All transactions</h2><div class="chips" id="chips"></div><div id="tbl"></div>' +
    '<div class="foot"><b>Bought / sold on the market</b>: the insider chose to buy or sell at market price with their own money. These say the most about what insiders think of the stock.<br>' +
      '<b>10b5-1 / pre-planned</b>: the trade was scheduled months ahead under a trading plan, so it says less about the insider\'s current view.<br>' +
      '<b>Awards & option exercises</b>: stock received as pay. <b>Tax withholding</b>: shares the company kept to cover taxes when stock pay vested; the insider didn\'t choose to sell.<br>' +
      '<b>% of holdings</b>: how much of that person\'s shares the trade represents. Value = shares × reported price. Data from SEC Form 4 filings, usually filed within 2 business days of the trade. Not investment advice.</div>';

  $('copy').onclick = copyLink;
  $('csv').onclick = downloadCsv;
  if ($('people')) peopleTable();
  chips(counts);
  table();
}

function card(cls, k, v, s) { return '<div class="card ' + cls + '"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="s">' + s + '</div></div>'; }
function people(list) { var o = {}; list.forEach(function (r) { o[r.insider] = 1; }); return Object.keys(o).length; }

function insights(buys, sells, bv, sv, planned, period) {
  var out = [];
  if (!buys.length) {
    out.push(['', 'No insider bought shares on the open market in ' + period + '. That\'s common at large companies, where insiders receive stock as pay and rarely buy more.']);
  } else {
    var n = people(buys);
    var big = buys.slice().sort(function (a, b) { return (b.value || 0) - (a.value || 0); })[0];
    out.push(['g', n + (n === 1 ? ' insider' : ' different insiders') + ' bought ' + money(bv) + ' of stock on the open market' +
      (n >= 2 ? '. Several insiders buying around the same time (“cluster buying”) is often seen as a stronger signal than one purchase.' : '.') +
      ' Largest: ' + esc(big.insider) + ', ' + money(big.value) + ' on ' + niceDate(big.date) + '.']);
  }
  if (sells.length) {
    var pct = sv ? Math.round(planned / sv * 100) : 0;
    out.push(['r', money(sv) + ' sold by ' + people(sells) + (people(sells) === 1 ? ' insider. ' : ' insiders. ') +
      (pct >= 99 ? 'All of it was pre-planned under 10b5-1 trading plans.' : pct > 0 ? pct + '% was pre-planned under 10b5-1 trading plans; ' + money(sv - planned) + ' was sold by choice.' : 'None of it was under a 10b5-1 plan, so these were sales by choice.')]);
    var withPct = sells.filter(function (r) { return pctHoldings(r) != null; });
    if (withPct.length) {
      var top = withPct.sort(function (a, b) { return pctHoldings(a) - pctHoldings(b); })[0];
      var p = -pctHoldings(top);
      if (p >= 0.1) out.push(['r', 'Biggest sale relative to holdings: ' + esc(top.insider) + ' sold ' + Math.round(p * 100) + '% of their shares on ' + niceDate(top.date) + (top.plan_10b5_1 ? ' (pre-planned).' : '.')]);
    }
  } else {
    out.push(['', 'No insider sold shares on the open market in ' + period + '.']);
  }
  return out.map(function (o) { return '<div class="' + o[0] + '">' + o[1] + '</div>'; }).join('');
}

/* ---------- chart: buys above the line, sales below ---------- */
function chart(buys, sells, days) {
  var weekly = days <= 90, end = new Date(), start = new Date(end.getTime() - days * 864e5), buckets = [];
  function key(ds) {
    var dt = new Date(ds + 'T12:00:00');
    if (!weekly) return dt.getFullYear() + '-' + dt.getMonth();
    return Math.floor((dt - start) / (7 * 864e5));
  }
  if (weekly) {
    for (var w = 0; w <= Math.floor(days / 7); w++) buckets.push({ k: w, label: new Date(start.getTime() + w * 7 * 864e5), b: 0, s: 0 });
  } else {
    var m = new Date(start.getFullYear(), start.getMonth(), 1);
    while (m <= end) { buckets.push({ k: m.getFullYear() + '-' + m.getMonth(), label: new Date(m), b: 0, s: 0 }); m = new Date(m.getFullYear(), m.getMonth() + 1, 1); }
  }
  var idx = {}; buckets.forEach(function (b, i) { idx[b.k] = i; });
  buys.forEach(function (r) { var i = idx[key(r.date)]; if (i != null) buckets[i].b += r.value || 0; });
  sells.forEach(function (r) { var i = idx[key(r.date)]; if (i != null) buckets[i].s += r.value || 0; });
  var max = Math.max.apply(null, buckets.map(function (b) { return Math.max(b.b, b.s); }).concat([1]));
  var W = 1100, H = 200, top = 14, mid = 92, half = 74, n = buckets.length, slot = (W - 80) / n, bw = Math.min(30, slot * 0.6);
  var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Insider buying and selling over time">';
  svg += '<text x="0" y="' + (mid - half + 10) + '" font-size="12" fill="#0f7b4f">+' + money(max) + '</text>';
  svg += '<text x="0" y="' + (mid + 4) + '" font-size="12" fill="#6b6b70">$0</text>';
  svg += '<text x="0" y="' + (mid + half) + '" font-size="12" fill="#b42318">−' + money(max) + '</text>';
  svg += '<line x1="72" x2="' + W + '" y1="' + mid + '" y2="' + mid + '" stroke="#c9c9c2"/>';
  buckets.forEach(function (b, i) {
    var x = 76 + i * slot + (slot - bw) / 2, lab = b.label.toLocaleDateString(undefined, weekly ? { month: 'short', day: 'numeric' } : { month: 'short' });
    var bh = b.b / max * half, sh = b.s / max * half;
    if (b.b) svg += '<rect x="' + x + '" y="' + (mid - bh) + '" width="' + bw + '" height="' + Math.max(bh, 1.5) + '" rx="2" fill="#0f7b4f"><title>' + lab + ': bought ' + money(b.b) + '</title></rect>';
    if (b.s) svg += '<rect x="' + x + '" y="' + mid + '" width="' + bw + '" height="' + Math.max(sh, 1.5) + '" rx="2" fill="#b42318"><title>' + lab + ': sold ' + money(b.s) + '</title></rect>';
    var every = Math.ceil(n / 8);
    if (i % every === 0) svg += '<text x="' + (x + bw / 2) + '" y="' + (H - 2) + '" font-size="12" text-anchor="middle" fill="#6b6b70">' + lab + '</text>';
  });
  return '<div class="legend"><span><i style="background:#0f7b4f"></i>Bought</span><span><i style="background:#b42318"></i>Sold</span><span>' + (weekly ? 'amount per week' : 'amount per month') + '</span></div>' + svg + '</svg>';
}

/* ---------- who's trading ---------- */
function peopleTable() {
  var byName = {};
  rows.forEach(function (r) {
    if (r.category !== 'buy' && r.category !== 'sell') return;
    var p = byName[r.insider] || (byName[r.insider] = { name: r.insider, group: r.group, roles: r.roles, bought: 0, sold: 0, trades: 0, last: '', maxPct: null });
    if (r.category === 'buy') p.bought += r.value || 0; else p.sold += r.value || 0;
    p.trades++; if (r.date > p.last) p.last = r.date;
    var pc = pctHoldings(r); if (r.category === 'sell' && pc != null && (p.maxPct == null || pc < p.maxPct)) p.maxPct = pc;
  });
  var list = Object.keys(byName).map(function (k) { return byName[k]; }).sort(function (a, b) { return (b.bought + b.sold) - (a.bought + a.sold); });
  var shownList = showAllPeople ? list : list.slice(0, 8);
  var h = '<table class="resp"><thead><tr><th>Insider</th><th class="n">Bought</th><th class="n">Sold</th><th class="n">Net</th><th class="n">Trades</th><th class="n">Biggest sale (% of their shares)</th><th>Last trade</th></tr></thead><tbody>';
  shownList.forEach(function (p) {
    var net = p.bought - p.sold;
    h += '<tr><td data-l="Insider"><div>' + esc(p.name) + who(p) + '</div></td>' +
      '<td class="n pos" data-l="Bought">' + (p.bought ? money(p.bought) : '–') + '</td>' +
      '<td class="n neg" data-l="Sold">' + (p.sold ? money(p.sold) : '–') + '</td>' +
      '<td class="n ' + (net >= 0 ? 'pos' : 'neg') + '" data-l="Net">' + (net < 0 ? '−' : '+') + money(Math.abs(net)) + '</td>' +
      '<td class="n" data-l="Trades">' + p.trades + '</td>' +
      '<td class="n" data-l="Biggest sale (% of their shares)">' + (p.maxPct == null ? '–' : pctText(p.maxPct)) + '</td>' +
      '<td data-l="Last trade">' + niceDate(p.last) + '</td></tr>';
  });
  h += '</tbody></table>';
  if (list.length > 8) h += '<div class="more"><button class="ghost" id="morePeople">' + (showAllPeople ? 'Show fewer' : 'Show all ' + list.length + ' insiders') + '</button></div>';
  $('people').innerHTML = h;
  if ($('morePeople')) $('morePeople').onclick = function () { showAllPeople = !showAllPeople; peopleTable(); };
}
function who(r) { return '<div class="role"><span class="grp' + (r.group === 'Board director' ? '' : ' o') + '">' + esc(r.group) + '</span>' + esc(r.roles) + '</div>'; }

/* ---------- transaction type buttons ---------- */
function chips(counts) {
  $('chips').innerHTML = CATS.map(function (c) {
    return '<button class="chip' + (shown[c[0]] ? ' on' : '') + '" data-c="' + c[0] + '"' + (counts[c[0]] ? '' : ' disabled') + '>' + c[1] + ' (' + counts[c[0]] + ')</button>';
  }).join('');
  Array.prototype.forEach.call(document.querySelectorAll('.chip'), function (b) {
    b.onclick = function () { var c = b.getAttribute('data-c'); shown[c] = !shown[c]; b.className = 'chip' + (shown[c] ? ' on' : ''); table(); };
  });
}

/* ---------- transactions table ---------- */
function table() {
  var list = rows.filter(function (r) { return shown[r.category]; });
  if (!list.length) { $('tbl').innerHTML = '<div class="status">' + (rows.length ? 'Pick a transaction type above to see those trades.' : 'No insider filings with transactions in this period.') + '</div>'; return; }
  var cols = [['date','Trade date'],['filed','Reported'],['insider','Who'],['type','Transaction'],
              ['shares','Shares','n'],['price','Price','n'],['value','Value','n'],['pct','% of holdings','n'],['','Filing']];
  function val(r, k) { return k === 'pct' ? pctHoldings(r) : r[k]; }
  list.sort(function (a, b) {
    var x = val(a, sortKey), y = val(b, sortKey);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
  });
  var h = '<table class="resp"><thead><tr>' + cols.map(function (c) {
    return '<th class="' + (c[2] || '') + (c[0] ? ' s' : '') + '" data-k="' + c[0] + '">' + c[1] + (c[0] === sortKey ? (sortDir < 0 ? ' ↓' : ' ↑') : '') + '</th>';
  }).join('') + '</tr></thead><tbody>';
  list.forEach(function (r) {
    var p = pctHoldings(r);
    h += '<tr>' +
      '<td data-l="Trade date">' + esc(r.date) + '</td>' +
      '<td data-l="Reported">' + esc(r.filed) + ' <span class="tag">' + esc(r.filed_time) + '</span></td>' +
      '<td data-l="Who"><div>' + esc(r.insider) + who(r) + '</div></td>' +
      '<td data-l="Transaction"><span><span class="pill ' + esc(r.category) + '">' + esc(r.type) + '</span>' + (r.plan_10b5_1 ? '<span class="tag">10b5-1</span>' : '') + '</span></td>' +
      '<td class="n" data-l="Shares">' + (r.acq_disp === 'D' ? '−' : '+') + num(r.shares) + '</td>' +
      '<td class="n" data-l="Price">' + (r.price ? '$' + r.price.toFixed(2) : '–') + '</td>' +
      '<td class="n" data-l="Value">' + money(r.value) + '</td>' +
      '<td class="n ' + (p == null ? '' : p < 0 ? 'neg' : 'pos') + '" data-l="% of holdings">' + pctText(p) + '</td>' +
      '<td data-l="Filing"><a target="_blank" href="' + esc(r.filing_url) + '">Form ' + esc(r.form) + '</a></td>' +
    '</tr>';
  });
  $('tbl').innerHTML = h + '</tbody></table>';
  Array.prototype.forEach.call(document.querySelectorAll('#tbl th[data-k]'), function (th) {
    th.onclick = function () {
      var k = th.getAttribute('data-k'); if (!k) return;
      sortDir = (k === sortKey) ? -sortDir : -1; sortKey = k; table();
    };
  });
}

/* ---------- sharing & export ---------- */
function copyLink() {
  var url = location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).then(function () { toast('Link copied'); }, function () { prompt('Copy this link:', url); });
  else prompt('Copy this link:', url);
}
function downloadCsv() {
  var head = ['Trade date','Reported','Reported time','Insider','Group','Role','Transaction','Category','10b5-1 plan','Shares','Acquired/Disposed','Price','Value','Shares held after','% of holdings','Filing'];
  var lines = [head].concat(rows.map(function (r) {
    var p = pctHoldings(r);
    return [r.date, r.filed, r.filed_time, r.insider, r.group, r.roles, r.type, r.category, r.plan_10b5_1 ? 'yes' : 'no', r.shares, r.acq_disp, r.price, r.value, r.owned_after, p == null ? '' : (p * 100).toFixed(1) + '%', r.filing_url];
  })).map(function (a) { return a.map(function (v) { v = v == null ? '' : String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }).join(','); });
  var blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = data.ticker + '-insider-trades-' + data.days + 'd.csv';
  document.body.appendChild(a); a.click(); a.remove();
}

/* ---------- open a shared link ---------- */
(function () {
  var p = new URLSearchParams(location.search);
  if (!p.get('q')) return;
  $('q').value = p.get('q');
  if (p.get('days')) $('days').value = p.get('days');
  if (p.get('dir') === '0') $('dir').checked = false;
  if (p.get('oth') === '0') $('oth').checked = false;
  search();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        gz = "gzip" in self.headers.get("Accept-Encoding", "") and len(data) > 2000
        if gz:
            data = gzip.compress(data)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/api/tickers":  # for search suggestions on the page
            try:
                return self._send(200, json.dumps(ticker_list()), "application/json")
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}), "application/json")
        if url.path == "/api":
            p = urllib.parse.parse_qs(url.query)
            try:
                res = get_insider_trades(
                    p.get("q", [""])[0],
                    days=max(1, min(365, int(p.get("days", ["90"])[0]))),
                    include_directors=p.get("dir", ["1"])[0] == "1",
                    include_others=p.get("oth", ["1"])[0] == "1",
                )
                body = json.dumps(res)
            except urllib.error.HTTPError as e:
                hint = (" The SEC refused the request; check that SEC_USER_AGENT contains a real email."
                        if e.code == 403 else "")
                body = json.dumps({"error": f"SEC returned HTTP {e.code}.{hint}"})
            except Exception as e:
                body = json.dumps({"error": str(e)})
            return self._send(200, body, "application/json")
        self._send(404, "not found", "text/plain")


def serve(port, host="127.0.0.1", open_browser=True):
    get_user_agent()  # ask for SEC contact info up front, in the terminal
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Insider Tracker running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    ap = argparse.ArgumentParser(description="Track insider (director/officer) stock trades via SEC Form 4.")
    ap.add_argument("ticker", nargs="?", help="Ticker symbol. Omit to open the browser UI.")
    ap.add_argument("--days", type=int, default=90, help="Look-back window in days (default 90)")
    ap.add_argument("--directors-only", action="store_true", help="Only board members")
    ap.add_argument("--open-market", action="store_true", help="Only open-market buys (P) and sales (S)")
    ap.add_argument("--derivative", action="store_true", help="Also show options/derivative transactions")
    ap.add_argument("--json", action="store_true", help="Print raw JSON")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    if not a.ticker:
        if os.environ.get("PORT"):  # running on a host such as Render
            return serve(int(os.environ["PORT"]), host="0.0.0.0", open_browser=False)
        return serve(a.port)

    get_user_agent()
    prog = lambda n, t: print(f"\r  reading filing {n}/{t}", end="", file=sys.stderr, flush=True)
    res = get_insider_trades(a.ticker, a.days, include_directors=True, include_others=not a.directors_only,
                             open_market_only=a.open_market, include_derivative=a.derivative, progress=prog)
    print(file=sys.stderr)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print_report(res)


if __name__ == "__main__":
    main()
