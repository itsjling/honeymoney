from __future__ import annotations

import html
import json
from decimal import Decimal, InvalidOperation


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Honeymoney Report</title>
<style>
  :root {
    color-scheme: light;
    --bg: #f4f5f6;
    --surface: #fbfbfc;
    --surface-hover: #eef0f2;
    --ink: #1f2328;
    --ink-muted: #5c636b;
    --ink-faint: #8b929a;
    --line: #e4e7ea;
    --line-strong: #d2d7dc;
    --pos: #2f7d55;
    --neg: #b24a3f;
    --focus: rgba(31, 35, 40, 0.35);
    --shadow: 0 1px 2px rgba(24, 28, 33, 0.05), 0 8px 24px rgba(24, 28, 33, 0.05);
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #14161a;
      --surface: #1b1e23;
      --surface-hover: #23272d;
      --ink: #e8eaed;
      --ink-muted: #a2a9b2;
      --ink-faint: #6c737c;
      --line: #282c32;
      --line-strong: #3a3f47;
      --pos: #6cc08c;
      --neg: #e0897c;
      --focus: rgba(232, 234, 237, 0.4);
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 30px rgba(0, 0, 0, 0.35);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #14161a;
    --surface: #1b1e23;
    --surface-hover: #23272d;
    --ink: #e8eaed;
    --ink-muted: #a2a9b2;
    --ink-faint: #6c737c;
    --line: #282c32;
    --line-strong: #3a3f47;
    --pos: #6cc08c;
    --neg: #e0897c;
    --focus: rgba(232, 234, 237, 0.4);
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 30px rgba(0, 0, 0, 0.35);
  }

  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    padding: clamp(1.5rem, 4vw, 3.5rem) clamp(1rem, 4vw, 2rem) 5rem;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  main { max-width: 64rem; margin: 0 auto; }
  .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }

  header.report-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding-bottom: 1.25rem;
    border-bottom: 1px solid var(--line-strong);
    margin-bottom: 2rem;
  }
  .report-head h1 {
    font-size: clamp(1.5rem, 3vw, 2rem);
    letter-spacing: -0.02em;
    font-weight: 640;
    margin: 0 0 0.35rem;
  }
  .report-head .meta { color: var(--ink-muted); font-size: 0.9rem; }
  .report-head .meta .count { color: var(--ink-faint); }

  #theme-toggle {
    flex: none;
    font: inherit;
    font-size: 0.78rem;
    letter-spacing: 0.01em;
    color: var(--ink-muted);
    background: var(--surface);
    border: 1px solid var(--line-strong);
    border-radius: 8px;
    padding: 0.4rem 0.7rem;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease, transform 0.1s ease;
    min-width: 5.2rem;
  }
  #theme-toggle:hover { background: var(--surface-hover); color: var(--ink); }
  #theme-toggle:active { transform: translateY(1px); }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 6px; }

  .stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--surface);
    box-shadow: var(--shadow);
    overflow: hidden;
    margin-bottom: 2.25rem;
  }
  .stat {
    padding: 1.1rem 1.25rem 1.2rem;
    border-left: 1px solid var(--line);
  }
  .stat:first-child { border-left: 0; }
  .stat .label {
    font-size: 0.72rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-faint);
    margin-bottom: 0.5rem;
  }
  .stat .value { font-size: clamp(1.15rem, 2.4vw, 1.6rem); letter-spacing: -0.01em; }
  .stat .value.pos { color: var(--pos); }
  .stat .value.neg { color: var(--neg); }

  .panel {
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--surface);
    box-shadow: var(--shadow);
    margin-bottom: 2.25rem;
  }
  .panel > .panel-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.15rem 1.4rem;
    border-bottom: 1px solid var(--line);
  }
  .panel-head h2 { font-size: 1.02rem; font-weight: 620; letter-spacing: -0.01em; margin: 0; }
  .panel-head .hint { font-size: 0.82rem; color: var(--ink-faint); }
  .panel-body { padding: 1.4rem; }

  .chart-row {
    display: grid;
    grid-template-columns: minmax(200px, 260px) 1fr;
    gap: clamp(1.25rem, 4vw, 2.5rem);
    align-items: center;
  }
  .donut { position: relative; width: 100%; max-width: 240px; margin: 0 auto; aspect-ratio: 1; }
  .donut svg { display: block; width: 100%; height: 100%; }
  .donut svg path { transition: opacity 0.15s ease; }
  .donut svg:hover path { opacity: 0.45; }
  .donut svg path:hover { opacity: 1; }
  .donut .hole { fill: var(--surface); pointer-events: none; }
  .donut-center {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    text-align: center;
  }
  .donut-center .big { font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 1.5rem; letter-spacing: -0.02em; }
  .donut-center .small { font-size: 0.68rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-faint); margin-top: 0.15rem; }

  table.legend { width: 100%; border-collapse: collapse; }
  table.legend th, table.legend td { padding: 0.5rem 0.25rem; text-align: left; border-bottom: 1px solid var(--line); }
  table.legend th {
    font-size: 0.7rem; letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--ink-faint); font-weight: 600;
  }
  table.legend td.amt, table.legend th.amt { text-align: right; }
  table.legend tr:last-child td { border-bottom: 0; }
  table.legend .cat { display: flex; align-items: center; gap: 0.55rem; min-width: 0; }
  table.legend .cat span.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .swatch { flex: none; width: 0.7rem; height: 0.7rem; border-radius: 3px; }
  .legend .amt { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .legend .amt.pos { color: var(--pos); }
  .legend .amt.neg { color: var(--neg); }
  .legend .share { color: var(--ink-muted); }

  .switch { display: inline-flex; align-items: center; gap: 0.6rem; cursor: pointer; font-size: 0.85rem; color: var(--ink-muted); }
  .switch input { position: absolute; opacity: 0; pointer-events: none; }
  .switch .track {
    width: 34px; height: 20px; border-radius: 999px;
    background: var(--line-strong); position: relative; transition: background 0.18s ease; flex: none;
  }
  .switch .track::after {
    content: ""; position: absolute; top: 2px; left: 2px;
    width: 16px; height: 16px; border-radius: 50%;
    background: var(--surface); box-shadow: 0 1px 2px rgba(0,0,0,0.25);
    transition: transform 0.18s ease;
  }
  .switch input:checked + .track { background: var(--ink); }
  .switch input:checked + .track::after { transform: translateX(14px); }
  .switch input:focus-visible + .track { outline: 2px solid var(--focus); outline-offset: 2px; }

  .table-wrap { overflow-x: auto; }
  table.txns { width: 100%; border-collapse: collapse; table-layout: fixed; min-width: 720px; }
  table.txns col.c-date { width: 132px; }
  table.txns col.c-merchant { width: 248px; }
  table.txns col.c-category { width: 158px; }
  table.txns col.c-amount { width: 134px; }
  table.txns col.c-account { width: 128px; }
  table.txns col.c-owner { width: 92px; }
  table.txns th, table.txns td {
    padding: 0.55rem 0.75rem;
    border-bottom: 1px solid var(--line);
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  table.txns th {
    font-size: 0.7rem; letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--ink-faint); font-weight: 600; position: sticky; top: 0;
    background: var(--surface);
  }
  table.txns th.amt, table.txns td.amt { text-align: right; }
  table.txns tbody tr { transition: background 0.12s ease; }
  table.txns tbody tr:hover { background: var(--surface-hover); }
  table.txns td.date { font-family: var(--mono); color: var(--ink-muted); font-size: 0.86rem; }
  table.txns td.merchant { font-weight: 520; }
  table.txns td.account, table.txns td.owner { color: var(--ink-faint); font-size: 0.86rem; }
  table.txns td.amt { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  table.txns td.amt.pos { color: var(--pos); }
  table.txns td.amt.neg { color: var(--neg); }
  table.txns td.amt.na { color: var(--ink-faint); }
  .cat-cell { display: flex; align-items: center; gap: 0.5rem; min-width: 0; }
  .cat-cell span.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cat-cell .review { flex: none; font-size: 0.68rem; letter-spacing: 0.04em; color: var(--neg); }

  .empty { color: var(--ink-faint); font-style: italic; padding: 1rem 0.75rem; }

  @media (max-width: 860px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .stat:nth-child(3) { border-left: 0; }
    .stat:nth-child(n+3) { border-top: 1px solid var(--line); }
  }
  @media (max-width: 720px) {
    .chart-row { grid-template-columns: 1fr; }
    table.txns col.c-account, table.txns col.c-owner { width: 0; }
    table.txns .col-account, table.txns .col-owner { display: none; }
    table.txns { min-width: 600px; }
  }

  .rise { opacity: 1; }
  @media (prefers-reduced-motion: no-preference) {
    .rise { animation: rise 0.5s cubic-bezier(0.16, 1, 0.3, 1) both; }
    .rise.d1 { animation-delay: 0.05s; }
    .rise.d2 { animation-delay: 0.12s; }
    .rise.d3 { animation-delay: 0.19s; }
    @keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
  }
</style>
</head>
<body>
<main>
  <header class="report-head rise">
    <div>
      <h1>Honeymoney Report</h1>
      <div class="meta">__PERIOD__ <span class="count">&middot; <span id="txn-count"></span> transactions</span></div>
    </div>
    <button id="theme-toggle" type="button" aria-label="Switch color theme">Auto</button>
  </header>

  <section class="stats rise d1" aria-label="Summary">
    <div class="stat"><div class="label">Spending</div><div class="value neg num" id="tile-spending"></div></div>
    <div class="stat"><div class="label">Income</div><div class="value pos num" id="tile-income"></div></div>
    <div class="stat"><div class="label">Net</div><div class="value num" id="tile-net"></div></div>
    <div class="stat"><div class="label">Uncategorized</div><div class="value num" id="tile-uncategorized"></div></div>
  </section>

  <section class="panel rise d2">
    <div class="panel-head">
      <h2>Category distribution</h2>
      <label class="switch">
        <input type="checkbox" id="exclude-transfers" checked>
        <span class="track"></span>
        <span>Exclude card payments and transfers</span>
      </label>
    </div>
    <div class="panel-body">
      <div class="chart-row">
        <div class="donut">
          <svg id="pie" viewBox="-1 -1 2 2" role="img" aria-label="Category distribution by amount"></svg>
          <div class="donut-center">
            <div class="big" id="donut-total"></div>
            <div class="small">in view</div>
          </div>
        </div>
        <table class="legend" id="legend"></table>
      </div>
    </div>
  </section>

  <section class="panel rise d3">
    <div class="panel-head"><h2>Transactions</h2><span class="hint">Newest first</span></div>
    <div class="panel-body">
      <div class="table-wrap">
        <table class="txns" id="transactions">
          <colgroup>
            <col class="c-date"><col class="c-merchant"><col class="c-category">
            <col class="c-amount"><col class="c-account col-account"><col class="c-owner col-owner">
          </colgroup>
        </table>
      </div>
    </div>
  </section>
</main>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var rows = JSON.parse(document.getElementById("data").textContent);
  var TRANSFER = { "Credit Card Payment": true, "Internal Transfer": true };
  var PALETTE = [
    "#4c9a8f", "#d98b3f", "#b7554b", "#7c9a5a", "#5a7fa6", "#9a6f9c",
    "#c9a24b", "#8a8f4f", "#b06a8a", "#5f8a6b", "#a1725a", "#6b7f8f"
  ];

  var root = document.documentElement;
  var toggleBtn = document.getElementById("theme-toggle");
  var MODES = ["auto", "light", "dark"];
  function applyMode(mode) {
    if (mode === "auto") { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", mode); }
    toggleBtn.textContent = mode.charAt(0).toUpperCase() + mode.slice(1);
    toggleBtn.setAttribute("data-mode", mode);
  }
  var stored = null;
  try { stored = localStorage.getItem("hm-theme"); } catch (e) {}
  applyMode(MODES.indexOf(stored) >= 0 ? stored : "auto");
  toggleBtn.addEventListener("click", function () {
    var current = toggleBtn.getAttribute("data-mode") || "auto";
    var next = MODES[(MODES.indexOf(current) + 1) % MODES.length];
    try { localStorage.setItem("hm-theme", next); } catch (e) {}
    applyMode(next);
  });

  function fmt(v) {
    if (v === null || v === undefined) { return "n/a"; }
    return v.toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function signClass(v) { return v !== null && v < 0 ? "neg" : "pos"; }

  var catMagnitude = {};
  rows.forEach(function (row) {
    if (row.amount === null) { return; }
    var category = row.category || "Unknown";
    catMagnitude[category] = (catMagnitude[category] || 0) + Math.abs(row.amount);
  });
  var COLOR = {};
  Object.keys(catMagnitude)
    .sort(function (a, b) { return catMagnitude[b] - catMagnitude[a]; })
    .forEach(function (category, index) { COLOR[category] = PALETTE[index % PALETTE.length]; });
  function colorFor(category) { return COLOR[category] || "var(--ink-faint)"; }

  function renderTiles() {
    var spending = 0, income = 0, uncategorized = 0;
    rows.forEach(function (row) {
      if (row.category === "" || row.category === "Unknown") { uncategorized += 1; }
      if (row.amount === null || TRANSFER[row.category]) { return; }
      if (row.amount < 0) { spending += row.amount; } else { income += row.amount; }
    });
    document.getElementById("txn-count").textContent = rows.length;
    document.getElementById("tile-spending").textContent = fmt(spending);
    document.getElementById("tile-income").textContent = fmt(income);
    var net = spending + income;
    var netEl = document.getElementById("tile-net");
    netEl.textContent = fmt(net);
    netEl.classList.toggle("pos", net >= 0);
    netEl.classList.toggle("neg", net < 0);
    document.getElementById("tile-uncategorized").textContent = uncategorized;
  }

  function chartData(excludeTransfers) {
    var totals = {}, counts = {}, chartedCount = 0;
    rows.forEach(function (row) {
      if (row.amount === null) { return; }
      if (excludeTransfers && TRANSFER[row.category]) { return; }
      var category = row.category || "Unknown";
      totals[category] = (totals[category] || 0) + row.amount;
      counts[category] = (counts[category] || 0) + 1;
      chartedCount += 1;
    });
    var entries = Object.keys(totals)
      .map(function (category) {
        return { category: category, sum: totals[category], count: counts[category] };
      })
      .filter(function (entry) { return Math.abs(entry.sum) > 0.005; })
      .sort(function (a, b) { return Math.abs(b.sum) - Math.abs(a.sum); });
    return { entries: entries, chartedCount: chartedCount };
  }

  function renderPie(entries) {
    var svg = document.getElementById("pie");
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }
    var total = entries.reduce(function (acc, e) { return acc + Math.abs(e.sum); }, 0);
    var ns = "http://www.w3.org/2000/svg";
    if (total) {
      if (entries.length === 1) {
        var circle = document.createElementNS(ns, "circle");
        circle.setAttribute("r", "1");
        circle.setAttribute("fill", colorFor(entries[0].category));
        var t1 = document.createElementNS(ns, "title");
        t1.textContent = entries[0].category + ": " + fmt(entries[0].sum) + " (100%)";
        circle.appendChild(t1);
        svg.appendChild(circle);
      } else {
        var angle = -Math.PI / 2;
        entries.forEach(function (entry) {
          var share = Math.abs(entry.sum) / total;
          var next = angle + share * 2 * Math.PI;
          var large = share > 0.5 ? 1 : 0;
          var path = document.createElementNS(ns, "path");
          path.setAttribute(
            "d",
            "M 0 0 L " + Math.cos(angle).toFixed(5) + " " + Math.sin(angle).toFixed(5) +
            " A 1 1 0 " + large + " 1 " +
            Math.cos(next).toFixed(5) + " " + Math.sin(next).toFixed(5) + " Z"
          );
          path.setAttribute("fill", colorFor(entry.category));
          var title = document.createElementNS(ns, "title");
          title.textContent = entry.category + ": " + fmt(entry.sum) +
            " (" + Math.round(share * 100) + "%)";
          path.appendChild(title);
          svg.appendChild(path);
          angle = next;
        });
      }
    }
    var hole = document.createElementNS(ns, "circle");
    hole.setAttribute("r", "0.6");
    hole.setAttribute("class", "hole");
    svg.appendChild(hole);
  }

  function renderLegend(entries) {
    var legend = document.getElementById("legend");
    legend.innerHTML = "";
    if (!entries.length) {
      legend.innerHTML = '<tr><td class="empty">No amounts to chart in this view.</td></tr>';
      return;
    }
    var total = entries.reduce(function (acc, e) { return acc + Math.abs(e.sum); }, 0);
    var head = legend.insertRow();
    [["Category", ""], ["Sum (HKD)", "amt"], ["Share", "amt"]].forEach(function (col) {
      var th = document.createElement("th");
      th.textContent = col[0];
      if (col[1]) { th.className = col[1]; }
      head.appendChild(th);
    });
    entries.forEach(function (entry) {
      var tr = legend.insertRow();
      var nameCell = tr.insertCell();
      var wrap = document.createElement("div");
      wrap.className = "cat";
      var sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = colorFor(entry.category);
      var nm = document.createElement("span");
      nm.className = "name";
      nm.textContent = entry.category;
      nm.title = entry.category;
      wrap.appendChild(sw);
      wrap.appendChild(nm);
      nameCell.appendChild(wrap);
      var sumCell = tr.insertCell();
      sumCell.className = "amt " + signClass(entry.sum);
      sumCell.textContent = fmt(entry.sum);
      var shareCell = tr.insertCell();
      shareCell.className = "amt share";
      shareCell.textContent = Math.round((Math.abs(entry.sum) / total) * 100) + "%";
    });
  }

  function renderChart() {
    var excludeTransfers = document.getElementById("exclude-transfers").checked;
    var data = chartData(excludeTransfers);
    renderPie(data.entries);
    renderLegend(data.entries);
    document.getElementById("donut-total").textContent = data.chartedCount;
  }

  function renderTransactions() {
    var table = document.getElementById("transactions");
    var old = table.querySelector("thead, tbody");
    while (table.querySelector("thead, tbody")) { table.querySelector("thead, tbody").remove(); }
    var thead = table.createTHead();
    var headRow = thead.insertRow();
    [
      ["Date", ""], ["Merchant", ""], ["Category", ""],
      ["Amount (HKD)", "amt"], ["Account", "col-account"], ["Owner", "col-owner"]
    ].forEach(function (col) {
      var th = document.createElement("th");
      th.textContent = col[0];
      if (col[1]) { th.className = col[1]; }
      headRow.appendChild(th);
    });
    var tbody = table.createTBody();
    if (!rows.length) {
      var er = tbody.insertRow();
      var ec = er.insertCell();
      ec.colSpan = 6;
      ec.className = "empty";
      ec.textContent = "No transactions recorded in this view.";
      return;
    }
    rows.slice()
      .sort(function (a, b) { return a.date < b.date ? 1 : a.date > b.date ? -1 : 0; })
      .forEach(function (row) {
        var tr = tbody.insertRow();
        cell(tr, "date", row.date);
        cell(tr, "merchant", row.merchant, row.merchant);

        var catCell = tr.insertCell();
        var wrap = document.createElement("div");
        wrap.className = "cat-cell";
        var sw = document.createElement("span");
        sw.className = "swatch";
        sw.style.background = colorFor(row.category || "Unknown");
        var nm = document.createElement("span");
        nm.className = "name";
        nm.textContent = row.category;
        nm.title = row.category;
        wrap.appendChild(sw);
        wrap.appendChild(nm);
        if (row.needs_review) {
          var rv = document.createElement("span");
          rv.className = "review";
          rv.textContent = "review";
          wrap.appendChild(rv);
        }
        catCell.appendChild(wrap);

        var amtCell = tr.insertCell();
        amtCell.className = "amt " + (row.amount === null ? "na" : signClass(row.amount));
        amtCell.textContent = fmt(row.amount);

        cell(tr, "account col-account", row.account, row.account);
        cell(tr, "owner col-owner", row.owner, row.owner);
      });
  }

  function cell(tr, className, text, title) {
    var td = tr.insertCell();
    td.className = className;
    td.textContent = text || "";
    if (title) { td.title = title; }
    return td;
  }

  document.getElementById("exclude-transfers").addEventListener("change", renderChart);
  renderTiles();
  renderChart();
  renderTransactions();
})();
</script>
</body>
</html>
"""


def build_report_html(rows: list[dict[str, str]], period_label: str) -> str:
    data = json.dumps(
        [_report_row(row) for row in rows], ensure_ascii=True, sort_keys=True
    ).replace("</", "<\\/")
    return _PAGE_TEMPLATE.replace("__PERIOD__", html.escape(period_label)).replace(
        "__DATA__", data
    )


def _report_row(row: dict[str, str]) -> dict[str, object]:
    return {
        "date": row.get("date", ""),
        "merchant": row.get("merchant", ""),
        "category": row.get("category", ""),
        "amount": _amount_value(row.get("amount_hkd", "")),
        "account": row.get("account", ""),
        "owner": row.get("owner", ""),
        "needs_review": row.get("needs_review") == "true",
    }


def _amount_value(value: str) -> float | None:
    try:
        return float(Decimal(value))
    except (InvalidOperation, ValueError):
        return None
