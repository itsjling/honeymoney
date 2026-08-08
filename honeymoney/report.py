from __future__ import annotations

import html
import json
from decimal import Decimal, InvalidOperation

from honeymoney.contracts import BalanceReconciliation
from honeymoney.valuation import valuation_summary

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

  .owner-controls {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.85rem 1rem;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface);
    box-shadow: var(--shadow);
    margin-bottom: 1.25rem;
  }
  .owner-controls .owner-heading { flex: none; }
  .owner-controls h2 { font-size: 0.78rem; margin: 0; }
  .owner-controls .hint { color: var(--ink-faint); font-size: 0.75rem; }
  .owner-filter { display: flex; flex-wrap: wrap; gap: 0.45rem 0.9rem; flex: 1; }
  .owner-option { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; }
  .owner-option input { accent-color: var(--ink); }
  .owner-actions { display: flex; gap: 0.45rem; }
  .owner-actions button {
    font: inherit;
    font-size: 0.75rem;
    color: var(--ink-muted);
    background: var(--surface);
    border: 1px solid var(--line-strong);
    border-radius: 7px;
    padding: 0.35rem 0.6rem;
    cursor: pointer;
  }
  .owner-actions button:hover { background: var(--surface-hover); color: var(--ink); }

  .stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
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
  .stat:first-child, .stat:nth-child(4) { border-left: 0; }
  .stat:nth-child(n+4) { border-top: 1px solid var(--line); }
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
  .cat-cell .provenance { flex: none; font-size: 0.68rem; letter-spacing: 0.04em; color: var(--ink-muted); }

  .empty { color: var(--ink-faint); font-style: italic; padding: 1rem 0.75rem; }
  .warning {
    border: 1px solid var(--line-strong);
    border-radius: 10px;
    background: var(--surface);
    color: var(--ink-muted);
    padding: 0.85rem 1rem;
    margin: -1rem 0 2rem;
  }
  .valuation-table { width: 100%; border-collapse: collapse; }
  .valuation-table th, .valuation-table td {
    padding: 0.65rem 0.5rem;
    border-bottom: 1px solid var(--line);
  }
  .valuation-table th { color: var(--ink-faint); font-weight: 600; text-align: right; }
  .valuation-table th:first-child, .valuation-table td:first-child { text-align: left; }
  .valuation-table td { text-align: right; }
  .valuation-table tr:last-child td { border-bottom: 0; font-weight: 620; }
  .coverage-table { width: 100%; border-collapse: collapse; min-width: 680px; }
  .coverage-table th, .coverage-table td {
    padding: 0.65rem 0.5rem;
    border-bottom: 1px solid var(--line);
    text-align: left;
  }
  .coverage-table th {
    color: var(--ink-faint);
    font-size: 0.72rem;
    font-weight: 600;
  }
  .coverage-table tr:last-child td { border-bottom: 0; }
  .coverage-table .outcome { font-family: var(--mono); font-size: 0.82rem; }
  .valuation-note { color: var(--ink-muted); font-size: 0.82rem; margin: 0.8rem 0 0; }
  .completeness-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1rem;
  }
  .completeness-item .label {
    color: var(--ink-faint);
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .completeness-item .value { font-size: 1.25rem; margin-top: 0.25rem; }

  @media (max-width: 860px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .stat:nth-child(3) { border-left: 0; }
    .stat:nth-child(n+3) { border-top: 1px solid var(--line); }
    .completeness-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 720px) {
    .owner-controls { align-items: flex-start; flex-direction: column; }
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
      <div class="meta">__PERIOD__ <span class="count">&middot; <span id="source-count">__SOURCE_COUNT__ source occurrences</span> &middot; <span id="txn-count">__CANONICAL_COUNT__</span> canonical transactions</span></div>
    </div>
    <button id="theme-toggle" type="button" aria-label="Switch color theme">Auto</button>
  </header>

  <section class="owner-controls rise d1" aria-label="Owner view">
    <div class="owner-heading">
      <h2>Owners</h2>
      <div class="hint" id="owner-view-label">Combined view</div>
    </div>
    <div class="owner-filter" id="owner-filter"></div>
    <div class="owner-actions">
      <button id="owner-select-all" type="button">Combined</button>
      <button id="owner-clear" type="button">Clear</button>
    </div>
  </section>

  <section class="stats rise d1" aria-label="Summary">
    <div class="stat"><div class="label">Spending, net of refunds · combined estimate</div><div class="value neg num" id="tile-spending">__SPENDING__</div></div>
    <div class="stat"><div class="label">Income · combined estimate</div><div class="value pos num" id="tile-income">__INCOME__</div></div>
    <div class="stat"><div class="label">Net cash flow · combined estimate</div><div class="value num" id="tile-net">__NET__</div></div>
    <div class="stat"><div class="label">Unresolved inflow</div><div class="value pos num" id="tile-unresolved-inflow">__UNRESOLVED_INFLOW__</div></div>
    <div class="stat"><div class="label">Unresolved outflow</div><div class="value neg num" id="tile-unresolved-outflow">__UNRESOLVED_OUTFLOW__</div></div>
    <div class="stat"><div class="label">Uncategorized</div><div class="value num" id="tile-uncategorized">__UNCATEGORIZED__</div></div>
  </section>

  __COMPLETENESS_WARNING__

  <section class="panel rise d2" aria-label="Valuation completeness">
    <div class="panel-head"><h2>Valuation completeness</h2><span class="hint">Canonical transactions in this period</span></div>
    <div class="panel-body completeness-grid">
      <div class="completeness-item" id="missing-total"><div class="label">Total missing</div><div class="value num">__MISSING_TOTAL__</div></div>
      <div class="completeness-item" id="missing-blocking"><div class="label">Cash-flow blockers</div><div class="value num">__MISSING_BLOCKING__</div></div>
      <div class="completeness-item" id="missing-excluded"><div class="label">Excluded flows</div><div class="value num">__MISSING_EXCLUDED__</div></div>
      <div class="completeness-item" id="missing-unresolved"><div class="label">Unresolved flows</div><div class="value num">__MISSING_UNRESOLVED__</div></div>
      <div class="completeness-item" id="missing-zero"><div class="label">Zero cash-flow rows</div><div class="value num">__MISSING_ZERO__</div></div>
      <div class="completeness-item" id="missing-other"><div class="label">Other flows</div><div class="value num">__MISSING_OTHER__</div></div>
    </div>
  </section>

  <section class="panel rise d2" aria-label="Cash-flow valuation">
    <div class="panel-head"><h2>Cash-flow valuation</h2><span class="hint">HKD</span></div>
    <div class="panel-body">
      <table class="valuation-table">
        <thead><tr><th>Flow</th><th>Actual</th><th>Estimated</th><th>Combined estimate</th></tr></thead>
        <tbody>
          <tr id="cash-flow-income"><td>Income</td><td class="num">__ACTUAL_INCOME__</td><td class="num">__ESTIMATED_INCOME__</td><td class="num">__COMBINED_INCOME__</td></tr>
          <tr id="cash-flow-spending"><td>Spending</td><td class="num">__ACTUAL_SPENDING__</td><td class="num">__ESTIMATED_SPENDING__</td><td class="num">__COMBINED_SPENDING__</td></tr>
          <tr id="cash-flow-refunds"><td>Refunds</td><td class="num">__ACTUAL_REFUNDS__</td><td class="num">__ESTIMATED_REFUNDS__</td><td class="num">__COMBINED_REFUNDS__</td></tr>
          <tr id="cash-flow-net"><td>Net cash flow</td><td class="num">__ACTUAL_NET__</td><td class="num">__ESTIMATED_NET__</td><td class="num">__COMBINED_NET__</td></tr>
        </tbody>
      </table>
      <p class="valuation-note">Combined estimates include statement and matched actual values plus configured or provider reference estimates. They are not exact bank conversion costs or tax valuations.</p>
    </div>
  </section>

  __BALANCE_COVERAGE__

  <section class="panel rise d2">
    <div class="panel-head">
      <h2>Category distribution</h2>
      <label class="switch">
        <input type="checkbox" id="exclude-transfers" checked>
        <span class="track"></span>
        <span>Show confirmed cash flow only</span>
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
  var allRows = JSON.parse(document.getElementById("data").textContent);
  var rows = allRows.slice();
  var CONFIRMED = { "income": true, "expense": true, "refund": true };
  var CASH_FLOW_FIELD = {
    "income": "income", "expense": "spending", "refund": "refunds"
  };
  var EXCLUDED_FLOW = {
    "internal_transfer": true,
    "credit_card_payment": true,
    "investment_transfer": true
  };
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
  allRows.forEach(function (row) {
    if (row.amount === null) { return; }
    var category = row.category || "Unknown";
    catMagnitude[category] = (catMagnitude[category] || 0) + Math.abs(row.amount);
  });
  var COLOR = {};
  Object.keys(catMagnitude)
    .sort(function (a, b) { return catMagnitude[b] - catMagnitude[a]; })
    .forEach(function (category, index) { COLOR[category] = PALETTE[index % PALETTE.length]; });
  function colorFor(category) { return COLOR[category] || "var(--ink-faint)"; }

  var availableOwners = allRows
    .map(function (row) { return row.owner; })
    .filter(function (owner, index, owners) {
      return owners.indexOf(owner) === index;
    })
    .sort();

  function renderOwnerControls() {
    var container = document.getElementById("owner-filter");
    if (!availableOwners.length) {
      container.textContent = "No owners in this report.";
      document.getElementById("owner-select-all").disabled = true;
      document.getElementById("owner-clear").disabled = true;
      return;
    }
    availableOwners.forEach(function (owner, index) {
      var label = document.createElement("label");
      label.className = "owner-option";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.value = owner;
      input.id = "owner-" + index;
      input.checked = true;
      var name = document.createElement("span");
      name.textContent = owner || "Unassigned";
      label.appendChild(input);
      label.appendChild(name);
      container.appendChild(label);
    });
  }

  function selectedOwners() {
    return Array.prototype.slice.call(
      document.querySelectorAll("#owner-filter input:checked")
    ).map(function (input) { return input.value; });
  }

  function applyOwnerFilter() {
    var selected = selectedOwners();
    rows = allRows.filter(function (row) { return selected.indexOf(row.owner) >= 0; });
    var label = document.getElementById("owner-view-label");
    if (selected.length === availableOwners.length) { label.textContent = "Combined view"; }
    else if (!selected.length) { label.textContent = "No owners selected"; }
    else { label.textContent = selected.join(", "); }
    renderAll();
  }

  function flowSummary() {
    var result = {
      spending: 0, income: 0, net: 0,
      unresolved_inflow: 0, unresolved_outflow: 0, uncategorized: 0
    };
    rows.forEach(function (row) {
      if (!row.category || row.category === "Unknown") { result.uncategorized += 1; }
      if (row.amount === null) { return; }
      var field = CASH_FLOW_FIELD[row.flow_type];
      if (field === "income") {
        result.income += row.amount;
      } else if (field) {
        result.spending += row.amount;
      } else if (row.flow_type === "unresolved" && row.amount > 0) {
        result.unresolved_inflow += row.amount;
      } else if (row.flow_type === "unresolved" && row.amount < 0) {
        result.unresolved_outflow += row.amount;
      }
    });
    result.net = result.spending + result.income;
    return result;
  }

  function emptyCashFlow() {
    return { income: 0, spending: 0, refunds: 0, net_cash_flow: 0 };
  }

  function addCashFlow(target, row) {
    if (row.amount === null) { return; }
    var field = CASH_FLOW_FIELD[row.flow_type];
    if (!field) { return; }
    target[field] += row.amount;
    target.net_cash_flow += row.amount;
  }

  function valuationSummary() {
    var actual = emptyCashFlow();
    var estimated = emptyCashFlow();
    var result = {
      missing: 0, blocking: 0, excluded: 0, unresolved: 0, zero: 0, other: 0,
      actual: actual, estimated: estimated, combined: emptyCashFlow()
    };
    rows.forEach(function (row) {
      if (row.valuation_status === "actual") { addCashFlow(actual, row); }
      else if (row.valuation_status === "estimated") { addCashFlow(estimated, row); }
      if (row.valuation_status !== "missing") { return; }
      result.missing += 1;
      if (EXCLUDED_FLOW[row.flow_type]) {
        result.excluded += 1;
      } else if (row.flow_type === "unresolved") {
        result.unresolved += 1;
      } else if (CASH_FLOW_FIELD[row.flow_type]) {
        if (row.posted_amount === 0) { result.zero += 1; }
        else { result.blocking += 1; }
      } else {
        result.other += 1;
      }
    });
    ["income", "spending", "refunds", "net_cash_flow"].forEach(function (field) {
      result.combined[field] = actual[field] + estimated[field];
    });
    return result;
  }

  function setCompleteness(id, value) {
    document.getElementById(id).querySelector(".value").textContent = value;
  }

  function setCashFlowRow(id, actual, estimated, combined) {
    var cells = document.getElementById(id).querySelectorAll("td");
    cells[1].textContent = fmt(actual);
    cells[2].textContent = fmt(estimated);
    cells[3].textContent = fmt(combined);
  }

  function sourceOccurrenceCount() {
    var seenGroups = {};
    return rows.reduce(function (total, row) {
      if (row.canonical_group_id) {
        if (seenGroups[row.canonical_group_id]) { return total; }
        seenGroups[row.canonical_group_id] = true;
      }
      var count = parseInt(row.source_occurrence_count, 10);
      return total + (count > 0 ? count : 1);
    }, 0);
  }

  function renderTiles() {
    var summary = flowSummary();
    var valuation = valuationSummary();
    document.getElementById("source-count").textContent =
      sourceOccurrenceCount() + " source occurrences";
    document.getElementById("txn-count").textContent = rows.length;
    document.getElementById("tile-spending").textContent = fmt(summary.spending);
    document.getElementById("tile-income").textContent = fmt(summary.income);
    document.getElementById("tile-net").textContent = fmt(summary.net);
    document.getElementById("tile-unresolved-inflow").textContent =
      fmt(summary.unresolved_inflow);
    document.getElementById("tile-unresolved-outflow").textContent =
      fmt(summary.unresolved_outflow);
    document.getElementById("tile-uncategorized").textContent = summary.uncategorized;
    var netEl = document.getElementById("tile-net");
    netEl.classList.toggle("pos", summary.net >= 0);
    netEl.classList.toggle("neg", summary.net < 0);

    setCompleteness("missing-total", valuation.missing);
    setCompleteness("missing-blocking", valuation.blocking);
    setCompleteness("missing-excluded", valuation.excluded);
    setCompleteness("missing-unresolved", valuation.unresolved);
    setCompleteness("missing-zero", valuation.zero);
    setCompleteness("missing-other", valuation.other);
    setCashFlowRow(
      "cash-flow-income",
      valuation.actual.income,
      valuation.estimated.income,
      valuation.combined.income
    );
    setCashFlowRow(
      "cash-flow-spending",
      valuation.actual.spending,
      valuation.estimated.spending,
      valuation.combined.spending
    );
    setCashFlowRow(
      "cash-flow-refunds",
      valuation.actual.refunds,
      valuation.estimated.refunds,
      valuation.combined.refunds
    );
    setCashFlowRow(
      "cash-flow-net",
      valuation.actual.net_cash_flow,
      valuation.estimated.net_cash_flow,
      valuation.combined.net_cash_flow
    );
    var warning = document.getElementById("valuation-warning");
    warning.hidden = valuation.missing === 0;
    warning.textContent = valuation.missing +
      (valuation.missing === 1 ? " row has" : " rows have") +
      " no HKD valuation. " + valuation.blocking +
      (valuation.blocking === 1 ? " row blocks" : " rows block") +
      " confirmed cash-flow totals.";
  }

  function chartData(excludeTransfers) {
    var totals = {}, counts = {}, chartedCount = 0;
    rows.forEach(function (row) {
      if (row.amount === null) { return; }
      if (excludeTransfers && !CONFIRMED[row.flow_type]) { return; }
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
      ["Original", "amt"], ["Amount (HKD)", "amt"], ["Valuation", ""],
      ["Account", "col-account"], ["Owner", "col-owner"]
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
      ec.colSpan = 8;
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
        nm.title = row.category + " · " + row.flow_type;
        wrap.appendChild(sw);
        wrap.appendChild(nm);
        if (row.needs_review) {
          var rv = document.createElement("span");
          rv.className = "review";
          rv.textContent = "review: " + row.review_reason_labels.join(", ");
          rv.title = row.review_reason_labels.join(", ");
          wrap.appendChild(rv);
        }
        if (row.provenance_status && row.provenance_status !== "single_source") {
          var provenance = document.createElement("span");
          provenance.className = "provenance";
          provenance.textContent = "overlap";
          provenance.title = row.provenance_status + " · " +
            row.source_occurrence_count + " source occurrences";
          wrap.appendChild(provenance);
        }
        catCell.appendChild(wrap);

        var originalCell = tr.insertCell();
        originalCell.className = "amt";
        originalCell.textContent = row.original_amount +
          (row.original_currency ? " " + row.original_currency : "");

        var amtCell = tr.insertCell();
        amtCell.className = "amt " + (row.amount === null ? "na" : signClass(row.amount));
        amtCell.textContent = fmt(row.amount);

        var valuationCell = tr.insertCell();
        valuationCell.textContent = row.valuation_label;
        valuationCell.title = [
          row.valuation_source,
          row.valuation_provider,
          row.valuation_rate_date ? "rate date " + row.valuation_rate_date : ""
        ].filter(Boolean).join(" · ");

        cell(tr, "account col-account", row.account, row.account);
        cell(tr, "owner col-owner", row.owner, row.owner);
      });
  }

  function renderBalanceCoverage() {
    var selectedAccounts = {};
    rows.forEach(function (row) { selectedAccounts[row.account_id || ""] = true; });
    var visible = 0;
    Array.prototype.forEach.call(
      document.querySelectorAll("#balance-coverage tr[data-account-id]"),
      function (row) {
        row.hidden = !selectedAccounts[row.getAttribute("data-account-id")];
        if (!row.hidden) { visible += 1; }
      }
    );
    var empty = document.getElementById("balance-coverage-empty");
    empty.hidden = visible > 0;
    if (!visible) { empty.textContent = "No statement sections match this owner view."; }
  }

  function cell(tr, className, text, title) {
    var td = tr.insertCell();
    td.className = className;
    td.textContent = text || "";
    if (title) { td.title = title; }
    return td;
  }

  function renderAll() {
    renderTiles();
    renderChart();
    renderTransactions();
    renderBalanceCoverage();
  }

  renderOwnerControls();
  document.getElementById("owner-filter").addEventListener("change", applyOwnerFilter);
  document.getElementById("owner-select-all").addEventListener("click", function () {
    Array.prototype.forEach.call(
      document.querySelectorAll("#owner-filter input"),
      function (input) { input.checked = true; }
    );
    applyOwnerFilter();
  });
  document.getElementById("owner-clear").addEventListener("click", function () {
    Array.prototype.forEach.call(
      document.querySelectorAll("#owner-filter input"),
      function (input) { input.checked = false; }
    );
    applyOwnerFilter();
  });
  document.getElementById("exclude-transfers").addEventListener("change", renderChart);
  renderAll();
})();
</script>
</body>
</html>
"""


def build_report_html(
    rows: list[dict[str, str]],
    period_label: str,
    *,
    source_occurrence_count: int | None = None,
    balance_reconciliation: BalanceReconciliation | None = None,
) -> str:
    data = json.dumps(
        [_report_row(row) for row in rows],
        ensure_ascii=True,
        sort_keys=True,
    ).replace("</", "<\\/")
    summary = _flow_summary(rows)
    valuation = valuation_summary(rows)
    missing_count = valuation["missing_count"]
    blocking_count = valuation["cash_flow_blocking_missing_count"]
    warning = (
        '<p class="warning" id="valuation-warning" role="alert"'
        f"{' hidden' if not missing_count else ''}>"
        f"{missing_count} "
        f"{'row has' if missing_count == 1 else 'rows have'} no HKD valuation. "
        f"{blocking_count} "
        f"{'row blocks' if blocking_count == 1 else 'rows block'} confirmed "
        "cash-flow totals.</p>"
    )
    cash_flow = valuation["cash_flow"]
    actual = cash_flow["actual"]
    estimated = cash_flow["estimated"]
    combined = cash_flow["combined_estimate"]
    replacements = {
        "__PERIOD__": html.escape(period_label),
        "__SOURCE_COUNT__": str(
            len(rows) if source_occurrence_count is None else source_occurrence_count
        ),
        "__CANONICAL_COUNT__": str(len(rows)),
        "__DATA__": data,
        "__SPENDING__": _format_amount(summary["spending"]),
        "__INCOME__": _format_amount(summary["income"]),
        "__NET__": _format_amount(summary["net"]),
        "__UNRESOLVED_INFLOW__": _format_amount(summary["unresolved_inflow"]),
        "__UNRESOLVED_OUTFLOW__": _format_amount(summary["unresolved_outflow"]),
        "__UNCATEGORIZED__": str(summary["uncategorized"]),
        "__COMPLETENESS_WARNING__": warning,
        "__MISSING_TOTAL__": str(missing_count),
        "__MISSING_BLOCKING__": str(blocking_count),
        "__MISSING_EXCLUDED__": str(valuation["excluded_flow_missing_count"]),
        "__MISSING_UNRESOLVED__": str(valuation["unresolved_flow_missing_count"]),
        "__MISSING_ZERO__": str(valuation["zero_amount_missing_count"]),
        "__MISSING_OTHER__": str(valuation["other_flow_missing_count"]),
        "__ACTUAL_INCOME__": _format_total(actual["income"]),
        "__ACTUAL_SPENDING__": _format_total(actual["spending"]),
        "__ACTUAL_REFUNDS__": _format_total(actual["refunds"]),
        "__ACTUAL_NET__": _format_total(actual["net_cash_flow"]),
        "__ESTIMATED_INCOME__": _format_total(estimated["income"]),
        "__ESTIMATED_SPENDING__": _format_total(estimated["spending"]),
        "__ESTIMATED_REFUNDS__": _format_total(estimated["refunds"]),
        "__ESTIMATED_NET__": _format_total(estimated["net_cash_flow"]),
        "__COMBINED_INCOME__": _format_total(combined["income"]),
        "__COMBINED_SPENDING__": _format_total(combined["spending"]),
        "__COMBINED_REFUNDS__": _format_total(combined["refunds"]),
        "__COMBINED_NET__": _format_total(combined["net_cash_flow"]),
        "__BALANCE_COVERAGE__": _balance_coverage_html(balance_reconciliation or {}),
    }
    document = _PAGE_TEMPLATE
    for placeholder, value in replacements.items():
        document = document.replace(placeholder, value)
    return document


def _balance_coverage_html(
    balance_reconciliation: BalanceReconciliation,
) -> str:
    body: list[str] = []
    for account_id, account in sorted(balance_reconciliation.items()):
        for statement in account["statements"]:
            source_file = statement.get("source_file", "") or "(unknown)"
            section = statement.get("statement_section", "") or "(none)"
            currency = statement.get("posted_currency", "") or "(unknown)"
            opening = "found" if statement.get("opening_evidence_found") else "missing"
            closing = "found" if statement.get("closing_evidence_found") else "missing"
            details = (
                account_id or "(unknown)",
                source_file,
                section,
                currency,
                opening,
                closing,
            )
            cells = "".join(f"<td>{html.escape(value)}</td>" for value in details)
            outcome = html.escape(statement["result"])
            reason = html.escape(statement.get("reason", ""))
            body.append(
                f'<tr data-account-id="{html.escape(account_id, quote=True)}">'
                f'{cells}<td class="outcome">{outcome}</td><td>{reason}</td></tr>'
            )
    empty_state = (
        '<tr id="balance-coverage-empty" hidden>'
        '<td colspan="8" class="empty">'
        "No statement sections match this owner view.</td></tr>"
        if body
        else '<tr id="balance-coverage-empty">'
        '<td colspan="8" class="empty">No statement sections found.</td></tr>'
    )
    rows = "".join(body) + empty_state
    return (
        '<section class="panel rise d2" id="balance-coverage" '
        'aria-label="Statement balance coverage">'
        '<div class="panel-head"><h2>Statement balance coverage</h2>'
        '<span class="hint">Evidence only; unavailable values stay hidden</span></div>'
        '<div class="panel-body table-wrap"><table class="coverage-table">'
        "<thead><tr><th>Account</th><th>Source</th><th>Section</th>"
        "<th>Currency</th><th>Opening</th><th>Closing</th><th>Outcome</th>"
        f"<th>Reason</th></tr></thead><tbody>{rows}</tbody></table></div></section>"
    )


def _report_row(row: dict[str, str]) -> dict[str, object]:
    amount = _amount_value(row.get("amount_hkd", ""))
    valuation_source = row.get("valuation_source", "")
    return {
        "date": row.get("date", ""),
        "merchant": row.get("merchant", ""),
        "category": row.get("category", ""),
        "flow_type": row.get("flow_type", "unresolved"),
        "amount": amount,
        "posted_amount": _amount_value(row.get("posted_amount", "")),
        "original_amount": row.get("original_amount", "")
        or row.get("posted_amount", ""),
        "original_currency": row.get("original_currency", "")
        or row.get("posted_currency", ""),
        "valuation_source": valuation_source,
        "valuation_status": _report_valuation_status(
            row.get("valuation_status", ""),
            valuation_source,
            amount,
        ),
        "valuation_rate_date": row.get("valuation_rate_date", ""),
        "valuation_provider": row.get("valuation_provider", ""),
        "valuation_label": _valuation_label(row),
        "account": row.get("account", ""),
        "account_id": row.get("account_id", ""),
        "owner": row.get("owner", ""),
        "needs_review": row.get("needs_review") == "true",
        "review_reasons": [
            item for item in row.get("review_reasons", "").split(";") if item
        ],
        "review_reason_labels": _review_labels(row.get("review_reasons", "")),
        "transaction_id": row.get("transaction_id", ""),
        "canonical_group_id": row.get("canonical_group_id", ""),
        "canonical_slot": row.get("canonical_slot", ""),
        "provenance_status": row.get("provenance_status", ""),
        "source_occurrence_count": row.get("source_occurrence_count", ""),
    }


def _report_valuation_status(
    status: str,
    source: str,
    amount: float | None,
) -> str:
    if status:
        return status
    if amount is None:
        return "missing"
    if source in {
        "configured_dated_rate",
        "hkma_daily_reference_rate",
        "configured_fixed_rate",
    }:
        return "estimated"
    return "actual"


def _valuation_label(row: dict[str, str]) -> str:
    status = row.get("valuation_status", "") or "missing"
    source = row.get("valuation_source", "") or "missing"
    labels = {
        "statement_posted": "Statement-posted",
        "matched_exchange_leg": "Matched exchange leg",
        "configured_dated_rate": "Dated rate estimate",
        "hkma_daily_reference_rate": "HKMA reference estimate",
        "configured_fixed_rate": "Fixed rate estimate",
        "missing": "Missing",
    }
    label = f"{labels.get(source, source)} ({status})"
    details = [
        row.get("valuation_rate_date", ""),
        row.get("valuation_provider", ""),
    ]
    details = [detail for detail in details if detail]
    return f"{label} · {' · '.join(details)}" if details else label


def _review_labels(value: str) -> list[str]:
    from honeymoney.review_state import review_reason_labels

    return review_reason_labels(value)


def _amount_value(value: str) -> float | None:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return float(amount) if amount.is_finite() else None


def missing_base_currency_count(rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        if _amount_value(row.get("amount_hkd", "")) is not None:
            continue
        posted_amount = _amount_value(row.get("posted_amount", ""))
        if posted_amount is not None and posted_amount != 0:
            count += 1
    return count


def _flow_summary(rows: list[dict[str, str]]) -> dict[str, Decimal | int]:
    spending = Decimal("0")
    income = Decimal("0")
    unresolved_inflow = Decimal("0")
    unresolved_outflow = Decimal("0")
    uncategorized = 0
    for row in rows:
        if row.get("category", "") in {"", "Unknown"}:
            uncategorized += 1
        try:
            amount = Decimal(row.get("amount_hkd", ""))
        except (InvalidOperation, ValueError):
            continue
        if not amount.is_finite():
            continue
        flow_type = row.get("flow_type", "unresolved")
        if flow_type in {"expense", "refund"}:
            spending += amount
        elif flow_type == "income":
            income += amount
        elif flow_type == "unresolved" and amount > 0:
            unresolved_inflow += amount
        elif flow_type == "unresolved" and amount < 0:
            unresolved_outflow += amount
    return {
        "spending": spending,
        "income": income,
        "net": spending + income,
        "unresolved_inflow": unresolved_inflow,
        "unresolved_outflow": unresolved_outflow,
        "uncategorized": uncategorized,
    }


def _format_amount(value: Decimal | int) -> str:
    return f"{Decimal(value):,.2f}"


def _format_total(value: str) -> str:
    return f"{Decimal(value):,.2f}"
