let period = "all";
let activeDesk = "overview";
let activeMode = "backtest";
let optFilter = "all";
let resultFilter = "all";
let searchQuery = "";
let lastSummary = null;
let lotSizeHint = 65;
const charts = {};
const chartTheme = {
  tick: "#8aa094",
  grid: "rgba(232,240,235,0.06)",
  ink: "#e8f0eb",
};

function currentTheme() {
  return document.documentElement.classList.contains("theme-light") ? "light" : "dark";
}

function syncChartTheme() {
  const light = currentTheme() === "light";
  chartTheme.tick = light ? "#5f7368" : "#8aa094";
  chartTheme.grid = light ? "rgba(20,35,28,0.07)" : "rgba(232,240,235,0.06)";
  chartTheme.ink = light ? "#14231c" : "#e8f0eb";
  if (typeof tinyLegend !== "undefined" && tinyLegend.labels) {
    tinyLegend.labels.color = chartTheme.tick;
  }
}

const money = (n, digits = 2) =>
  n == null || Number.isNaN(Number(n))
    ? "—"
    : `₹${Number(n).toLocaleString("en-IN", { maximumFractionDigits: digits })}`;

const num = (n, digits = 2) =>
  n == null || Number.isNaN(Number(n))
    ? "—"
    : Number(n).toLocaleString("en-IN", { maximumFractionDigits: digits });

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
  return data;
}

function toast(text, kind = "") {
  const wrap = document.getElementById("toastWrap");
  const el = document.createElement("div");
  el.className = `toast ${kind}`.trim();
  el.textContent = text || "";
  wrap.innerHTML = "";
  if (!text) return;
  wrap.appendChild(el);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    if (el.parentNode) el.remove();
  }, 4200);
}

function setBusy(btn, busy) {
  if (!btn) return;
  btn.classList.toggle("busy", !!busy);
  const spin = btn.querySelector(".btn-spin");
  const label = btn.querySelector(".btn-label");
  if (spin) spin.hidden = !busy;
  if (label && busy) label.textContent = "Running…";
  if (label && !busy) label.textContent = "Backtest";
}

function destroyChart(key) {
  if (charts[key]) {
    charts[key].destroy();
    delete charts[key];
  }
}

const tinyLegend = {
  position: "bottom",
  labels: {
    boxWidth: 8,
    boxHeight: 8,
    font: { size: 10, family: "IBM Plex Sans" },
    color: chartTheme.tick,
    padding: 8,
  },
};

function axisOpts() {
  return {
    x: {
      grid: { color: chartTheme.grid, drawBorder: false },
      ticks: { font: { size: 9, family: "IBM Plex Sans" }, color: chartTheme.tick, maxTicksLimit: 6, maxRotation: 0 },
    },
    y: {
      grid: { color: chartTheme.grid, drawBorder: false },
      ticks: { font: { size: 9, family: "IBM Plex Sans" }, color: chartTheme.tick, maxTicksLimit: 5 },
    },
  };
}

function companyOf(t) {
  return (
    t.raw?.option?.underlying ||
    t.symbol ||
    (t.option_symbol ? String(t.option_symbol).replace(/\d.*$/, "") : "") ||
    "—"
  );
}

function optLabel(t) {
  if (!t.option_type && (t.desk === "stocks" || t.raw?.trade_instrument === "equity")) {
    return { opt: "EQ", strike: "—", cls: "pos", text: `EQ ${t.symbol || ""}` };
  }
  const opt = t.option_type || "—";
  const strike = t.strike ?? "—";
  const cls = opt === "CE" ? "pos" : opt === "PE" ? "neg" : "";
  return { opt, strike, cls, text: `${opt} ${strike}` };
}

function fmtTsFull(v) {
  if (!v) return "—";
  const s = String(v).replace("T", " ");
  // Prefer IST-looking local stamp without ms/timezone noise
  if (s.length >= 19) return s.slice(0, 19);
  if (s.length >= 16) return s.slice(0, 16);
  return s;
}

function qtyOf(t) {
  return Number(t.qty || 0) || 0;
}

function lotsOf(t) {
  const qty = qtyOf(t);
  if (!qty) return "—";
  const lot = Number(t.raw?.option?.lot_size || lotSizeHint || 65) || 65;
  if (t.desk === "stocks" || t.raw?.trade_instrument === "equity" || lot <= 1) {
    return `${qty} sh`;
  }
  const lots = qty / lot;
  return Number.isInteger(lots) ? `${lots}×${lot}` : `${qty}`;
}

function setActiveDesk(desk) {
  activeDesk = desk === "nifty" || desk === "stocks" ? desk : "overview";
  document.querySelectorAll(".desk-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.desk === activeDesk);
  });
  const overview = document.getElementById("overviewPanel");
  const work = document.getElementById("deskWorkbench");
  const isOverview = activeDesk === "overview";
  if (overview) overview.hidden = !isOverview;
  if (overview) overview.classList.toggle("on", isOverview);
  if (work) work.hidden = isOverview;
  const bt = document.getElementById("btnBacktest");
  const pt = document.getElementById("btnPaper");
  if (bt) bt.disabled = isOverview;
  if (pt) pt.disabled = isOverview;
  refreshAll();
}

function investedOf(t) {
  const stored = Number(t.invested_amount);
  if (stored > 0) return stored;
  const prem = Number(t.option_premium ?? t.raw?.option?.premium);
  const qty = qtyOf(t);
  if (prem > 0 && qty > 0) return prem * qty;
  const spot = Number(t.spot_ref ?? t.entry_price ?? t.planned_entry);
  if (spot > 0 && qty > 0) return spot * 0.008 * qty;
  return null;
}

