"use strict";

// Spaceshift dashboard - page bootstrap: wires up every page's widgets on
// load, then the live-refresh poll that keeps the page's data current
// without a full reload. Loaded last, after dashboard-utils.js and
// dashboard-features.js, since it calls into both. Split from dashboard.js.

formatTimestamps(document);
renderMeters(document);
wireAutoresizeToggles(document);
wireLimitInputs(document);
wireConfigDirtyTracking(document);
wireSettingsUpdateButton();
wireNotificationTestButton();
wireAllDrawers();
wireCodeCopyButtons(document);
wireInlineCopyButtons(document);
wireBatchActions();
wireAuditPresets();
wireAuditDetailModal();
wireSettingsTabs();
markClickableRows();
initThemeToggle();
initKeyboardShortcuts();
wireTableSorting();
wireExportCSVButtons();
wireShortcutsModal();
updateVolumesKPI();
updateInstancesKPI();

// ---- Live refresh ------------------------------------------------------------

// Re-fetches the current page and swaps in just the stats cards and each
// table's rows, rather than a full reload or a second, separately-maintained
// JS rendering path for the same data - the Jinja template stays the single
// source of truth for what a row looks like. Filter/search inputs live
// outside the swapped elements, so they're untouched.
const POLL_INTERVAL_MS = 10000;
const SWAP_IDS = [
  "dashboard-stats", "volumes-summary", "instances-summary", "volumes-dist-card", "instances-tbody", "volumes-tbody", "resize-tbody", "scan-tbody", "audit-tbody",
  "accounts-tbody", "account-status",
];
let refreshInFlight = false;

const liveIndicator = document.getElementById("live-indicator");
const liveLabel = document.getElementById("live-label");

async function refreshLiveData() {
  if (document.hidden || refreshInFlight || batchUpdateInFlight) return;
  // A swap would wipe an edit that hasn't been saved yet - skip this cycle.
  if (document.querySelector('.limit-input[data-dirty="true"]')) return;
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

    // Preserve checked state on checkboxes across live poll swap
    const checkedIds = new Set(
      Array.from(document.querySelectorAll(".row-checkbox:checked"))
        .map(cb => cb.dataset.id)
        .filter(Boolean)
    );
    const selectAllState = {};
    document.querySelectorAll(".select-all-checkbox").forEach(cb => {
      if (cb.checked && cb.dataset.table) selectAllState[cb.dataset.table] = true;
    });

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

    // Restore checkbox selections
    if (checkedIds.size > 0) {
      document.querySelectorAll(".row-checkbox").forEach(cb => {
        if (checkedIds.has(cb.dataset.id)) {
          cb.checked = true;
        }
      });
      document.querySelectorAll(".select-all-checkbox").forEach(cb => {
        if (selectAllState[cb.dataset.table]) {
          cb.checked = true;
        }
      });
    }
    updateBatchBar();

    formatTimestamps(document);
    renderMeters(document);
    wireAutoresizeToggles(document);
    wireLimitInputs(document);
    wireConfigDirtyTracking(document);
    wireInlineCopyButtons(document);
    markClickableRows();
    updateVolumesKPI();
    updateInstancesKPI();
    Object.values(tableControllers).forEach(function (c) { c.apply(); });
    if (liveIndicator) liveIndicator.dataset.state = "idle";
    if (liveLabel) liveLabel.textContent = "Live";
  } catch (e) {
    // Network hiccup or similar - show it, and try again next tick. No
    // countdown/age text on this indicator (just "is it live right now",
    // not "how many seconds ago") - only the error state needs to say
    // anything at all.
    if (liveIndicator) liveIndicator.dataset.state = "error";
    if (liveLabel) liveLabel.textContent = "Error";
  } finally {
    refreshInFlight = false;
  }
}

if (document.body.dataset.live !== "off") {
  setInterval(refreshLiveData, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshLiveData();
  });
}
