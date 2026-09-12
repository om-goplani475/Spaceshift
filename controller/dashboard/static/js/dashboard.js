"use strict";

// ---- Timestamps -----------------------------------------------------------

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
      fetch("/api/" + kind + "/" + encodeURIComponent(id) + "/autoresize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ autoresize: desired }),
      })
        .then(function (resp) {
          if (!resp.ok) throw new Error("request failed");
          return resp.json();
        })
        .then(function () {
          toggle.closest("tr").dataset.autoresize = String(desired);
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
  root.querySelectorAll(".config-value-input, .config-bool-input").forEach(function (input) {
    const row = input.closest(".setting");
    const statusEl = row.querySelector(".config-status");
    const eventName = input.type === "checkbox" ? "change" : "input";
    input.addEventListener(eventName, function () {
      row.dataset.dirty = "true";
      statusEl.textContent = "Unsaved";
      statusEl.className = "config-status dirty";
    });
  });
}

function wireSettingsUpdateButton() {
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

      return fetch("/api/config/" + encodeURIComponent(key), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: value }),
      })
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

formatTimestamps(document);
wireAutoresizeToggles(document);
wireConfigDirtyTracking(document);
wireSettingsUpdateButton();

// ---- Live refresh ------------------------------------------------------------

// Re-fetches the current page and swaps in just the stats cards and each
// table's rows, rather than a full reload or a second, separately-maintained
// JS rendering path for the same data - the Jinja template stays the single
// source of truth for what a row looks like. Filter/search inputs live
// outside the swapped elements, so they're untouched.
const POLL_INTERVAL_MS = 10000;
const SWAP_IDS = ["dashboard-stats", "instances-tbody", "volumes-tbody", "resize-tbody", "scan-tbody"];
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
    const resp = await fetch(location.pathname);
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
    wireAutoresizeToggles(document);
    wireConfigDirtyTracking(document);
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

setInterval(refreshLiveData, POLL_INTERVAL_MS);
setInterval(renderLiveAge, 1000);
renderLiveAge();
document.addEventListener("visibilitychange", function () {
  if (!document.hidden) refreshLiveData();
});