function premiumOf(t) {
  const prem = Number(t.option_premium ?? t.raw?.option?.premium);
  if (prem > 0) return prem;
  const inv = investedOf(t);
  const qty = qtyOf(t);
  if (inv > 0 && qty > 0) return inv / qty;
  return null;
}

/** Exit premium / unit — from stored exit prem, else entry prem + PnL/qty. */
function exitPremiumOf(t) {
  const stored = Number(t.raw?.option?.exit_premium ?? t.exit_premium);
  if (Number.isFinite(stored) && stored > 0) return stored;
  const entryPrem = premiumOf(t);
  const qty = qtyOf(t);
  if (entryPrem != null && t.pnl != null && qty > 0) {
    return entryPrem + Number(t.pnl) / qty;
  }
  return null;
}

/** Spot/index unit price used by engine fills (PnL = Δprice × qty). */
function unitPrice(t, which) {
  let v = null;
  if (which === "entry") v = Number(t.entry_price ?? t.planned_entry);
  else if (which === "exit") v = Number(t.exit_price);
  else if (which === "stop") v = Number(t.planned_stop);
  else if (which === "target") v = Number(t.planned_target);
  return Number.isFinite(v) ? v : null;
}

function qtyPrice(unit, t) {
  const qty = qtyOf(t);
  if (unit == null || Number.isNaN(Number(unit)) || !qty) return null;
  return Number(unit) * qty;
}

function pxPairHtml(unit, total) {
  if (unit == null || Number.isNaN(Number(unit))) {
    return `<div class="px-cell"><b>—</b></div>`;
  }
  return `<div class="px-cell">
    <b>${money(unit)}</b>
    <small class="qty-note">${total == null ? "—" : money(total)} ×qty</small>
  </div>`;
}

function pxCells(t, which) {
  const u = unitPrice(t, which);
  return { unit: u, qty: qtyPrice(u, t), html: pxPairHtml(u, qtyPrice(u, t)) };
}

function pnlPerUnit(t) {
  const qty = qtyOf(t);
  if (t.pnl == null || !qty) return null;
  return Number(t.pnl) / qty;
}

function riskCapitalOf(t) {
  const v = Number(t.max_loss_planned ?? t.risk_verdict?.max_loss);
  return v > 0 ? v : null;
}

function fmtTs(v) {
  if (!v) return "—";
  const s = String(v).replace("T", " ");
  if (s.length >= 16) return s.slice(0, 16);
  return s;
}

function durationLabel(entry, exit) {
  try {
    const a = new Date(entry);
    const b = new Date(exit);
    const mins = Math.max(0, Math.round((b - a) / 60000));
    if (mins < 60) return `${mins}m`;
    return `${Math.floor(mins / 60)}h ${mins % 60}m`;
  } catch {
    return "—";
  }
}

function matchesClientFilters(t) {
  if (optFilter !== "all" && String(t.option_type || "").toUpperCase() !== optFilter) return false;
  if (resultFilter === "win" && !(Number(t.pnl) > 0)) return false;
  if (resultFilter === "loss" && !(Number(t.pnl) < 0)) return false;
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    const blob = [
      companyOf(t),
      t.symbol,
      t.desk,
      t.option_type,
      t.strike,
      t.session_date,
      t.exit_reason,
      t.raw?.exit_label,
      t.why,
      t.option_symbol,
      t.strategy,
      t.expiry,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (!blob.includes(q)) return false;
  }
  return true;
}

function filterList(rows) {
  return (rows || []).filter(matchesClientFilters);
}

