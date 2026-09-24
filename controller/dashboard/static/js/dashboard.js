"use strict";

// ---- CSRF ---------------------------------------------------------------------

// Every state-changing fetch() sends the token from base.html's meta tag;
// Flask-WTF rejects a POST without it. The token is tied to the session,
// not the page, so it stays valid across live refreshes.
const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

function postJSON(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF_TOKEN },
    body: JSON.stringify(body),
  });
}

// ---- Timestamps -----------------------------------------------------------

// ---- Usage meters -----------------------------------------------------------

// Width comes from one of 21 fixed "pN" classes (dashboard.css), not a
// style="width:" attribute or an el.style.width assignment - both count as
// inline styles under the dashboard's CSP (no style-src 'unsafe-inline'),
// and Chrome blocks script-set el.style mutations under it too.
function renderMeters(root) {
  root.querySelectorAll(".meter[data-pct]").forEach(function (el) {
    const fill = el.querySelector(".meter-fill");
    if (!fill) return;
    const pct = Math.max(0, Math.min(100, Math.round(Number(el.dataset.pct) / 5) * 5));
    fill.className = "meter-fill p" + pct;
  });
}

function formatTimestamps(root) {
  root.querySelectorAll(".ts").forEach(function (el) {
    var d = new Date(el.dataset.ts);
    el.textContent = d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    });
    el.title = d.toISOString();
  });
}

// ---- Table filtering and paging --------------------------------------------

const PAGE_SIZE = 25;
const tableControllers = {};

// getRows() re-reads table.tBodies[0].rows fresh on every call, rather than
// a one-time snapshot - this table's tbody content gets replaced wholesale
// by refreshLiveData() every poll (see below), so a stale array captured
// once at page load would silently stop matching what's on screen.
function wireFilters(tableId, searchId, selectIds, countId, pagerId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  function getRows() {
    return Array.from(table.tBodies[0].rows).filter(r => r.dataset.volume !== undefined);
  }
  const search = document.getElementById(searchId);
  const selects = selectIds.map(id => document.getElementById(id)).filter(Boolean);
  const countEl = document.getElementById(countId);
  const pagerEl = document.getElementById(pagerId);
  let page = 1;

  function matches(row) {
    const q = (search && search.value || "").trim().toLowerCase();
    if (q && !(row.dataset.volume.includes(q) || row.dataset.instance.includes(q))) return false;
    for (const sel of selects) {
      if (sel.value && row.dataset[sel.dataset.field] !== sel.value) return false;
    }
    return true;
  }

  function apply() {
    const rows = getRows();
    const matched = rows.filter(matches);
    const totalPages = Math.max(1, Math.ceil(matched.length / PAGE_SIZE));
    if (page > totalPages) page = totalPages;
    const start = (page - 1) * PAGE_SIZE;
    const pageRows = new Set(matched.slice(start, start + PAGE_SIZE));

    for (const row of rows) {
      row.classList.toggle("is-hidden", !pageRows.has(row));
    }
    // The server-rendered empty row only exists when there's no data at
    // all; a filter that matches nothing needs its own message.
    let noMatch = table.querySelector(".no-match-row");
    if (rows.length > 0 && matched.length === 0) {
      if (!noMatch) {
        noMatch = document.createElement("tr");
        noMatch.className = "empty-row no-match-row";
        const cell = document.createElement("td");
        cell.className = "empty";
        cell.colSpan = table.tHead.rows[0].cells.length;
        cell.innerHTML = '<span class="empty-title">Nothing matches these filters.</span>';
        noMatch.appendChild(cell);
        table.tBodies[0].appendChild(noMatch);
      }
    } else if (noMatch) {
      noMatch.remove();
    }
    if (countEl) {
      const shown = matched.length === 0 ? 0 : start + 1;
      const shownEnd = Math.min(start + PAGE_SIZE, matched.length);
      countEl.textContent = matched.length
        ? shown + "–" + shownEnd + " of " + matched.length + (matched.length !== rows.length ? " (" + rows.length + " total)" : "")
        : "0 of " + rows.length;
    }
    if (pagerEl) {
      pagerEl.innerHTML = "";
      if (totalPages > 1) {
        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "btn";
        prev.textContent = "Previous";
        prev.disabled = page <= 1;
        prev.onclick = function () { page--; apply(); };
        const label = document.createElement("span");
        label.textContent = "Page " + page + " of " + totalPages;
        const next = document.createElement("button");
        next.type = "button";
        next.className = "btn";
        next.textContent = "Next";
        next.disabled = page >= totalPages;
        next.onclick = function () { page++; apply(); };
        pagerEl.append(prev, label, next);
      }
    }
  }

  function applyFromStart() { page = 1; apply(); }

  if (search) search.addEventListener("input", applyFromStart);
  for (const sel of selects) sel.addEventListener("change", applyFromStart);
  apply();
  tableControllers[tableId] = { apply };
}

