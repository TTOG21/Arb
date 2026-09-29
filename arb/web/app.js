"use strict";

// Dashboard for the arbitrage desk. It only calls the existing API endpoints.
// Every value from the API is rendered as text, never as HTML.

const $ = (selector) => document.querySelector(selector);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!response.ok) throw new Error(describeError(body, response.status));
  return body;
}

function describeError(body, status) {
  if (body && Array.isArray(body.detail)) {
    return body.detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("\n");
  }
  if (body && body.detail) return String(body.detail);
  return `HTTP ${status}`;
}

const TONE = {
  PAPER_CANDIDATE: "good", VALIDATED_FOR_PAPER: "good", FEASIBLE_FOR_PAPER: "good", PASS_FOR_PAPER: "good",
  CANDIDATE: "good", running: "good", VERIFIED: "good", OK: "good", PAPER_RESULT: "good",
  RESEARCH_ONLY: "warn", INCONCLUSIVE: "warn", NEEDS_ENGINEERING: "warn", CONDITIONAL_FOR_PAPER: "warn",
  MISSING: "warn", CONDITION: "warn", HYPOTHETICAL: "warn", UNVERIFIED: "warn", PAPER_SKIPPED: "warn",
  NO_TRADE: "bad", REJECTED: "bad", BLOCKED: "bad", VETO: "bad", FAIL: "bad", NO_CANDIDATE: "bad",
  BREACHED: "bad", ERROR: "bad", HALT: "bad", PAPER_HALTED: "bad",
  INFO: "info", ESTIMATED: "info", RESEARCH: "info", PAPER: "info", OPPORTUNITY_OBSERVED: "info", CONFIG_CHANGED: "info",
};
const SEVERITY_ORDER = { FAIL: 0, MISSING: 1, CONDITION: 2, INFO: 3 };

const badge = (value) => h("span", { class: `badge ${TONE[value] || "muted"}` }, value ?? "—");

function trimDecimal(text, max = 8) {
  if (text === null || text === undefined) return "—";
  const [whole, fraction] = String(text).split(".");
  if (!fraction || fraction.length <= max) return String(text);
  const kept = fraction.slice(0, max).replace(/0+$/, "");
  return `${whole}${kept ? "." + kept : ""}…`;
}
const moneyText = (m) => (m ? `${trimDecimal(m.amount)} ${m.asset}` : "—");
const bpsText = (value) => (value === null || value === undefined ? "—" : `${trimDecimal(value, 2)} bps`);

let displayZone = "Europe/Berlin";
function when(iso) {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("el-GR", { dateStyle: "short", timeStyle: "medium", timeZone: displayZone }).format(new Date(iso));
  } catch {
    return iso;
  }
}

// replaceChildren would print null/false as text; drop them first.
function fill(target, ...children) {
  target.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

const card = (title, ...children) => h("div", { class: "card" }, h("h2", {}, title), ...children);
const wideCard = (title, ...children) => h("div", { class: "card wide" }, h("h2", {}, title), ...children);
const empty = (text) => h("div", { class: "empty" }, text);

function kv(rows) {
  return h("table", { class: "kv" }, rows.filter(Boolean).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, v ?? "—"))));
}

function table(headers, rows, onRowClick) {
  if (!rows.length) return empty("Τίποτα ακόμα.");
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, headers.map((head) => h("th", {}, head)))),
      h(
        "tbody",
        {},
        rows.map((cells, i) =>
          h("tr", onRowClick ? { class: "clickable", onclick: () => onRowClick(i) } : {}, cells.map((c) => h("td", {}, c ?? "—"))),
        ),
      ),
    ),
  );
}

let noticeTimer = null;
function notify(message, isError = false) {
  const box = $("#notice");
  box.textContent = message;
  box.className = isError ? "notice error" : "notice";
  box.hidden = false;
  clearTimeout(noticeTimer);
  if (!isError) noticeTimer = setTimeout(() => (box.hidden = true), 5000);
}

async function guarded(action) {
  try {
    await action();
  } catch (error) {
    notify(error.message || String(error), true);
  }
}