function renderEquityChart(canvasId, curve, key) {
  destroyChart(key);
  const el = document.getElementById(canvasId);
  if (!el) return;
  const labels = (curve || []).map((p) => (p.t || "").slice(5, 16).replace("T", " "));
  const data = (curve || []).map((p) => p.equity);
  charts[key] = new Chart(el, {
    type: "line",
    data: {
      labels: labels.length ? labels : ["—"],
      datasets: [{
        data: data.length ? data : [0],
        borderColor: "#2fd18a",
        backgroundColor: (ctx) => {
          const { chart } = ctx;
          const { ctx: c, chartArea } = chart;
          if (!chartArea) return "rgba(47,209,138,0.12)";
          const g = c.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          g.addColorStop(0, "rgba(47,209,138,0.28)");
          g.addColorStop(1, "rgba(47,209,138,0)");
          return g;
        },
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        borderWidth: 2.2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 700, easing: "easeOutQuart" },
      plugins: { legend: { display: false } },
      scales: axisOpts(),
    },
  });
}

function renderWinChart(canvasId, stats, key) {
  destroyChart(key);
  const el = document.getElementById(canvasId);
  if (!el) return;
  charts[key] = new Chart(el, {
    type: "doughnut",
    data: {
      labels: ["W", "L", "F"],
      datasets: [{
        data: [stats.wins || 0, stats.losses || 0, stats.flats || 0],
        backgroundColor: ["#2fd18a", "#ff6b5c", "#6d8578"],
        borderWidth: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "64%",
      animation: { animateRotate: true, duration: 800 },
      plugins: { legend: tinyLegend },
    },
  });
}

function renderExitChart(canvasId, breakdown, key) {
  destroyChart(key);
  const el = document.getElementById(canvasId);
  if (!el) return;
  const labels = Object.keys(breakdown || {});
  const values = Object.values(breakdown || {});
  charts[key] = new Chart(el, {
    type: "bar",
    data: {
      labels: labels.length ? labels : ["none"],
      datasets: [{
        data: values.length ? values : [0],
        backgroundColor: "#3ecfcf",
        borderRadius: 6,
        maxBarThickness: 28,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 650 },
      plugins: { legend: { display: false } },
      scales: axisOpts(),
    },
  });
}

function renderPnlBars(canvasId, curve, key) {
  destroyChart(key);
  const el = document.getElementById(canvasId);
  if (!el) return;
  const labels = (curve || []).map((_, i) => `${i + 1}`);
  const data = (curve || []).map((p) => p.pnl);
  charts[key] = new Chart(el, {
    type: "bar",
    data: {
      labels: labels.length ? labels : ["—"],
      datasets: [{
        data: data.length ? data : [0],
        backgroundColor: (data.length ? data : [0]).map((v) =>
          v >= 0 ? "rgba(47,209,138,0.85)" : "rgba(255,107,92,0.85)"
        ),
        borderRadius: 4,
        maxBarThickness: 18,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 650 },
      plugins: { legend: { display: false } },
      scales: axisOpts(),
    },
  });
}

function renderStats(elId, s) {
  const el = document.getElementById(elId);
  if (!el || !s) return;
  const cells = [
    ["Trades", s.total], ["Open", s.open], ["Closed", s.closed],
    ["Wins", s.wins], ["Losses", s.losses], ["Win %", `${s.win_rate}%`],
    ["Net PnL", money(s.pnl)], ["Gross profit", money(s.gross_profit)],
    ["Avg R", s.avg_r], ["Best", money(s.best_trade)],
    ["Worst", money(s.worst_trade)], ["PF", s.profit_factor],
  ];
  el.innerHTML = cells.map(([label, val], i) => {
    let cls = "";
    if (["Net PnL", "Gross profit", "Best", "Worst"].includes(label)) {
      const n = Number(String(val).replace(/[₹,]/g, ""));
      if (!Number.isNaN(n)) cls = n >= 0 ? "pos" : "neg";
    }
    return `<article style="animation-delay:${i * 30}ms"><label>${label}</label><strong class="${cls}">${val}</strong></article>`;
  }).join("");
}

function paintHeroMetrics() {
  if (!lastSummary) return;
  const s = lastSummary;
  const acct = s.account || {};
  const capital = Number(acct.total_capital ?? s.config?.budget ?? 0);
  const mode = activeMode;
  let profit = 0;
  let balance = capital;
  let tag = "Backtest";
  let profitNote = "Net closed PnL · backtest";
  let balanceNote = "Capital + backtest PnL";

  if (mode === "paper") {
    profit = Number(acct.paper_profit ?? s.paper?.pnl ?? 0);
    balance = Number(acct.paper_cash ?? s.paper_account?.cash ?? capital);
    tag = "Paper";
    profitNote = "Net closed PnL · paper";
    balanceNote = "Available paper cash";
  } else if (mode === "live") {
    profit = Number(acct.live_profit ?? s.live?.pnl ?? 0);
    balance = capital + profit;
    tag = "Live";
    profitNote = "Net closed PnL · live";
    balanceNote = "Capital + live PnL";
  } else {
    profit = Number(acct.backtest_profit ?? s.backtest?.pnl ?? 0);
    balance = Number(acct.backtest_balance ?? capital + profit);
    tag = "Backtest";
  }

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  const setTone = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove("pos", "neg");
    if (Number.isFinite(Number(value))) el.classList.add(Number(value) >= 0 ? "pos" : "neg");
  };

  setText("mCapital", money(capital));
  setText("mTotalProfit", money(profit));
  setTone("mTotalProfit", profit);
  setText("mBalance", money(balance));
  setTone("mBalance", balance - capital);
  setText("mProfitLabel", `${tag} profit`);
  setText("mProfitTag", tag);
  setText("mBalanceLabel", `${tag} balance`);
  setText("mBalanceTag", tag);
  setText("mProfitSub", profitNote);
  setText("mBalanceSub", balanceNote);
}

function planFromSummary() {
  const s = lastSummary || {};
  const cfg = s.config || {};
  const plan = cfg.plan || {};
  const cash = Number(
    s.paper_account?.cash ?? s.account?.paper_cash ?? s.account?.free_cash ?? plan.cash_budget ?? cfg.budget ?? 0,
  );
  const maxRisk = Number(plan.max_risk_inr ?? cfg.max_loss_per_trade_inr ?? 0);
  const maxProfit = Number(plan.max_profit_inr ?? cfg.max_profit_per_trade_inr ?? 0);
  const trailAct = Number(plan.trail_activate_inr ?? cfg.trail_activate_inr ?? 0);
  const trailStep = Number(plan.trail_step_inr ?? cfg.trail_step_inr ?? 0);
  return {
    cash,
    maxRisk,
    maxProfit,
    trailAct,
    trailStep,
    trailActivateR: plan.trail_activate_r ?? cfg.trail_activate_r,
    trailStepR: plan.trail_step_r ?? cfg.trail_step_r,
    trailEnabled: cfg.trail_enabled !== false,
    lossPerLot: Number(plan.loss_per_lot_inr ?? cfg.loss_per_lot_inr ?? 0),
    profitPerLot: Number(plan.profit_per_lot_inr ?? cfg.profit_per_lot_inr ?? 0),
    targetR: Number(cfg.default_target_r ?? cfg.min_reward_risk ?? 0),
  };
}

function lotsCountOf(t) {
  const fromVerdict = Number(t.risk_verdict?.lots ?? t.lots ?? t.raw?.lots);
  if (fromVerdict > 0) return fromVerdict;
  const qty = qtyOf(t);
  const lot = Number(t.raw?.option?.lot_size || lotSizeHint || 65) || 65;
  if (!qty) return 1;
  if (t.desk === "stocks" || t.raw?.trade_instrument === "equity" || lot <= 1) {
    // Stock F&O: treat each contract lot as 1 for plan math when qty is premium units
    const optLot = Number(t.raw?.option?.lot_size || t.option_lot_size || 0);
    if (optLot > 1) return Math.max(1, Math.round(qty / optLot));
    return Math.max(1, Math.round(qty));
  }
  return Math.max(1, Math.round(qty / lot));
}

function tradePlanNumbers(t) {
  const desk = planFromSummary();
  const lots = lotsCountOf(t);
  const riskTrade = riskCapitalOf(t);
  const rr = Number(t.reward_risk || t.target_r || desk.targetR || 0);
  let maxRisk = riskTrade;
  if (maxRisk == null || !(maxRisk > 0)) {
    maxRisk = desk.lossPerLot > 0 ? desk.lossPerLot * lots : desk.maxRisk || null;
  }
  let maxProfit = null;
  if (maxRisk != null && rr > 0) maxProfit = maxRisk * rr;
  else if (desk.profitPerLot > 0) maxProfit = desk.profitPerLot * lots;
  else if (desk.maxProfit > 0) maxProfit = desk.maxProfit;
  const trailAct =
    maxRisk != null && desk.trailActivateR
      ? maxRisk * Number(desk.trailActivateR)
      : desk.trailAct || null;
  const trailStep =
    maxRisk != null && desk.trailStepR
      ? maxRisk * Number(desk.trailStepR)
      : desk.trailStep || null;
  return { desk, lots, maxRisk, maxProfit, trailAct, trailStep, rr };
}

function showDetail(elId, t) {
  const el = document.getElementById(elId);
  if (!t) {
    el.className = "detail empty";
    el.textContent = "Select a trade";
    return;
  }
  const steps = (t.strategy_steps || []).map((s) => `<li>${s}</li>`).join("");
  const risk = t.risk_verdict
    ? `PASS=${t.risk_verdict.passed} · qty ${t.risk_verdict.qty} · max ${money(t.risk_verdict.max_loss)}`
    : "—";
  const trail = t.raw?.trail || {};
  const trailLine = trail.activated
    ? `Yes · peak ${trail.peak ?? "—"} · stop ${trail.final_stop ?? "—"}`
    : "No";
  const so = trail.scale_out;
  const outs = trail.scale_outs || (so ? [so] : []);
  const runner = trail.runner;
  const ladder = (trail.ladder_inr || [1000, 1500, 1800]).join(" → ");
  const scaleLine = outs.length
    ? outs.map((x) => `L${x.lot_index || "?"} @ ${x.price} (₹${x.pnl_inr ?? "—"})`).join(" · ")
    : `Waiting ladder ${ladder} (need 2+ lots)`;
  const runnerLine = runner
    ? `Left ${runner.qty} @ ${runner.price} · ${runner.reason || "—"}`
    : (outs.length ? "Trailing…" : "—");
  const optType = t.option_type || "—";
  const strike = t.strike ?? "—";
  const optTone = optType === "CE" ? "good" : optType === "PE" ? "bad" : "";
  const callName = optType === "CE" ? "CALL (CE)" : optType === "PE" ? "PUT (PE)" : "OPTION";
  const isOpen = t.status === "open";
  const exitLabel = t.raw?.exit_label || t.exit_reason || "—";
  const entry = pxCells(t, "entry");
  const stop = pxCells(t, "stop");
  const target = pxCells(t, "target");
  const exit = pxCells(t, "exit");
  const pnlU = pnlPerUnit(t);
  const qty = qtyOf(t);
  const planN = tradePlanNumbers(t);
  const trailTargetLine = !planN.desk.trailEnabled
    ? "Trailing off"
    : planN.trailAct != null
      ? `${money(planN.trailAct)} activate (${planN.desk.trailActivateR ?? "—"}R) · step ${money(planN.trailStep)} (${planN.desk.trailStepR ?? "—"}R)`
      : "—";
  el.className = "detail";
  el.innerHTML = `
    <h3>${companyOf(t)} · BUY <span class="badge ${optTone}">${optType} ${strike}</span></h3>
    <div class="badge-row">
      <span class="badge">${t.mode}</span>
      <span class="badge">${t.desk || "nifty"}</span>
      <span class="badge">${isOpen ? "RUNNING" : t.status}</span>
      <span class="badge ${optTone}">${callName} ${strike}</span>
      <span class="badge">${t.moneyness || "ATM"}</span>
      <span class="badge">Qty ${qty || "—"}</span>
      <span class="badge">Lot ${lotsOf(t)}</span>
      <span class="badge">Invested ${money(investedOf(t))}</span>
      ${so ? `<span class="badge good">Scale-out done</span>` : ""}
      <span class="badge ${((t.pnl || 0) >= 0) ? "good" : "bad"}">${t.pnl == null ? (isOpen ? "open" : "—") : money(t.pnl)}</span>
    </div>
    <div class="detail-grid">
      <div class="detail-tile">
        <em>Cash</em>
        <b>${money(planN.desk.cash, 0)}</b>
        <small>Free paper cash</small>
      </div>
      <div class="detail-tile">
        <em>Max risk</em>
        <b class="neg">${money(planN.maxRisk)}</b>
        <small>This trade loss cap · ${planN.lots} lot</small>
      </div>
      <div class="detail-tile">
        <em>Max profit</em>
        <b class="pos">${money(planN.maxProfit)}</b>
        <small>Target · R:R ${planN.rr || "—"}</small>
      </div>
      <div class="detail-tile">
        <em>Trail target</em>
        <b class="pos">${planN.desk.trailEnabled ? money(planN.trailAct) : "Off"}</b>
        <small>${planN.desk.trailEnabled ? `Activate ${planN.desk.trailActivateR ?? "—"}R · step ${planN.desk.trailStepR ?? "—"}R` : "Disabled"}</small>
      </div>
      <div class="detail-tile">
        <em>Prem entry /u</em>
        <b>${money(premiumOf(t))}</b>
        <small>e.g. buy @ 100</small>
      </div>
      <div class="detail-tile">
        <em>Prem exit /u</em>
        <b class="${(t.pnl || 0) >= 0 ? "pos" : "neg"}">${isOpen ? "—" : money(exitPremiumOf(t))}</b>
        <small>${isOpen ? "running" : "e.g. sell @ 110"}</small>
      </div>
      <div class="detail-tile">
        <em>Lot ₹ (prem × qty)</em>
        <b>${money(investedOf(t))}</b>
        <small>${qty || 0} qty invested</small>
      </div>
      <div class="detail-tile">
        <em>PnL / unit</em>
        <b class="${(t.pnl || 0) >= 0 ? "pos" : "neg"}">${pnlU == null ? "—" : money(pnlU)}</b>
        <small>Prem exit − prem entry</small>
      </div>
      <div class="detail-tile">
        <em>Stop</em>
        <b>${money(stop.unit)}</b>
        <small>${t.stop_rule || "stop"}</small>
      </div>
      <div class="detail-tile">
        <em>Target</em>
        <b>${money(target.unit)}</b>
        <small>${t.target_r ?? "—"}R</small>
      </div>
      <div class="detail-tile wide">
        <em>Net PnL</em>
        <b class="${(t.pnl || 0) >= 0 ? "pos" : "neg"}">${t.pnl == null ? (isOpen ? "open" : "—") : `${money(t.pnl)} · R=${Number(t.pnl_r || 0).toFixed(2)}`}</b>
        <small>Risk ${money(planN.maxRisk)} · Max profit ${money(planN.maxProfit)} · Trail ${money(planN.trailAct)} · spot ${num(entry.unit)} → ${isOpen ? "—" : num(exit.unit)}</small>
      </div>
    </div>
    <div class="kv">
      <span>Option</span><span><b>${callName}</b> · strike <b>${strike}</b> · ${t.moneyness || "ATM"}</span>
      <span>F&O symbol</span><span style="font-family:var(--mono);font-size:0.75rem">${t.option_symbol || "—"}</span>
      <span>Expiry</span><span>${t.expiry || "—"}</span>
      <span>Spot ref</span><span>${t.spot_ref ?? t.planned_entry ?? "—"}</span>
      <span>Company</span><span><b>${companyOf(t)}</b></span>
      <span>Qty / lot</span><span><b>${qty}</b> units · ${lotsOf(t)}</span>
      <span>Cash</span><span><b>${money(planN.desk.cash, 0)}</b></span>
      <span>Max risk</span><span><b class="neg">${money(planN.maxRisk)}</b> · ${risk}</span>
      <span>Max profit</span><span><b class="pos">${money(planN.maxProfit)}</b> (${planN.rr || "—"}R)</span>
      <span>Trail target</span><span><b>${trailTargetLine}</b></span>
      <span>Trail status</span><span>${trailLine}</span>
      <span>Scale-out</span><span>${scaleLine}</span>
      <span>Runner trail</span><span>${runnerLine}</span>
      <span>Side</span><span>${t.side || "buy"} (buy ${optType})</span>
      <span>Strategy</span><span>${t.strategy || "—"}</span>
      <span>Why</span><span>${t.why || "—"}</span>
      <span>How</span><span>${t.how || "—"}</span>
      <span>Entry time</span><span><b>${fmtTsFull(t.entry_time)}</b></span>
      <span>Exit time</span><span><b>${isOpen ? "— running —" : fmtTsFull(t.exit_time)}</b></span>
      <span>Prem entry → exit</span><span><b>${money(premiumOf(t))}</b> → <b>${isOpen ? "—" : money(exitPremiumOf(t))}</b></span>
      <span>Duration</span><span>${(!isOpen && t.entry_time && t.exit_time) ? durationLabel(t.entry_time, t.exit_time) : (isOpen ? "open" : "—")}</span>
      <span>R:R</span><span>${t.reward_risk ?? "—"}</span>
    </div>
    <strong style="font-size:0.78rem">Steps</strong>
    <ol>${steps || "<li>None</li>"}</ol>
  `;
}

function bindRows(body, trades, detailId) {
  body.querySelectorAll("tr[data-idx]").forEach((tr) => {
    tr.addEventListener("click", () => {
      body.querySelectorAll("tr").forEach((x) => x.classList.remove("active"));
      tr.classList.add("active");
      showDetail(detailId, trades[Number(tr.dataset.idx)]);
    });
  });
}

function renderOpenTable(bodyId, trades, kind, detailId) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  if (!trades.length) {
    body.innerHTML = `<tr><td colspan="11">No running trades</td></tr>`;
    return;
  }
  body.innerHTML = trades.map((t, i) => {
    const { opt, strike, cls } = optLabel(t);
    const company = companyOf(t);
    const stop = unitPrice(t, "stop");
    const target = unitPrice(t, "target");
    const delay = `style="animation-delay:${Math.min(i, 12) * 28}ms"`;
    const lead = kind === "bt"
      ? `<td>${t.session_date || "—"}</td>`
      : `<td><b>RUNNING</b></td>`;
    return `<tr data-idx="${i}" ${delay}>
      ${lead}
      <td><b title="${t.option_symbol || company}">${company}</b></td>
      <td><b class="${cls}">${opt}</b></td>
      <td><b>${strike}</b></td>
      <td><b>${fmtTsFull(t.entry_time)}</b></td>
      <td><b>${money(premiumOf(t))}</b></td>
      <td>${money(investedOf(t))}</td>
      <td>${money(stop)}</td>
      <td>${money(target)}</td>
      <td><b>${qtyOf(t) || "—"}</b></td>
      <td>${lotsOf(t)}</td>
    </tr>`;
  }).join("");
  bindRows(body, trades, detailId);
}

function renderClosedTable(bodyId, trades, kind, detailId) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  if (!trades.length) {
    body.innerHTML = `<tr><td colspan="15">No closed trades for current filters</td></tr>`;
    return;
  }
  body.innerHTML = trades.map((t, i) => {
    const pnlCls = (t.pnl || 0) >= 0 ? "pos" : "neg";
    const { opt, strike, cls } = optLabel(t);
    const company = companyOf(t);
    const exitLabel = t.raw?.exit_label || t.exit_reason || "—";
    const premIn = premiumOf(t);
    const premOut = exitPremiumOf(t);
    const pnlU = pnlPerUnit(t);
    const delay = `style="animation-delay:${Math.min(i, 16) * 22}ms"`;
    const lead = kind === "bt"
      ? `<td>${t.session_date || "—"}</td>`
      : `<td>${t.status}</td>`;
    return `<tr data-idx="${i}" ${delay}>
      ${lead}
      <td><b title="${t.option_symbol || company}">${company}</b></td>
      <td><b class="${cls}">${opt}</b></td>
      <td><b>${strike}</b></td>
      <td><b>${fmtTsFull(t.entry_time)}</b></td>
      <td><b>${money(premIn)}</b></td>
      <td><b class="${pnlCls}">${money(premOut)}</b></td>
      <td>${money(investedOf(t))}</td>
      <td><b>${fmtTsFull(t.exit_time)}</b></td>
      <td>${exitLabel}</td>
      <td><b>${qtyOf(t) || "—"}</b></td>
      <td>${lotsOf(t)}</td>
      <td class="${pnlCls}">${pnlU == null ? "—" : money(pnlU)}</td>
      <td class="${pnlCls}">${t.pnl == null ? "—" : money(t.pnl)}</td>
      <td>${t.pnl_r == null ? "—" : Number(t.pnl_r).toFixed(2)}</td>
    </tr>`;
  }).join("");
  bindRows(body, trades, detailId);
}

function paintModeCharts(prefix, stats) {
  renderEquityChart(`${prefix}EquityChart`, stats.equity_curve, `${prefix}Equity`);
  renderWinChart(`${prefix}WinChart`, stats, `${prefix}Win`);
  renderExitChart(`${prefix}ExitChart`, stats.exit_breakdown, `${prefix}Exit`);
  if (prefix === "bt") renderPnlBars("btPnlChart", stats.equity_curve, "btPnl");
}

function splitTrades(bucket) {
  if (Array.isArray(bucket)) {
    return {
      open: bucket.filter((t) => t.status === "open"),
      closed: bucket.filter((t) => t.status === "closed"),
    };
  }
  return {
    open: bucket?.open || [],
    closed: bucket?.closed || [],
  };
}

function moveTabInk() {
  const tabs = document.getElementById("modeTabs");
  const ink = document.getElementById("tabInk");
  const on = tabs.querySelector(".tab.on");
  if (!on || !ink) return;
  ink.style.width = `${on.offsetWidth}px`;
  ink.style.left = `${on.offsetLeft}px`;
}

function updateFilterMeta(s) {
  const meta = document.getElementById("filterMeta");
  const bits = [`Session: ${period}`];
  if (s?.period_start) bits.push(`from ${String(s.period_start).slice(0, 10)}`);
  if (optFilter !== "all") bits.push(optFilter);
  if (resultFilter !== "all") bits.push(resultFilter);
  if (searchQuery) bits.push(`“${searchQuery}”`);
  meta.textContent = bits.join(" · ");
}

function applyTradeViews() {
  if (!lastSummary) return;
  const s = lastSummary;
  const bt = splitTrades(s.trades.backtest);
  const paper = splitTrades(s.trades.paper);
  const live = splitTrades(s.trades.live);

  const btOpen = filterList(bt.open);
  const btClosed = filterList(bt.closed);
  const paperOpen = filterList(paper.open);
  const paperClosed = filterList(paper.closed);
  const liveOpen = filterList(live.open);
  const liveClosed = filterList(live.closed);

  document.getElementById("btOpenCount").textContent = `${btOpen.length}`;
  document.getElementById("btCount").textContent = `${btClosed.length}`;
  document.getElementById("paperOpenCount").textContent = `${paperOpen.length}`;
  document.getElementById("paperCount").textContent = `${paperClosed.length}`;
  document.getElementById("liveOpenCount").textContent = `${liveOpen.length}`;
  document.getElementById("liveCount").textContent = `${liveClosed.length}`;

  renderOpenTable("btOpenBody", btOpen, "bt", "btDetail");
  renderClosedTable("btBody", btClosed, "bt", "btDetail");
  renderOpenTable("paperOpenBody", paperOpen, "paper", "paperDetail");
  renderClosedTable("paperBody", paperClosed, "paper", "paperDetail");
  renderOpenTable("liveOpenBody", liveOpen, "live", "liveDetail");
  renderClosedTable("liveBody", liveClosed, "live", "liveDetail");
  updateFilterMeta(s);
}

async function refreshHealth() {
  const h = await api("/api/health");
  document.getElementById("pillTime").textContent = `IST ${h.time_ist?.replace("T", " ").slice(11, 19) || "—"}`;
  document.getElementById("pillLtp").textContent =
    h.nifty_ltp != null ? `NIFTY ${Number(h.nifty_ltp).toLocaleString("en-IN")}` : "NIFTY —";
  const mode = document.getElementById("pillMode");
  if (h.live_enabled && !h.paper) {
    mode.textContent = "LIVE";
    mode.className = "chip bad";
  } else {
    mode.textContent = "PAPER";
    mode.className = "chip amber";
  }
  const angel = document.getElementById("pillAngel");
  if (h.angel?.logged_in) {
    angel.textContent = "Angel OK";
    angel.className = "chip ok";
  } else {
    angel.textContent = h.angel?.credentials_present ? "Angel…" : "No .env";
    angel.className = "chip";
  }
}

function paintOverview(s) {
  const nifty = s.desks?.nifty || {};
  const stocks = s.desks?.stocks || {};
  const tone = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = money(value);
    el.classList.remove("pos", "neg");
    el.classList.add(Number(value) >= 0 ? "pos" : "neg");
  };
  tone("ovNiftyPaper", nifty.paper?.pnl);
  tone("ovNiftyBt", nifty.backtest?.pnl);
  document.getElementById("ovNiftyInv").textContent = money(s.account?.nifty_invested);
  document.getElementById("ovNiftyOpen").textContent = String(nifty.paper?.open ?? 0);
  tone("ovStocksPaper", stocks.paper?.pnl);
  tone("ovStocksBt", stocks.backtest?.pnl);
  document.getElementById("ovStocksInv").textContent = money(s.account?.stocks_invested);
  document.getElementById("ovStocksOpen").textContent = String(stocks.paper?.open ?? 0);
  document.getElementById("ovFreeCash").textContent = money(s.account?.free_cash ?? s.account?.paper_cash);
  document.getElementById("ovCapNifty").textContent = money(s.account?.nifty_invested);
  document.getElementById("ovCapStocks").textContent = money(s.account?.stocks_invested);
  document.getElementById("ovCapTotal").textContent = money(s.account?.total_capital);
}

async function refreshAll() {
  toast("Updating…");
  document.body.classList.add("is-loading");
  try {
    await refreshHealth();
    const deskQ = activeDesk === "overview" ? "overview" : activeDesk;
    const s = await api(`/api/summary?period=${period}&desk=${deskQ}`);
    lastSummary = s;
    const lot = s.config.option_lot_size || s.config.lot_size || 65;
    lotSizeHint = Number(lot) || 65;
    const strategy = s.config.strategy || "—";
    const label = s.config.desk_label || (activeDesk === "overview" ? "Overview" : activeDesk);
    document.getElementById("pillLot").textContent =
      s.config.trade_instrument === "equity" ? `Shares ×${lot}` : `Lot ${lot}`;
    document.getElementById("pillStrategy").textContent = strategy;
    document.getElementById("brandSub").textContent =
      `${label} · ${strategy} · Angel One`;

    const tone = (id, value) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = money(value);
      el.classList.remove("pos", "neg");
      el.classList.add(Number(value) >= 0 ? "pos" : "neg");
    };
    tone("mBtPnl", s.backtest.pnl);
    tone("mPaperPnl", s.paper.pnl);
    tone("mLivePnl", s.live.pnl);
    const cash = s.paper_account?.cash ?? s.account?.paper_cash;
    document.getElementById("mCash").textContent = money(cash);
    document.getElementById("mLiveArm").textContent = s.live_status.armed ? "ARMED" : "OFF";
    paintHeroMetrics();
    paintOverview(s);

    const daysInput = document.getElementById("btDays");
    if (daysInput && !daysInput.dataset.touched) {
      daysInput.value = s.config.backtest_default_days || 30;
    }

    if (activeDesk !== "overview") {
      syncChartTheme();
      renderStats("btStats", s.backtest);
      renderStats("paperStats", s.paper);
      renderStats("liveStats", s.live);
      paintModeCharts("bt", s.backtest);
      paintModeCharts("paper", s.paper);
      paintModeCharts("live", s.live);
      applyTradeViews();

      document.getElementById("btRuns").innerHTML =
        (s.recent_backtests || []).map((b, i) => `<tr style="animation-delay:${i * 30}ms">
          <td>${(b.started_at || "").replace("T", " ").slice(0, 19)}</td>
          <td>${b.strategy || "—"}</td>
          <td>${b.days}</td><td>${b.trades}</td><td>${Number(b.win_rate).toFixed(1)}%</td>
          <td class="${b.total_pnl >= 0 ? "pos" : "neg"}">${money(b.total_pnl)}</td>
          <td>${b.avg_r}</td><td>${money(b.max_drawdown)}</td><td>${b.profit_factor}</td>
        </tr>`).join("") || `<tr><td colspan="9">No runs — click Backtest</td></tr>`;

      const eventDesk = activeDesk === "stocks" ? "stocks" : "nifty";
      const events = await api(`/api/events?limit=30&desk=${eventDesk}`);
      document.getElementById("paperEvents").innerHTML = events.length
        ? events.map((e) => `<li>[${e.ts?.slice(11, 19)}] ${e.message}</li>`).join("")
        : "<li>None</li>";

      const banner = document.getElementById("liveBanner");
      banner.textContent = s.live_status.message;
      banner.className = s.live_status.armed ? "banner on" : "banner";
      document.getElementById("liveConfigLines").innerHTML =
        `desk=<code>${activeDesk}</code> · strategy=<code>${strategy}</code> · ` +
        `instrument=<code>${s.config.trade_instrument || "—"}</code> · ` +
        `lot=<code>${lot}</code> · watchlist=<code>${s.config.watchlist_n || 0}</code> · ` +
        `target ${s.config.default_target_r}R · flat ${s.config.flat_by}`;
      moveTabInk();
    }

    toast(`Ready · ${label} · ${period}`, "ok");
  } catch (e) {
    toast(String(e.message || e), "err");
  } finally {
    document.body.classList.remove("is-loading");
  }
}