// Each page has a subset of these tables; wireFilters() is a no-op for the
// ones that aren't present. Each <select> carries its own data-field.
wireFilters("resize-table", "resize-search", ["resize-status-filter"], "resize-count", "resize-pager");
wireFilters(
  "scan-table", "scan-search",
  ["scan-account-filter", "scan-region-filter", "scan-size-filter", "scan-decision-filter"],
  "scan-count", "scan-pager"
);
wireFilters(
  "instances-table", "instances-search",
  ["instances-account-filter", "instances-region-filter", "instances-autoresize-filter"],
  "instances-count", "instances-pager"
);
wireFilters(
  "volumes-table", "volumes-search",
  ["volumes-account-filter", "volumes-region-filter", "volumes-autoresize-filter"],
  "volumes-count", "volumes-pager"
);

// ---- Slide-over detail drawer -------------------------------------------------

// Generic: any table listed here shows a right-hand drawer with every
// column of the clicked row, keyed off the table's own <thead> - no
// per-table drawer markup or extra backend fields needed. The first
// column is always the row's real identifier (volume/instance id) and
// drives the Identifier section and the generated AWS CLI command; a
// "Name" column, when the table has one, becomes the drawer's big title
// instead (falling back to the id for tables with no Name column) so the
// drawer reads the way the AWS console does - human name up top, id
// underneath. Clicks on an interactive cell (the autoresize switch, a
// link) pass through instead of opening the drawer. Plain DOM, not a
// framework component - the dashboard's CSP has no script-src
// 'unsafe-eval', which rules out Alpine.js (and anything else that
// compiles expressions via `new Function` at runtime).
const DRAWER_TABLES = ["resize-table", "scan-table", "instances-table", "volumes-table"];

function openDrawer(id, title, fields) {
  const root = document.getElementById("drawer-root");
  if (!root) return;
  document.getElementById("drawer-title").textContent = title;
  const isNamed = title !== id;
  const subtitle = document.getElementById("drawer-subtitle");
  subtitle.textContent = isNamed ? id : "Details from this row";
  subtitle.classList.toggle("mono", isNamed);
  document.getElementById("drawer-id").textContent = id;
  const fieldsEl = document.getElementById("drawer-fields");
  fieldsEl.innerHTML = "";
  fields.forEach(function (f) {
    const row = document.createElement("div");
    row.className = "drawer-field";
    const k = document.createElement("span");
    k.className = "k";
    k.textContent = f.k;
    const v = document.createElement("span");
    v.className = "v";
    v.textContent = f.v;
    row.append(k, v);
    fieldsEl.appendChild(row);
  });

  const cliMatch = /^(vol|i)-[0-9a-f]+$/.exec(id);
  const cliSection = document.getElementById("drawer-cli-section");
  if (cliMatch) {
    const cmd = (id.startsWith("vol-") ? "aws ec2 describe-volumes --volume-ids " : "aws ec2 describe-instances --instance-ids ") + id;
    document.getElementById("drawer-cli-cmd").textContent = cmd;
    cliSection.classList.remove("is-hidden");
  } else {
    cliSection.classList.add("is-hidden");
  }

  document.getElementById("drawer-overlay").classList.add("is-open");
  document.getElementById("drawer").classList.add("is-open");
}

function closeDrawer() {
  const overlay = document.getElementById("drawer-overlay");
  const drawer = document.getElementById("drawer");
  if (overlay) overlay.classList.remove("is-open");
  if (drawer) drawer.classList.remove("is-open");
}

