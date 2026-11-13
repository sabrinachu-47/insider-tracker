#!/usr/bin/env python3
"""
Insider Tracker - see what a company's directors and officers have been
buying and selling, straight from SEC Form 4 filings (free, no API key).

Usage
-----
  python insider_tracker.py                 # opens a search page in your browser
  python insider_tracker.py AAPL            # prints a table in the terminal
  python insider_tracker.py NVDA --days 180 --directors-only --open-market

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
from datetime import date, datetime, timedelta
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

def get_insider_trades(query, days=90, directors_only=False, open_market_only=False,
                       include_derivative=False, max_filings=150, progress=None):
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

    rows = []
    for n, f in enumerate(filings, 1):
        if progress:
            progress(n, len(filings))
        acc = f["accession"].replace("-", "")
        doc = f["doc"].split("/")[-1]  # strip the xsl rendering prefix -> raw XML
        url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc)
        try:
            owners, txs = parse_form4(sec_get(url))
        except Exception as e:  # skip a broken filing, keep going
            print(f"  ! skipped {url}: {e}", file=sys.stderr)
            continue
        if directors_only and not any(o["is_director"] for o in owners):
            continue
        name = " / ".join(o["name"] for o in owners)
        roles = sorted({r for o in owners for r in o["roles"]})
        for tx in txs:
            if tx["derivative"] and not include_derivative:
                continue
            if open_market_only and tx["code"] not in ("P", "S"):
                continue
            if tx["date"] and date.fromisoformat(tx["date"][:10]) < cutoff:
                continue
            accepted = f["accepted"]
            rows.append({
                **tx,
                "insider": name,
                "roles": ", ".join(roles),
                "filed": f["filed"],
                "filed_time": (accepted[11:16] + " ET") if len(accepted) >= 16 else "",
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
    hdr = f"{'Trade date':<11} {'Filed (ET)':<20} {'Insider':<26} {'Role':<22} {'Type':<26} {'Shares':>12} {'Price':>10} {'Value':>12}"
    print(hdr)
    print("-" * len(hdr))
    for r in res["rows"]:
        sign = "-" if r["acq_disp"] == "D" else "+"
        shares = f"{sign}{r['shares']:,.0f}" if r["shares"] is not None else "-"
        price = f"${r['price']:,.2f}" if r["price"] else "-"
        typ = r["type"] + (" (10b5-1)" if r["plan_10b5_1"] else "")
        print(f"{(r['date'] or '')[:10]:<11} {(r['filed'] + ' ' + r['filed_time']).strip():<20} "
              f"{r['insider'][:26]:<26} {r['roles'][:22]:<22} {typ[:26]:<26} "
              f"{shares:>12} {price:>10} {fmt_money(r['value']):>12}")


# --------------------------------------------------------------------------
# Browser UI
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insider Tracker</title>
<meta charset="utf-8">
<style>
  :root { --bg:#f6f6f3; --card:#fff; --ink:#1c1c1e; --muted:#6b6b70; --line:#e3e3de;
          --buy:#0f7b4f; --buy-bg:#e5f3eb; --sell:#b42318; --sell-bg:#fbe9e7; --accent:#2f4fd8; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:var(--bg); color:var(--ink); }
  .wrap { max-width:1180px; margin:0 auto; padding:28px 24px 48px; }
  h1 { font-size:24px; margin:0 0 4px; letter-spacing:-.01em; }
  .sub { color:var(--muted); }
  form { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:20px 0 8px; }
  input[type=text] { font-size:16px; padding:11px 14px; border:1px solid var(--line); border-radius:9px; width:200px; text-transform:uppercase; background:#fff; }
  select { font-size:14px; padding:10px 12px; border-radius:9px; border:1px solid var(--line); background:#fff; }
  button { font-size:15px; padding:11px 22px; border-radius:9px; border:none; background:var(--accent); color:#fff; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:wait; }
  label.chk { display:flex; gap:6px; align-items:center; color:var(--muted); cursor:pointer; }
  .status { color:var(--muted); padding:10px 0; }
  .err { color:var(--sell); }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid #ccc; border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; vertical-align:-2px; margin-right:8px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .title { font-size:20px; font-weight:650; margin:14px 0 2px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin:16px 0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .card .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
  .card .v { font-size:24px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }
  .card .s { color:var(--muted); font-size:13px; font-variant-numeric:tabular-nums; }
  .card.buy .v { color:var(--buy); } .card.sell .v { color:var(--sell); }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); vertical-align:top; }
  th { font-size:11.5px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.04em; background:#fafaf7; cursor:pointer; user-select:none; white-space:nowrap; }
  td { white-space:nowrap; }
  td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
  tr:last-child td { border-bottom:none; }
  .pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600; background:#efefeb; color:#444; }
  .pill.P { background:var(--buy-bg); color:var(--buy); }
  .pill.S { background:var(--sell-bg); color:var(--sell); }
  .tag { font-size:11px; color:var(--muted); margin-left:4px; }
  .role { color:var(--muted); font-size:12.5px; white-space:normal; }
  a { color:var(--accent); text-decoration:none; }
  .foot { color:var(--muted); font-size:12px; margin-top:14px; line-height:1.6; }
  /* phones: each trade becomes a small card */
  @media (max-width:700px) {
    .wrap { padding:20px 14px 40px; }
    input[type=text] { width:100%; }
    form > * { flex:1 1 auto; }
    table, tbody, tr, td { display:block; width:100%; border:none; }
    thead { display:none; }
    table { background:none; }
    tr { background:var(--card); border:1px solid var(--line); border-radius:12px; margin-bottom:10px; padding:6px 4px; }
    td { display:flex; justify-content:space-between; gap:12px; padding:5px 12px; border:none; white-space:normal; text-align:right; }
    td::before { content:attr(data-l); color:var(--muted); font-size:12px; text-align:left; }
    td.n { text-align:right; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Insider Tracker</h1>
  <div class="sub">Recent stock buys and sales by a company's directors and executives, from SEC Form 4 filings.</div>

  <form id="f">
    <input id="q" type="text" placeholder="Ticker, e.g. AAPL" autocomplete="off" autofocus required>
    <select id="days">
      <option value="30">Last 30 days</option>
      <option value="90" selected>Last 90 days</option>
      <option value="180">Last 6 months</option>
      <option value="365">Last 12 months</option>
    </select>
    <label class="chk"><input type="checkbox" id="dir"> Board directors only</label>
    <label class="chk"><input type="checkbox" id="om" checked> Real buys/sales only</label>
    <button id="go">Search</button>
  </form>

  <div id="status" class="status"></div>
  <div id="out"></div>
</div>

<script>
var $ = function (id) { return document.getElementById(id); };
function money(v) {
  if (v == null) return '–';
  var a = Math.abs(v);
  if (a >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
  return '$' + Math.round(v).toLocaleString();
}
function num(v) { return v == null ? '–' : Math.round(v).toLocaleString(); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

var rows = [], sortKey = 'date', sortDir = -1;

$('f').onsubmit = function (e) {
  e.preventDefault();
  var q = $('q').value.trim();
  if (!q) return;
  $('go').disabled = true;
  $('out').innerHTML = '';
  $('status').className = 'status';
  $('status').innerHTML = '<span class="spinner"></span>Reading SEC filings… big companies can take up to 30 seconds (up to a minute if the site was asleep).';
  var p = new URLSearchParams({ q: q, days: $('days').value, dir: $('dir').checked ? 1 : 0, om: $('om').checked ? 1 : 0 });
  fetch('api?' + p).then(function (r) { return r.json(); }).then(function (d) {
    if (d.error) throw new Error(d.error);
    $('status').textContent = ''; $('go').disabled = false; render(d);
  }).catch(function (err) {
    $('status').className = 'status err'; $('status').textContent = err.message || String(err); $('go').disabled = false;
  });
};

function render(d) {
  rows = d.rows;
  var s = d.summary, net = s.buy_value - s.sell_value;
  $('out').innerHTML =
    '<div class="title">' + esc(d.company) + ' (' + esc(d.ticker) + ')</div>' +
    '<div class="sub">' + d.filings_checked + ' insider filings in the last ' + d.days + ' days · ' +
      '<a target="_blank" href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=' + d.cik + '&type=4&owner=include&count=40">view on SEC website</a></div>' +
    '<div class="cards">' +
      '<div class="card buy"><div class="k">Bought (open market)</div><div class="v">' + money(s.buy_value) + '</div>' +
        '<div class="s">' + s.buy_count + ' trades · ' + num(s.buy_shares) + ' shares</div></div>' +
      '<div class="card sell"><div class="k">Sold (open market)</div><div class="v">' + money(s.sell_value) + '</div>' +
        '<div class="s">' + s.sell_count + ' trades · ' + num(s.sell_shares) + ' shares</div></div>' +
      '<div class="card ' + (net >= 0 ? 'buy' : 'sell') + '"><div class="k">Net</div><div class="v">' + (net < 0 ? '−' : '') + money(Math.abs(net)) + '</div>' +
        '<div class="s">' + (net > 0 ? 'More buying than selling' : net < 0 ? 'More selling than buying' : 'No open-market trades') + '</div></div>' +
    '</div>' +
    '<div id="tbl"></div>' +
    '<div class="foot">Value = shares × price reported in the filing. ' +
      '<b>10b5-1</b> means the trade was scheduled in advance under a trading plan, so it says less about the insider\'s view of the stock. ' +
      'Untick "Real buys/sales only" to also see stock awards, option exercises and shares withheld for taxes. Not investment advice.</div>';
  table();
}

function table() {
  if (!rows.length) { $('tbl').innerHTML = '<div class="status">No matching trades in this period.</div>'; return; }
  var cols = [['date','Trade date'],['filed','Reported'],['insider','Who'],['type','Transaction'],
              ['shares','Shares','n'],['price','Price','n'],['value','Value','n'],['owned_after','Shares held after','n'],['','Filing']];
  var sorted = rows.slice().sort(function (a, b) {
    var x = a[sortKey], y = b[sortKey];
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
  });
  var h = '<table><thead><tr>' + cols.map(function (c) {
    return '<th class="' + (c[2] || '') + '" data-k="' + c[0] + '">' + c[1] + (c[0] === sortKey ? (sortDir < 0 ? ' ↓' : ' ↑') : '') + '</th>';
  }).join('') + '</tr></thead><tbody>';
  sorted.forEach(function (r) {
    h += '<tr>' +
      '<td data-l="Trade date">' + esc(r.date) + '</td>' +
      '<td data-l="Reported">' + esc(r.filed) + ' <span class="tag">' + esc(r.filed_time) + '</span></td>' +
      '<td data-l="Who"><div>' + esc(r.insider) + '<div class="role">' + esc(r.roles) + '</div></div></td>' +
      '<td data-l="Transaction"><span><span class="pill ' + esc(r.code) + '">' + esc(r.type) + '</span>' + (r.plan_10b5_1 ? '<span class="tag">10b5-1</span>' : '') + '</span></td>' +
      '<td class="n" data-l="Shares">' + (r.acq_disp === 'D' ? '−' : '+') + num(r.shares) + '</td>' +
      '<td class="n" data-l="Price">' + (r.price ? '$' + r.price.toFixed(2) : '–') + '</td>' +
      '<td class="n" data-l="Value">' + money(r.value) + '</td>' +
      '<td class="n" data-l="Shares held after">' + num(r.owned_after) + '</td>' +
      '<td data-l="Filing"><a target="_blank" href="' + esc(r.filing_url) + '">Form ' + esc(r.form) + '</a></td>' +
    '</tr>';
  });
  $('tbl').innerHTML = h + '</tbody></table>';
  Array.prototype.forEach.call(document.querySelectorAll('th[data-k]'), function (th) {
    th.onclick = function () {
      var k = th.getAttribute('data-k'); if (!k) return;
      sortDir = (k === sortKey) ? -sortDir : -1; sortKey = k; table();
    };
  });
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/api":
            p = urllib.parse.parse_qs(url.query)
            try:
                res = get_insider_trades(
                    p.get("q", [""])[0],
                    days=max(1, min(365, int(p.get("days", ["90"])[0]))),
                    directors_only=p.get("dir", ["0"])[0] == "1",
                    open_market_only=p.get("om", ["0"])[0] == "1",
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
    res = get_insider_trades(a.ticker, a.days, a.directors_only, a.open_market, a.derivative, progress=prog)
    print(file=sys.stderr)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print_report(res)


if __name__ == "__main__":
    main()
