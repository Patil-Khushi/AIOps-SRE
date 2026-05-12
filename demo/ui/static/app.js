// Adaptive AIOps demo UI — vanilla JS, no framework, no build step.
// Talks to demo/ui/server.py via fetch().

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ──────────────────────────────────────────────────────────────────────────
// Status strip — health probe on load, then every 30 s
// ──────────────────────────────────────────────────────────────────────────

async function refreshHealth() {
  try {
    const r = await fetch("/api/health");
    if (!r.ok) throw new Error(`health http ${r.status}`);
    const h = await r.json();
    setChip("status-llm", `LLM: ${h.llm_provider}`, h.llm_provider === "stub" ? "warn" : "ok");
    setChip("status-prom",  `Prometheus: ${h.prometheus_reachable ? "ok" : "down"}`, h.prometheus_reachable ? "ok" : "bad");
    setChip("status-jaeger", `Jaeger: ${h.jaeger_reachable ? "ok" : "down"}`, h.jaeger_reachable ? "ok" : "bad");
    $("#status-time").textContent = new Date(h.checked_at).toLocaleTimeString();
  } catch (err) {
    setChip("status-llm", "backend down", "bad");
    setChip("status-prom", "—", "bad");
    setChip("status-jaeger", "—", "bad");
    $("#status-time").textContent = "—";
  }
}

function setChip(id, text, state) {
  const el = $("#" + id);
  el.textContent = text;
  el.className = "status-chip " + (state || "");
}

// ──────────────────────────────────────────────────────────────────────────
// Fixtures pane
// ──────────────────────────────────────────────────────────────────────────

async function loadFixtures() {
  const list = $("#fixtures-list");
  list.innerHTML = '<div class="empty">loading…</div>';
  try {
    const data = await (await fetch("/api/fixtures")).json();
    const cases = data.cases || [];
    if (!cases.length) {
      list.innerHTML = '<div class="empty">no fixtures found</div>';
      return;
    }
    list.innerHTML = "";
    for (const c of cases) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `
        <div class="card-title">${escapeHtml(c.id)}</div>
        <div class="card-desc">${escapeHtml(c.description || "")}</div>
        <div class="card-meta">service=${escapeHtml(c.input?.service || "?")} · value=${escapeHtml(String(c.input?.value ?? "?"))}</div>
      `;
      card.addEventListener("click", () => triageFixture(c.id, card));
      list.appendChild(card);
    }
  } catch (err) {
    list.innerHTML = `<div class="empty">error loading fixtures: ${escapeHtml(err.message)}</div>`;
  }
}

async function triageFixture(id, card) {
  card.classList.add("loading");
  showLoadingVerdict(`triaging fixture: ${id}…`);
  try {
    const r = await fetch(`/api/triage/fixture/${encodeURIComponent(id)}`, { method: "POST" });
    if (!r.ok) throw new Error(`http ${r.status}: ${await r.text()}`);
    const verdict = await r.json();
    renderVerdict(verdict, { source: `fixture: ${id}` });
  } catch (err) {
    showError(err.message);
  } finally {
    card.classList.remove("loading");
  }
}

// ──────────────────────────────────────────────────────────────────────────
// Scenarios pane (flagd inject / reset)
// ──────────────────────────────────────────────────────────────────────────

let _pollTimers = new Map();  // scenario_id -> timer handle