function copyToClipboard(text, btn) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(function () {
    btn.classList.add("is-copied");
    setTimeout(function () { btn.classList.remove("is-copied"); }, 1400);
  });
}

function wireDrawerChrome() {
  const root = document.getElementById("drawer-root");
  if (!root || root.dataset.wired) return;
  root.dataset.wired = "1";
  const overlay = document.getElementById("drawer-overlay");
  const closeBtn = document.getElementById("drawer-close");
  if (overlay) overlay.addEventListener("click", closeDrawer);
  if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDrawer();
  });
  const copyId = document.getElementById("drawer-copy-id");
  if (copyId) copyId.addEventListener("click", function () { copyToClipboard(document.getElementById("drawer-id").textContent, copyId); });
  const copyCli = document.getElementById("drawer-copy-cli");
  if (copyCli) copyCli.addEventListener("click", function () { copyToClipboard(document.getElementById("drawer-cli-cmd").textContent, copyCli); });
}

// A handful of cells aren't plain text (the autoresize switch has none at
// all) - read those from the row's own data-* attributes instead of the
// cell's rendered text, which is what a generic "read every <td>" pass
// would otherwise show as a blank "–".
function _drawerCellValue(header, cell, row) {
  if (header === "Autoresize") return row.dataset.autoresize === "true" ? "On" : "Off";
  const text = cell && cell.textContent.trim();
  return text || "–";
}

function wireRowDrawer(tableId) {
  const table = document.getElementById(tableId);
  if (!table || table.dataset.drawerWired) return;
  table.dataset.drawerWired = "1";
  table.addEventListener("click", function (e) {
    if (e.target.closest("input, select, button, a, label")) return;
    const row = e.target.closest("tbody tr");
    if (!row || !table.contains(row) || row.classList.contains("empty-row")) return;
    const headers = Array.from(table.tHead.rows[0].cells).map(th => th.textContent.trim());
    const cells = Array.from(row.cells);
    const id = cells[0] && cells[0].textContent.trim();
    if (!id) return;
    const fields = headers
      .slice(1)
      .map((k, i) => ({ k: k, v: _drawerCellValue(k, cells[i + 1], row) }))
      .filter((f) => f.k !== "Name");
    const nameIndex = headers.indexOf("Name");
    const name = nameIndex > 0 && cells[nameIndex] && cells[nameIndex].textContent.trim();
    const title = name && name !== "–" ? name : id;
    openDrawer(id, title, fields);
  });
}

function wireAllDrawers() {
  wireDrawerChrome();
  DRAWER_TABLES.forEach(wireRowDrawer);
}

// ---- Autoresize toggles ----------------------------------------------------

// Each of these wiring functions re-queries root (default: the whole
// document) every time it's called, so calling it again after
// refreshLiveData() swaps in fresh table rows picks up the new elements -
// old elements are gone from the DOM by then, so there's no risk of
// double-binding the same listener twice.
function wireAutoresizeToggles(root) {
  root.querySelectorAll(".autoresize-toggle").forEach(function (toggle) {
    toggle.addEventListener("change", function () {
      const kind = toggle.dataset.kind; // "instances" or "volumes"
      const id = toggle.dataset.id;
      const desired = toggle.checked;
      toggle.disabled = true;
      postJSON("/api/" + kind + "/" + encodeURIComponent(id) + "/autoresize", { autoresize: desired })
        .then(function (resp) {
          if (!resp.ok) throw new Error("request failed");
          return resp.json();
        })
        .then(function () {
          const row = toggle.closest("tr");
          row.dataset.autoresize = String(desired);
          // The real "is it watched" resolution (including threshold
          // fallback) lives server-side and settles within one live-poll
          // cycle (10s) - but the Watched badge only needs the same two
          // facts the toggle itself just changed, so update it now rather
          // than leaving it stale until the next poll.
          if (kind === "volumes" && row.dataset.watchedReason !== "unattached") {
            const viaInstance = !desired && row.dataset.instanceAutoresize === "true";
            const watched = desired || viaInstance;
            const cell = row.querySelector(".watched-cell");
            if (cell) {
              cell.innerHTML = watched
                ? '<span class="badge badge-green">Yes' + (viaInstance ? " · via instance" : "") + "</span>"
                : '<span class="badge badge-muted">No</span>';
            }
            row.dataset.watchedReason = watched ? (viaInstance ? "instance" : "own") : "off";
          }
        })
        .catch(function () {
          toggle.checked = !desired;
          alert("Couldn't update autoresize for " + id + ". Please try again.");
        })
        .finally(function () {
          toggle.disabled = false;
        });
    });
  });
}

