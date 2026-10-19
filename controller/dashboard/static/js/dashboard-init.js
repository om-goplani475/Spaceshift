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
wireRowKeyboard();
markScrollRegions(document);
window.addEventListener("resize", function () { markScrollRegions(document); });
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
const REFRESH_TIMEOUT_MS = 8000;
let refreshInFlight = false;
// ETag of the last full response. The server answers 304 when nothing
// changed, which skips the download, the parse and the table re-render.
let lastEtag = null;

// The swap below replaces whole table bodies, which would drop keyboard
// focus (and a screen reader's place) if it was on a row, toggle or copy
// button inside one. Remember what had focus as a selector and put it back.
function captureFocus() {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const inside = SWAP_IDS.concat(["settings-list"]).some(function (id) {
    const root = document.getElementById(id);
    return root && root.contains(el);
  });
  if (!inside) return null;
  if (el.id) return "#" + CSS.escape(el.id);
  let sel = el.tagName.toLowerCase();
  el.classList.forEach(function (c) { sel += "." + CSS.escape(c); });
  ["data-id", "data-kind", "data-copy", "data-field", "data-volume", "data-instance", "name"].forEach(function (a) {
    if (el.hasAttribute(a)) sel += "[" + a + '="' + CSS.escape(el.getAttribute(a)) + '"]';
  });
  return sel;
}

function restoreFocus(sel) {
  if (!sel || (document.activeElement && document.activeElement !== document.body)) return;
  const target = document.querySelector(sel);
  if (target) target.focus({ preventScroll: true });
}

const liveIndicator = document.getElementById("live-indicator");
const liveLabel = document.getElementById("live-label");

function markLive() {
  if (liveIndicator) {
    liveIndicator.dataset.state = "idle";
    liveIndicator.title = "This page refreshes itself every 10 seconds while visible";
  }
  if (liveLabel) liveLabel.textContent = "Live";
}

async function refreshLiveData() {
  if (document.hidden || refreshInFlight || batchUpdateInFlight) return;
  // A swap would wipe an edit that hasn't been saved yet - skip this cycle.
  if (document.querySelector('.limit-input[data-dirty="true"]')) return;
  refreshInFlight = true;
  if (liveIndicator) liveIndicator.dataset.state = "refreshing";
  const abort = new AbortController();
  const timeout = setTimeout(function () { abort.abort(); }, REFRESH_TIMEOUT_MS);
  try {
    // Query string included: the audit page's filters live there.
    const headers = { "X-Spaceshift-Poll": "1" };
    if (lastEtag) headers["If-None-Match"] = lastEtag;
    const resp = await fetch(location.pathname + location.search, { headers: headers, signal: abort.signal });
    // The session expired (or was revoked) and the server sent us to the
    // sign-in page - go there properly instead of silently showing stale
    // data with a green "Live" dot.
    if (resp.redirected && new URL(resp.url).pathname === "/login") {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
      return;
    }
    if (resp.status === 304) {
      markLive();
      return;
    }
    if (!resp.ok) throw new Error("refresh failed: " + resp.status);
    lastEtag = resp.headers.get("ETag");
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

    const focusSel = captureFocus();
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
    } else if (settingsList && updatedSettings) {
      // Skipped this cycle: forget the ETag so the next poll is a full
      // response instead of a 304 that would leave settings stale.
      lastEtag = null;
    }

    // The polled HTML has its CSRF token blanked (see _conditional_page);
    // the forms just swapped in get this page's own, which stays valid.
    document.querySelectorAll('input[name="csrf_token"]').forEach(function (input) {
      if (!input.value) input.value = CSRF_TOKEN;
    });

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
    markScrollRegions(document);
    updateVolumesKPI();
    updateInstancesKPI();
    Object.values(tableControllers).forEach(function (c) { c.apply(); });
    restoreFocus(focusSel);
    markLive();
  } catch (e) {
    // Network hiccup or similar - show it, and try again next tick. No
    // countdown/age text on this indicator (just "is it live right now",
    // not "how many seconds ago") - only the error state needs to say
    // anything at all.
    if (liveIndicator) {
      liveIndicator.dataset.state = "error";
      liveIndicator.title = "The last refresh failed, so the numbers may be out of date. Trying again in 10 seconds.";
    }
    if (liveLabel) liveLabel.textContent = "Not updating";
  } finally {
    clearTimeout(timeout);
    refreshInFlight = false;
  }
}

if (document.body.dataset.live !== "off") {
  setInterval(refreshLiveData, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshLiveData();
  });
}