async function loadScenarios() {
  const list = $("#scenarios-list");
  list.innerHTML = '<div class="empty">loading…</div>';
  try {
    const data = await (await fetch("/api/scenarios")).json();
    const items = data.scenarios || [];
    list.innerHTML = "";
    for (const s of items) {
      const card = document.createElement("div");
      card.className = "card scenario";
      card.dataset.scenarioId = s.scenario_id;
      const isOn = s.current_variant === "on";
      card.innerHTML = `
        <div class="card-title">
          ${escapeHtml(s.title)}
          ${isOn ? '<span class="chip suppressed">ON</span>' : '<span class="chip">off</span>'}
        </div>
        <div class="card-desc">${escapeHtml(s.description)}</div>
        <div class="card-meta">flag=${escapeHtml(s.flag)} · expected_alert=${escapeHtml(s.alert)} · ETA ~${s.eta_seconds}s</div>
        <div class="row" style="margin-top:8px">
          <button class="primary btn-inject" data-scenario-id="${escapeAttr(s.scenario_id)}" data-alert="${escapeAttr(s.alert)}" ${isOn ? "disabled" : ""}>Inject</button>
          <button class="btn-reset" data-scenario-id="${escapeAttr(s.scenario_id)}" ${isOn ? "" : "disabled"}>Reset</button>
          <span class="scenario-status" data-scenario-id="${escapeAttr(s.scenario_id)}"></span>
        </div>
      `;
      list.appendChild(card);
    }
    list.querySelectorAll(".btn-inject").forEach((b) =>
      b.addEventListener("click", (e) => injectScenario(b.dataset.scenarioId, b.dataset.alert))
    );
    list.querySelectorAll(".btn-reset").forEach((b) =>
      b.addEventListener("click", () => resetScenario(b.dataset.scenarioId))
    );
  } catch (err) {
    list.innerHTML = `<div class="empty">error: ${escapeHtml(err.message)}</div>`;
  }
}

async function injectScenario(scenarioId, alertName) {
  setScenarioStatus(scenarioId, "injecting flag…");
  try {
    const r = await fetch(`/api/scenarios/${scenarioId}/inject`, { method: "POST" });
    if (!r.ok) throw new Error(`http ${r.status}: ${await r.text()}`);
    const result = await r.json();
    setScenarioStatus(scenarioId, `flag ${result.flag}=on  ·  waiting for ${alertName}…`);
    await loadScenarios();   // refresh the card to show "ON"
    showLoadingVerdict(`scenario ${scenarioId} injected. Polling Prometheus for ${alertName}…`);
    pollForAlert(scenarioId, alertName, result.eta_seconds || 120);
  } catch (err) {
    setScenarioStatus(scenarioId, `error: ${err.message}`, true);
  }
}

async function resetScenario(scenarioId) {
  setScenarioStatus(scenarioId, "resetting flag…");
  // Stop any active poll for this scenario.
  if (_pollTimers.has(scenarioId)) {
    clearInterval(_pollTimers.get(scenarioId));
    _pollTimers.delete(scenarioId);
  }
  try {
    const r = await fetch(`/api/scenarios/${scenarioId}/reset`, { method: "POST" });
    if (!r.ok) throw new Error(`http ${r.status}: ${await r.text()}`);
    setScenarioStatus(scenarioId, "flag → off");
    await loadScenarios();
  } catch (err) {
    setScenarioStatus(scenarioId, `error: ${err.message}`, true);
  }
}

function pollForAlert(scenarioId, alertName, etaSeconds) {
  if (_pollTimers.has(scenarioId)) clearInterval(_pollTimers.get(scenarioId));
  const startedAt = Date.now();
  const maxMs = (etaSeconds + 60) * 1000;
  let tick = 0;
  const interval = setInterval(async () => {
    tick++;
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    setScenarioStatus(scenarioId, `polling Prometheus… ${elapsed}s elapsed (poll #${tick})`);
    try {
      const data = await (await fetch("/api/live-alerts")).json();
      const match = (data.alerts || []).find((a) => (a.metric || "").includes(alertName));
      if (match) {
        clearInterval(interval);
        _pollTimers.delete(scenarioId);
        setScenarioStatus(scenarioId, `alert ${alertName} fired — triaging…`);
        await refreshLive();        // refresh the live-alerts cards too
        // auto-triage the matched alert
        const t = await fetch("/api/triage", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ alert: match }),
        });
        if (t.ok) {
          const verdict = await t.json();
          renderVerdict(verdict, { source: `scenario: ${scenarioId} → ${alertName}` });
          setScenarioStatus(scenarioId, `verdict ready (${verdict.severity})`);
        } else {
          setScenarioStatus(scenarioId, `triage failed: ${t.status}`, true);
        }
        return;
      }
    } catch (err) {
      // Keep polling — Prometheus pf might briefly blip.
    }
    if (Date.now() - startedAt > maxMs) {
      clearInterval(interval);
      _pollTimers.delete(scenarioId);
      setScenarioStatus(scenarioId, `timed out after ${Math.round(maxMs/1000)}s — alert never fired. Check rule thresholds.`, true);
    }
  }, 10_000);
  _pollTimers.set(scenarioId, interval);
}