// ---- Settings ----------------------------------------------------------------

// Marks a setting "dirty" (edited, not yet submitted) rather than saving on
// every keystroke/toggle - one "Update settings" click at the bottom
// submits everything dirty at once, so changing several values doesn't
// mean several round-trips (or several chances to forget one). A dirty
// setting also blocks the live-refresh poll from overwriting it - see
// refreshLiveData()'s settings guard below.
function wireConfigDirtyTracking(root) {
  // Only the Settings page's rows have the dirty/status machinery; the
  // account forms reuse the input styling and submit normally.
  root.querySelectorAll("#settings-list .config-value-input, #settings-list .config-bool-input").forEach(function (input) {
    const row = input.closest(".setting");
    const statusEl = row.querySelector(".config-status");
    const eventName = input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input";
    input.addEventListener(eventName, function () {
      row.dataset.dirty = "true";
      statusEl.textContent = "Unsaved";
      statusEl.className = "config-status dirty";
    });
  });
}

function wireSettingsUpdateButton() {
  // Enter in a text field would otherwise submit the form as a plain
  // page POST; the Update button is the only way settings are saved.
  const form = document.getElementById("settings-form");
  if (form) form.addEventListener("submit", function (e) { e.preventDefault(); });
  const btn = document.getElementById("settings-update-btn");
  if (!btn) return;
  btn.addEventListener("click", function () {
    const dirtyRows = Array.from(document.querySelectorAll('#settings-list .setting[data-dirty="true"]'));
    const summaryEl = document.getElementById("settings-update-summary");
    if (dirtyRows.length === 0) {
      summaryEl.textContent = "Nothing to save";
      summaryEl.className = "config-status";
      return;
    }
    btn.disabled = true;
    summaryEl.textContent = "Saving…";
    summaryEl.className = "config-status";

    const requests = dirtyRows.map(function (row) {
      const key = row.dataset.key;
      const statusEl = row.querySelector(".config-status");
      const boolInput = row.querySelector(".config-bool-input");
      const textInput = row.querySelector(".config-value-input");
      const value = boolInput ? (boolInput.checked ? "true" : "false") : textInput.value.trim();

      return postJSON("/api/config/" + encodeURIComponent(key), { value: value })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data, row: row, statusEl: statusEl };
          });
        })
        .catch(function () {
          return { ok: false, data: { error: "Request failed" }, row: row, statusEl: statusEl };
        });
    });

    Promise.all(requests).then(function (results) {
      let succeeded = 0;
      let failed = 0;
      results.forEach(function (result) {
        if (result.ok) {
          succeeded++;
          delete result.row.dataset.dirty;
          result.statusEl.textContent = "Saved";
          result.statusEl.className = "config-status ok";
        } else {
          failed++;
          result.statusEl.textContent = result.data.error || "Invalid value";
          result.statusEl.className = "config-status error";
        }
      });
      summaryEl.textContent = failed === 0
        ? "Saved " + succeeded + " setting" + (succeeded === 1 ? "" : "s") + " - applies on the next cycle"
        : "Saved " + succeeded + ", " + failed + " failed - see the messages above";
      summaryEl.className = "config-status " + (failed === 0 ? "ok" : "error");
      btn.disabled = false;
    });
  });
}

function wireNotificationTestButton() {
  const btn = document.getElementById("notification-test-btn");
  if (!btn) return;
  const statusEl = document.getElementById("notification-test-status");
  btn.addEventListener("click", function () {
    btn.disabled = true;
    statusEl.textContent = "Sending…";
    statusEl.className = "config-status";
    postJSON("/api/notifications/test", {})
      .then(function (resp) { return resp.json().then(function (data) { return { ok: resp.ok, data: data }; }); })
      .catch(function () { return { ok: false, data: { error: "Request failed" } }; })
      .then(function (result) {
        statusEl.textContent = result.ok
          ? "Sent via " + result.data.method + " - check the channel"
          : (result.data.error || "Failed");
        statusEl.className = "config-status " + (result.ok ? "ok" : "error");
        btn.disabled = false;
      });
  });
}