// ---------------------------------------------------------------- tabs

const TABS = ["overview", "scan", "paper", "journal", "config"];
let currentTab = "overview";

function showTab(name) {
  currentTab = TABS.includes(name) ? name : "overview";
  for (const tab of TABS) $(`#tab-${tab}`).hidden = tab !== currentTab;
  for (const button of document.querySelectorAll("nav [data-tab]")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === currentTab));
  }
  if (location.hash.slice(1) !== currentTab) history.replaceState(null, "", `#${currentTab}`);
  if (currentTab === "config") guarded(loadConfig);
  else refreshCurrent();
}

function refreshCurrent() {
  const refresh = { overview: refreshOverview, paper: refreshPaper, journal: refreshJournal }[currentTab];
  if (refresh) guarded(refresh);
}

// ---------------------------------------------------------------- overview

async function refreshOverview() {
  const [caps, opportunities] = await Promise.all([api("/desk/capabilities"), api("/opportunities")]);
  renderHeader(caps);
  renderScreener(caps.screener);
  renderMarket(caps.tools.exchange_market_data);
  renderOpportunities(opportunities.opportunities);
  renderMissing(caps.missing_inputs);
}

function renderHeader(caps) {
  const market = caps.tools.exchange_market_data;
  fill($("#header-meta"), 
    badge(caps.mode),
    h("span", { class: "muted" }, `config v${caps.config_version}`),
    h("span", { class: "muted" }, `δεδομένα: ${market.source}`),
    badge(market.evidence_label),
  );
}

function renderScreener(status) {
  const start = h("button", { onclick: () => guarded(async () => {
    await api("/screener/start", { method: "POST" });
    notify("Ο screener ξεκίνησε.");
    await refreshOverview();
  }) }, "Εκκίνηση");
  const stop = h("button", { class: "secondary", onclick: () => guarded(async () => {
    await api("/screener/stop", { method: "POST" });
    notify("Ο screener σταμάτησε.");
    await refreshOverview();
  }) }, "Διακοπή");
  fill($("#screener-card"), 
    h("h2", {}, "Screener"),
    h("div", { class: "row" }, badge(status.status), status.paper_halted ? badge("PAPER_HALTED") : null, h("span", { class: "muted" }, `mode ${status.mode}`)),
    kv([
      ["Κύκλοι", status.cycles_completed],
      ["Τελευταίος κύκλος", when(status.last_cycle_finished_at)],
      ["Διάστημα", `${status.interval_seconds} s`],
      ["Jobs", `${status.jobs.triangular} triangular, ${status.jobs.cross_exchange} cross-exchange`],
      ["Ενεργές διαδρομές", status.active_pairs],
      ["Τελευταίο σφάλμα", status.last_error || "—"],
      status.paper_halt_reason ? ["Paper halt", status.paper_halt_reason] : null,
    ]),
    h("h3", {}, "Αποτελέσματα τελευταίου κύκλου"),
    table(["Job", "Απόφαση", "Λόγος"], status.last_results.map((r) => [`${r.job} ${r.target}`, badge(r.decision), r.reason])),
    h("div", { class: "row" }, start, stop),
    h("p", { class: "muted" }, status.note),
  );
}

function renderMarket(market) {
  const reach = market.reachability || {};
  const venues = Object.entries(reach.venues || {});
  const streams = Object.entries(reach.streams || {}).flatMap(([venue, symbols]) =>
    Object.entries(symbols).map(([symbol, s]) => [venue, symbol, badge(s.ready ? "OK" : "UNVERIFIED"), s.updates, s.last_update_age_ms, s.rebuilds, s.last_error || "—"]),
  );
  fill($("#market-card"), 
    h("h2", {}, "Δεδομένα αγοράς"),
    h("div", { class: "row" }, h("span", {}, market.source), badge(market.evidence_label)),
    h("p", { class: "muted" }, market.detail),
    h("h3", {}, "Ανταλλακτήρια"),
    venues.length
      ? table(["Venue", "Τελευταία επιτυχία", "Τελευταίο σφάλμα"], venues.map(([v, s]) => [v, when(s.last_success_at), s.last_error || "—"]))
      : empty("Δεν έχει γίνει ακόμα κλήση σε ανταλλακτήριο."),
    reach.streams !== undefined ? h("h3", {}, "Streams") : null,
    reach.streams !== undefined ? table(["Venue", "Ζεύγος", "Κατάσταση", "Updates", "Ηλικία ms", "Rebuilds", "Σφάλμα"], streams) : null,
  );
}