function setScenarioStatus(scenarioId, text, isError = false) {
  const el = document.querySelector(`.scenario-status[data-scenario-id="${scenarioId}"]`);
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? "var(--bad)" : "var(--text-dim)";
}

// ──────────────────────────────────────────────────────────────────────────
// Live alerts pane
// ──────────────────────────────────────────────────────────────────────────

async function refreshLive() {
  const list = $("#live-list");
  list.innerHTML = '<div class="empty">fetching from Prometheus…</div>';
  try {
    const data = await (await fetch("/api/live-alerts")).json();
    const alerts = data.alerts || [];
    if (!alerts.length) {
      list.innerHTML = '<div class="empty">no firing alerts. Toggle one at flagd UI ↗ and wait ~60 s.</div>';
      return;
    }
    list.innerHTML = "";
    alerts.forEach((a, idx) => {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `
        <div class="card-title">${escapeHtml(a.metric)}</div>
        <div class="card-desc">${escapeHtml(a.service)} — value ${escapeHtml(String(a.value))}${a.severity_hint ? ` · <span class="chip">${escapeHtml(a.severity_hint)}</span>` : ""}</div>
        <div class="card-meta">${escapeHtml(a.alert_id)}</div>
      `;
      card.addEventListener("click", () => triageLiveOne(a, card));
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<div class="empty">error: ${escapeHtml(err.message)}</div>`;
  }
}

async function triageLiveOne(alert, card) {
  card.classList.add("loading");
  showLoadingVerdict(`triaging live alert: ${alert.alert_id}…`);
  try {
    const r = await fetch("/api/triage", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ alert }),
    });
    if (!r.ok) throw new Error(`http ${r.status}: ${await r.text()}`);
    const verdict = await r.json();
    renderVerdict(verdict, { source: `live: ${alert.alert_id}` });
  } catch (err) {
    showError(err.message);
  } finally {
    card.classList.remove("loading");
  }
}

async function triageAllLive() {
  showLoadingVerdict("triaging all firing alerts…");
  try {
    const r = await fetch("/api/triage/live", { method: "POST" });
    if (!r.ok) throw new Error(`http ${r.status}: ${await r.text()}`);
    const data = await r.json();
    if (!data.count) {
      showError("no firing alerts to triage.");
      return;
    }
    // Render the first verdict in detail; show others as compact rows.
    renderVerdict(data.verdicts[0], { source: `live (1 of ${data.count})` });
    if (data.count > 1) {
      const extras = data.verdicts.slice(1);
      const block = document.createElement("div");
      block.className = "verdict-block";
      block.innerHTML = `<h3 style="font-size:12px;color:var(--text-dim);margin:12px 0 6px;">also triaged (${extras.length} more)</h3>`;
      extras.forEach((v) => {
        const row = document.createElement("div");
        row.className = "card";
        row.innerHTML = `
          <div class="card-title">${escapeHtml(v.affected_service || "?")}: <span class="sev-badge sev-${v.severity}">${escapeHtml(v.severity || "")}</span></div>
          <div class="card-desc">${escapeHtml(v.alert_summary || "")}</div>
        `;
        block.appendChild(row);
      });
      $("#verdict-pane").appendChild(block);
    }
  } catch (err) {
    showError(err.message);
  }
}

// ──────────────────────────────────────────────────────────────────────────
// Verdict rendering
// ──────────────────────────────────────────────────────────────────────────

function showLoadingVerdict(text) {
  $("#verdict-pane").innerHTML = `<div class="empty">${escapeHtml(text)}</div>`;
}

function showError(msg) {
  $("#verdict-pane").innerHTML = `<div class="empty" style="color:var(--bad)">error: ${escapeHtml(msg)}</div>`;
}

function renderVerdict(v, ctx = {}) {
  const status = v.status || "Active";
  const audit = v.audit_metadata || {};
  const trace = audit.decision_trace || [];

  const rows = [
    ["source",                ctx.source || "—"],
    ["affected_service",      v.affected_service || "—"],
    ["severity",              `<span class="sev-badge sev-${v.severity}">${escapeHtml(v.severity || "?")}</span>`],
    ["confidence_score",      typeof v.confidence_score === "number" ? v.confidence_score.toFixed(2) : "—"],
    ["status",                `<span class="chip ${status === "Suppressed" ? "suppressed" : "active"}">${escapeHtml(status)}</span>`],
    ["duplicate_alert_count", String(v.duplicate_alert_count ?? 1)],
    ["assigned_team",         escapeHtml(v.assigned_team || "—")],
    ["assigned_engineer",     escapeHtml(v.assigned_engineer || "—")],
    ["recommended_runbook",   v.recommended_runbook ? `<a href="${escapeAttr(v.recommended_runbook)}" target="_blank" rel="noreferrer">${escapeHtml(v.recommended_runbook)}</a>` : "—"],
    ["alert_summary",         escapeHtml(v.alert_summary || "—")],
  ];

  let html = `<div class="verdict-block">`;
  for (const [k, val] of rows) {
    html += `<div class="verdict-row"><span class="k">${k}</span><span class="v">${val}</span></div>`;
  }
  html += `</div>`;

  // Decision trace
  html += `<div class="verdict-block"><h3 style="font-size:12px;color:var(--text-dim);margin:0 0 6px;">decision trace (${trace.length} stages)</h3>`;
  html += `<ul class="trace-timeline">`;
  trace.forEach((line) => {
    const dim = /skipped|error|not registered|empty/i.test(line) ? "dim" : "";
    html += `<li class="${dim}">${escapeHtml(line)}</li>`;
  });
  html += `</ul></div>`;

  // Audit + raw JSON
  html += `
    <div class="verdict-block">
      <div class="verdict-row"><span class="k">created_by</span><span class="v">${escapeHtml(audit.created_by || "—")}</span></div>
      <div class="verdict-row"><span class="k">created_at</span><span class="v">${escapeHtml(audit.created_at || "—")}</span></div>
      <div class="verdict-row"><span class="k">source_alerts</span><span class="v">${escapeHtml((audit.source_alerts || []).join(", ") || "—")}</span></div>
    </div>
    <details>
      <summary>raw JSON</summary>
      <pre>${escapeHtml(JSON.stringify(v, null, 2))}</pre>
    </details>
  `;

  $("#verdict-pane").innerHTML = html;
}

// ──────────────────────────────────────────────────────────────────────────
// Misc
// ──────────────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
function escapeAttr(s) { return escapeHtml(s); }

// ──────────────────────────────────────────────────────────────────────────
// Wire-up
// ──────────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  $("#btn-refresh-live").addEventListener("click", refreshLive);
  $("#btn-triage-all-live").addEventListener("click", triageAllLive);
  $("#btn-clear").addEventListener("click", () =>
    showLoadingVerdict("No verdict yet. Click a fixture on the left, or refresh live alerts.")
  );
  loadFixtures();
  loadScenarios();
  refreshHealth();
  setInterval(refreshHealth, 30_000);
});