document.querySelectorAll(".period").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".period").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    period = btn.dataset.period;
    refreshAll();
  });
});

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    activeMode = btn.dataset.mode;
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("on"));
    const panel = document.getElementById(`panel-${activeMode}`);
    panel.classList.add("on");
    paintHeroMetrics();
    moveTabInk();
  });
});

document.querySelectorAll("[data-filter='opt'] .fbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-filter='opt'] .fbtn").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    optFilter = btn.dataset.opt;
    applyTradeViews();
  });
});

document.querySelectorAll("[data-filter='result'] .fbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-filter='result'] .fbtn").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    resultFilter = btn.dataset.result;
    applyTradeViews();
  });
});

const searchBox = document.getElementById("searchBox");
let searchTimer;
searchBox.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchQuery = searchBox.value.trim();
    applyTradeViews();
  }, 160);
});

document.getElementById("btDays").addEventListener("change", (e) => {
  e.target.dataset.touched = "1";
});

function applyTheme(theme) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.classList.remove("theme-dark", "theme-light");
  document.documentElement.classList.add(next === "light" ? "theme-light" : "theme-dark");
  try { localStorage.setItem("nifty-desk-theme", next); } catch (e) {}
  document.querySelectorAll(".theme-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.theme === next);
  });
  syncChartTheme();
  if (lastSummary) {
    try {
      paintModeCharts("bt", lastSummary.backtest);
      paintModeCharts("paper", lastSummary.paper);
      paintModeCharts("live", lastSummary.live);
    } catch (e) {
      console.warn("chart theme refresh", e);
    }
  }
}