function renderOpportunities(list) {
  fill($("#opportunities-card"), 
    h("h2", {}, "Ευκαιρίες που παρακολουθούνται"),
    table(
      ["Διαδρομή", "Στρατηγική", "Απόφαση", "Παρατηρήσεις", "Τελευταία φορά"],
      list.map((o) => [o.route, o.strategy, badge(o.decision), o.observations, when(o.last_seen)]),
      (i) => guarded(() => openOpportunity(list[i].opportunity_id)),
    ),
    list.length ? h("p", { class: "muted" }, "Πάτα μια γραμμή για όλες τις λεπτομέρειες.") : null,
  );
}

function renderMissing(gaps) {
  fill($("#missing-card"), 
    h("h2", {}, `Τι λείπει για PAPER_CANDIDATE (${gaps.length})`),
    gaps.length
      ? h("ul", { class: "plain" }, gaps.map((g) => h("li", {}, h("span", { class: "mono" }, g.field), " — ", g.why)))
      : empty("Όλα τα απαιτούμενα πεδία έχουν συμπληρωθεί."),
    h("p", { class: "muted" }, "Συμπληρώνονται στην καρτέλα Ρυθμίσεις."),
  );
}

async function openOpportunity(id) {
  const data = await api(`/opportunities/${encodeURIComponent(id)}`);
  showTab("scan");
  const packet = data.packet;
  renderResult({ ...data.summary, config_version: packet.config_version, opportunity: packet, summary: data.summary }, `Ευκαιρία ${id}`);
}

// ---------------------------------------------------------------- scan