function markClickableRows() {
  DRAWER_TABLES.forEach(function (id) {
    const table = document.getElementById(id);
    if (!table) return;
    Array.from(table.tBodies[0].rows).forEach(function (r) {
      if (!r.classList.contains("empty-row")) r.classList.add("is-clickable");
    });
  });
}

formatTimestamps(document);
renderMeters(document);
wireAutoresizeToggles(document);
wireConfigDirtyTracking(document);
wireSettingsUpdateButton();
wireNotificationTestButton();
wireAllDrawers();
markClickableRows();

// ---- Live refresh ------------------------------------------------------------

// Re-fetches the current page and swaps in just the stats cards and each
// table's rows, rather than a full reload or a second, separately-maintained
// JS rendering path for the same data - the Jinja template stays the single
// source of truth for what a row looks like. Filter/search inputs live
// outside the swapped elements, so they're untouched.
const POLL_INTERVAL_MS = 10000;
const SWAP_IDS = [
  "dashboard-stats", "instances-tbody", "volumes-tbody", "resize-tbody", "scan-tbody", "audit-tbody",
  "accounts-tbody", "account-status",
];
let refreshInFlight = false;
let lastRefreshedAt = Date.now();

const liveIndicator = document.getElementById("live-indicator");
const liveAge = document.getElementById("live-age");

function renderLiveAge() {
  if (!liveAge) return;
  const seconds = Math.round((Date.now() - lastRefreshedAt) / 1000);
  liveAge.textContent = seconds < 5 ? "just now" : seconds + "s ago";
}

async function refreshLiveData() {
  if (document.hidden || refreshInFlight) return;
  refreshInFlight = true;
  if (liveIndicator) liveIndicator.dataset.state = "refreshing";
  try {
    // Query string included: the audit page's filters live there.
    const resp = await fetch(location.pathname + location.search);
    // The session expired (or was revoked) and the server sent us to the
    // sign-in page - go there properly instead of silently showing stale
    // data with a green "Live" dot.
    if (resp.redirected && new URL(resp.url).pathname === "/login") {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
      return;
    }
    if (!resp.ok) throw new Error("refresh failed: " + resp.status);
    const html = await resp.text();
    const fresh = new DOMParser().parseFromString(html, "text/html");

    SWAP_IDS.forEach(function (id) {
      const current = document.getElementById(id);
      const updated = fresh.getElementById(id);
      if (current && updated) current.innerHTML = updated.innerHTML;
    });

    // Settings have an unsaved-input state (typed/toggled but not yet
    // submitted via the Update button) that a background swap would
    // silently wipe out - skip that section for this cycle if the user is
    // either actively focused inside it, or has any dirty (unsaved) row,
    // since focus can move away from a field without the edit being saved.
    const settingsList = document.getElementById("settings-list");
    const updatedSettings = fresh.getElementById("settings-list");
    const editingSettings = settingsList && settingsList.contains(document.activeElement);
    const hasUnsavedSettings = settingsList && settingsList.querySelector('.setting[data-dirty="true"]');
    if (settingsList && updatedSettings && !editingSettings && !hasUnsavedSettings) {
      settingsList.innerHTML = updatedSettings.innerHTML;
    }

    formatTimestamps(document);
    renderMeters(document);
    wireAutoresizeToggles(document);
    wireConfigDirtyTracking(document);
    markClickableRows();
    Object.values(tableControllers).forEach(function (c) { c.apply(); });
    lastRefreshedAt = Date.now();
    if (liveIndicator) liveIndicator.dataset.state = "idle";
  } catch (e) {
    // Network hiccup or similar - show it, and try again next tick.
    if (liveIndicator) liveIndicator.dataset.state = "error";
  } finally {
    refreshInFlight = false;
    renderLiveAge();
  }
}

if (document.body.dataset.live !== "off") {
  setInterval(refreshLiveData, POLL_INTERVAL_MS);
  setInterval(renderLiveAge, 1000);
  renderLiveAge();
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshLiveData();
  });
}