document.querySelectorAll(".theme-btn").forEach((btn) => {
  btn.addEventListener("click", () => applyTheme(btn.dataset.theme));
});
applyTheme(currentTheme());

document.querySelectorAll(".desk-btn").forEach((btn) => {
  btn.addEventListener("click", () => setActiveDesk(btn.dataset.desk));
});
document.querySelectorAll("[data-open-desk]").forEach((btn) => {
  btn.addEventListener("click", () => setActiveDesk(btn.dataset.openDesk));
});

document.getElementById("btnRefresh").addEventListener("click", refreshAll);
document.getElementById("btnBacktest").addEventListener("click", async () => {
  if (activeDesk === "overview") {
    toast("Open Nifty or Stocks desk to run backtest", "err");
    return;
  }
  const btn = document.getElementById("btnBacktest");
  const days = Math.max(5, Math.min(120, Number(document.getElementById("btDays").value) || 30));
  setBusy(btn, true);
  toast(`Backtest · ${activeDesk} · ${days}d…`);
  try {
    const payload = { days, desk: activeDesk };
    if (activeDesk === "nifty") payload.symbols = ["NIFTY"];
    const r = await api("/api/backtest/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(`Done · ${r.trades} trades · ${money(r.total_pnl)} · ${r.strategy || ""}`, "ok");
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    setBusy(btn, false);
  }
});
document.getElementById("btnPaper").addEventListener("click", async () => {
  if (activeDesk === "overview") {
    toast("Open Nifty or Stocks desk for Paper tick", "err");
    return;
  }
  toast(`Paper tick · ${activeDesk}…`);
  try {
    const r = await api("/api/paper/tick", {
      method: "POST",
      body: JSON.stringify({ desk: activeDesk }),
    });
    toast(
      `[${r.desk || activeDesk}] open ${r.opened?.length || 0} · close ${r.closed?.length || 0} · cash ${money(r.cash)}`,
      "ok",
    );
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  }
});
const resetModal = document.getElementById("resetModal");
const resetModalDesk = document.getElementById("resetModalDesk");

function syncResetRadioUI() {
  resetModal.querySelectorAll(".reset-option").forEach((label) => {
    const input = label.querySelector('input[name="resetMode"]');
    label.classList.toggle("is-selected", !!(input && input.checked));
  });
}

function openResetModal() {
  const desk = activeDesk === "overview" ? "all" : activeDesk;
  const deskLabel =
    desk === "all" ? "All desks (Nifty + Stocks)" : desk === "stocks" ? "Stocks desk" : "Nifty desk";
  resetModalDesk.textContent = `Scope: ${deskLabel}`;
  const paperRadio = resetModal.querySelector('input[name="resetMode"][value="paper"]');
  if (paperRadio) paperRadio.checked = true;
  syncResetRadioUI();
  resetModal.hidden = false;
  resetModal.classList.add("is-open");
  document.body.style.overflow = "hidden";
  document.getElementById("resetModalConfirm")?.focus();
}

function closeResetModal() {
  resetModal.classList.remove("is-open");
  resetModal.hidden = true;
  document.body.style.overflow = "";
}

async function confirmResetModal() {
  const desk = activeDesk === "overview" ? "all" : activeDesk;
  const selected = resetModal.querySelector('input[name="resetMode"]:checked');
  const mode = selected?.value || "paper";
  const btn = document.getElementById("resetModalConfirm");
  btn.disabled = true;
  try {
    const r = await api("/api/reset", {
      method: "POST",
      body: JSON.stringify({ desk, mode }),
    });
    closeResetModal();
    toast(r.message, "ok");
    await refreshAll();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("btnReset").addEventListener("click", openResetModal);
document.getElementById("resetModalCancel").addEventListener("click", closeResetModal);
document.getElementById("resetModalClose").addEventListener("click", closeResetModal);
document.getElementById("resetModalConfirm").addEventListener("click", confirmResetModal);
resetModal.querySelectorAll('input[name="resetMode"]').forEach((input) => {
  input.addEventListener("change", syncResetRadioUI);
});
resetModal.addEventListener("click", (e) => {
  if (e.target === resetModal) closeResetModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !resetModal.hidden) closeResetModal();
});

window.addEventListener("resize", moveTabInk);
setActiveDesk("overview");
requestAnimationFrame(moveTabInk);
setInterval(refreshHealth, 30000);