async function submitScan(event, path, fields) {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector("button");
  const params = new URLSearchParams();
  for (const name of fields) params.set(name, form.elements[name].value.trim());
  button.disabled = true;
  fill($("#scan-result"), h("div", { class: "card" }, "Σάρωση σε εξέλιξη…"));
  try {
    renderResult(await api(`${path}?${params}`), "Αποτέλεσμα σάρωσης");
  } catch (error) {
    fill($("#scan-result"));
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderResult(d, title) {
  const summary = d.summary || {};
  const parts = [
    wideCard(
      title,
      h("div", { class: "row" }, badge(d.decision), d.feasibility ? badge(d.feasibility) : null, h("span", { class: "muted" }, `εκτέλεση: ${d.execution_status || "NO_TRADE"}`)),
      h("p", { class: "reason" }, d.reason),
      kv([
        ["Στρατηγική", d.strategy],
        d.venue ? ["Ανταλλακτήριο", d.venue] : null,
        d.symbol ? ["Ζεύγος / ανταλλακτήρια", `${d.symbol} @ ${(d.venues || []).join(", ")}`] : null,
        summary.route ? ["Διαδρομή", summary.route] : null,
        d.routes_checked !== undefined ? ["Διαδρομές που ελέγχθηκαν", d.routes_checked] : null,
        d.market_data ? ["Δεδομένα αγοράς", `${d.market_data.source} (${d.market_data.evidence_label})`] : null,
        d.config_version ? ["Config", `v${d.config_version}, ${d.mode}`] : null,
        summary.evaluated_at ? ["Αξιολόγηση", when(summary.evaluated_at)] : null,
        summary.next_action ? ["Επόμενο βήμα", summary.next_action] : null,
      ]),
    ),
  ];
  if (d.opportunity) parts.push(...renderPacket(d.opportunity));
  if (d.leads && d.leads.length) parts.push(leadsCard(d.leads));
  if (d.rejected_pairs && d.rejected_pairs.length) parts.push(rejectedCard(d.rejected_pairs));
  if (d.candidates && d.candidates.length > 1) parts.push(candidatesCard(d.candidates));
  fill($("#scan-result"), h("div", { class: "grid" }, parts));
}

function renderPacket(p) {
  const v = p.vector_result;
  const findings = [p.scout_result, v, p.relay_result, p.aegis_verdict]
    .flatMap((r) => (r && r.findings) || [])
    .sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);
  const cards = [
    card(
      "Ρόλοι",
      table(["Ρόλος", "Αποτέλεσμα"], [
        ["SCOUT", badge(p.scout_result.status)],
        ["VECTOR", badge(v.status)],
        ["RELAY", badge(p.relay_result.status)],
        ["AEGIS", badge(p.aegis_verdict.status)],
        ["ATLAS", badge(p.final_decision)],
      ]),
      h("p", { class: "muted" }, "Ντετερμινιστικοί έλεγχοι σε μία διεργασία, όχι ανεξάρτητοι agents."),
    ),
    card(
      "Οικονομικά (VECTOR)",
      kv([
        ["Μέγεθος: ζητήθηκε / εφικτό", `${moneyText(p.requested_size)} / ${moneyText(p.feasible_size)}`],
        ["Gross", `${moneyText(p.gross_capture)} (${bpsText(v.gross_bps)})`],
        ["Conditional net", `${moneyText(p.conditional_net)} (${bpsText(v.conditional_bps)})`],
        ["Conservative net", `${moneyText(p.conservative_net)} (${bpsText(v.conservative_bps)})`],
        ["Expected net", `— ${p.null_reasons.expected_net || ""}`],
        ["Breakeven fee ανά leg", bpsText(v.breakeven_fee_bps_per_leg)],
        ["Μεγαλύτερη ευαισθησία", v.sensitivity ? v.sensitivity.biggest : "—"],
        ["Evidence", p.evidence_labels.packet],
      ]),
    ),
  ];
  if (p.execution_plan) {
    cards.push(wideCard(
      "Σχέδιο εντολών (RELAY, μόνο για paper)",
      table(
        ["Venue", "Πλευρά", "Ζεύγος", "Ποσότητα", "Limit", "TIF", "Fee"],
        p.execution_plan.map((l) => [l.venue, l.side, l.symbol, trimDecimal(l.amount_base), trimDecimal(l.limit_price),
          l.time_in_force, l.expected_fee.amount ? `${trimDecimal(l.expected_fee.amount)} ${l.expected_fee.asset}` : "άγνωστο"]),
      ),
    ));
  }
  if (p.stress_scenarios) {
    cards.push(wideCard(
      "Stress σενάρια (δεν είναι μέγιστη ζημιά)",
      table(
        ["Σενάριο", "Εκτεθειμένο", "Αξία τώρα", "Αποτέλεσμα", "Ζημιά"],
        p.stress_scenarios.map((s) => [h("span", { class: "mono" }, s.scenario), moneyText(s.unmatched_exposure), moneyText(s.exposure_value_now),
          moneyText(s.net_result), s.loss ? moneyText(s.loss) : "άγνωστη"]),
      ),
    ));
  }
  cards.push(wideCard(
    `Ευρήματα (${findings.length})`,
    findings.length
      ? h("div", {}, findings.map((f) => h("div", { class: "finding" }, badge(f.severity), h("span", { class: "mono" }, `${f.role} ${f.code}`), h("span", {}, f.detail))))
      : empty("Κανένα εύρημα."),
  ));
  cards.push(wideCard(
    "Order books",
    table(
      ["Ζεύγος", "Πηγή", "Ηλικία ms", "Έγκυρο", "Ορατά επίπεδα", "Checksum"],
      p.book_integrity.map((b, i) => [b.symbol, b.source || "—", p.book_age_ms[i], badge(b.valid ? "OK" : "FAIL"),
        p.book_depth_by_leg[i] ? p.book_depth_by_leg[i].levels_visible : "—", b.checksum]),
    ),
    h("p", { class: "muted" }, `Skew ανάμεσα στα legs: ${p.snapshot_skew_ms} ms (όριο ${p.skew_limit_ms ?? "δεν έχει οριστεί"}), όριο ηλικίας ${p.freshness_limit_ms ?? "δεν έχει οριστεί"} ms.`),
  ));
  const limits = p.aegis_verdict.limits_checked || [];
  if (limits.length) {
    cards.push(wideCard(
      "Όρια (AEGIS)",
      table(["Όριο", "Κατάσταση", "Μετρήθηκε", "Όριο"], limits.map((l) => [l.limit + (l.venue ? ` (${l.venue})` : ""), badge(l.status),
        l.observed ? moneyText(l.observed) : (l.share ?? "—"), l.limit_value && l.limit_value.amount ? moneyText(l.limit_value) : (l.limit_value ?? "—")])),
    ));
  }
  return cards;
}

function leadsCard(leads) {
  return wideCard(
    "Κορυφαίες διαδρομές από τα tickers (research leads)",
    table(["Διαδρομή", "Gross στην κορυφή του βιβλίου"], leads.map((l) => [l.route, bpsText(l.top_of_book_gross_bps)])),
    h("p", { class: "muted" }, leads[0].note),
  );
}

function rejectedCard(pairs) {
  return wideCard(
    "Ζεύγη ανταλλακτηρίων που απορρίφθηκαν",
    table(["Αγορά", "Πώληση", "Εμφανές spread"], pairs.map((p) => [p.buy, p.sell, p.displayed_spread ? moneyText(p.displayed_spread) : p.reason])),
  );
}

function candidatesCard(candidates) {
  return wideCard(
    "Όλοι οι υποψήφιοι",
    table(["Διαδρομή", "Απόφαση", "Conditional net", "Conservative net"], candidates.map((c) => [c.route, badge(c.decision), moneyText(c.conditional_net), moneyText(c.conservative_net)])),
  );
}

// ---------------------------------------------------------------- paper and journal

async function refreshPaper() {
  const [portfolio, trades] = await Promise.all([api("/paper/portfolio"), api("/get_trade_log?limit=50")]);
  const reset = h("button", { class: "danger", onclick: () => guarded(async () => {
    if (!confirm("Να ξαναστηθεί το paper portfolio από τα balances_by_venue των ρυθμίσεων;")) return;
    await api("/paper/reset", { method: "POST" });
    notify("Το paper portfolio ξαναστήθηκε.");
    await refreshPaper();
  }) }, "Επαναφορά paper portfolio");
  const rows = Object.entries(portfolio.balances || {}).flatMap(([venue, held]) => Object.entries(held).map(([asset, amount]) => [venue, asset, trimDecimal(amount)]));
  fill($("#portfolio-card"), 
    h("h2", {}, "Paper portfolio"),
    portfolio.balances
      ? table(["Venue", "Asset", "Υπόλοιπο"], rows)
      : empty("Δεν έχει στηθεί ακόμα. Στήνεται από τα balances_by_venue των ρυθμίσεων όταν ξεκινάς τον screener σε PAPER mode, ή με το κουμπί επαναφοράς."),
    h("p", { class: "muted" }, portfolio.note),
    portfolio.seeded_at ? h("p", { class: "muted" }, `Στήθηκε ${when(portfolio.seeded_at)} από config v${portfolio.seeded_from_config_version}.`) : null,
    h("div", { class: "row" }, reset),
  );
  fill($("#trades-card"), 
    h("h2", {}, "Paper συναλλαγές"),
    table(
      ["Ώρα", "Διαδρομή", "Planned", "Realized", "Legs", "Προβλήματα"],
      trades.logs.map((t) => [when(t.ts), t.route, moneyText(t.planned_net), moneyText(t.realized_net), t.fills.length, (t.problems || []).join("; ") || "—"]),
    ),
    h("p", { class: "muted" }, trades.note),
  );
}

function journalText(e) {
  switch (e.kind) {
    case "OPPORTUNITY_OBSERVED": return `${e.route}: ${e.decision}. ${e.reason}`;
    case "OPPORTUNITY_CLOSED": return `${e.route}: έκλεισε μετά από ${e.observations} παρατηρήσεις`;
    case "NO_CANDIDATE": return `${e.scope}: ${e.code}. ${e.detail}`;
    case "ERROR": return `${e.scope}: ${e.error}`;
    case "CONFIG_CHANGED": return `v${e.config_version}: ${(e.changed_fields || []).join(", ")}`;
    case "PAPER_RESULT": return `${e.route}: realized ${moneyText(e.realized_net)}, planned ${moneyText(e.planned_net)}`;
    case "PAPER_SKIPPED": return e.reason;
    case "HALT": return `${e.trigger}: ${e.detail}`;
    case "SCREENER_STARTED": return `mode ${e.mode}, config v${e.config_version}`;
    case "SCREENER_STOPPED": return `μετά από ${e.cycles_completed} κύκλους`;
    case "PAPER_PORTFOLIO_SEEDED": return `από config v${e.config_version}`;
    default: return "";
  }
}

async function refreshJournal() {
  const data = await api("/journal?limit=100");
  fill($("#journal-card"), 
    h("h2", {}, "Ημερολόγιο αποφάσεων"),
    table(["Ώρα", "Τύπος", "Περιγραφή"], data.entries.map((e) => [when(e.ts), badge(e.kind), journalText(e)])),
  );
}

// ---------------------------------------------------------------- config

async function loadConfig() {
  const data = await api("/config");
  displayZone = data.config.display_timezone || displayZone;
  $("#config-text").value = JSON.stringify(data.config, null, 2);
  $("#config-meta").textContent = `Έκδοση ${data.config_version}` + (data.updated_at ? `, τελευταία αλλαγή ${when(data.updated_at)}` : "");
  $("#config-errors").hidden = true;
}

async function saveConfig() {
  const errors = $("#config-errors");
  let parsed;
  try {
    parsed = JSON.parse($("#config-text").value);
  } catch (error) {
    errors.textContent = `Μη έγκυρο JSON: ${error.message}`;
    errors.hidden = false;
    return;
  }
  try {
    const result = await api("/config", { method: "PUT", body: JSON.stringify(parsed) });
    notify(result.changed_fields.length ? `Αποθηκεύτηκε ως έκδοση ${result.config_version}.` : "Καμία αλλαγή.");
    await loadConfig();
  } catch (error) {
    errors.textContent = error.message;
    errors.hidden = false;
  }
}

async function prefillScanForms() {
  const { config } = await api("/config");
  displayZone = config.display_timezone || displayZone;
  const tri = config.scan.triangular_jobs[0];
  const cross = config.scan.cross_exchange_jobs[0];
  const fill = (form, values) => {
    for (const [name, value] of Object.entries(values)) if (!form.elements[name].value) form.elements[name].value = value;
  };
  if (tri) fill($("#scan-triangular"), { venue: tri.venue, start_asset: tri.start_asset, size: tri.size });
  if (cross) fill($("#scan-cross"), { symbol: cross.symbol, venues: cross.venues.join(","), size: cross.size });
}

// ---------------------------------------------------------------- start

for (const button of document.querySelectorAll("nav [data-tab]")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}
$("#scan-triangular").addEventListener("submit", (e) => submitScan(e, "/find_triangular_arbitrage", ["venue", "start_asset", "size"]));
$("#scan-cross").addEventListener("submit", (e) => submitScan(e, "/find_cross_exchange_arbitrage", ["symbol", "venues", "size"]));
$("#config-save").addEventListener("click", () => guarded(saveConfig));
$("#config-reload").addEventListener("click", () => guarded(loadConfig));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

guarded(prefillScanForms);
showTab(location.hash.slice(1) || "overview");
setInterval(() => {
  if (!document.hidden) refreshCurrent();
}, 5000);
