"use strict";

// Spaceshift dashboard - page/feature-specific wiring: batch actions, audit
// presets and detail modal, settings tabs, KPI updates, autoresize toggles,
// and the settings config form. Depends on dashboard-utils.js (postJSON,
// showToast, CSRF_TOKEN, ...), which must load first. Split from dashboard.js.

let batchUpdateInFlight = false;

function getSelectedCheckboxes() {
  return Array.from(document.querySelectorAll(".row-checkbox:checked"));
}

function updateBatchBar() {
  const batchBar = document.getElementById("batch-action-bar");
  if (!batchBar) return;
  const countEl = document.getElementById("batch-bar-count");
  const selected = getSelectedCheckboxes();
  if (selected.length > 0) {
    const kind = selected[0].dataset.kind || "items";
    if (countEl) countEl.textContent = selected.length + " " + (selected.length === 1 ? (kind === "volumes" ? "volume" : "instance") : (kind === "volumes" ? "volumes" : "instances")) + " selected";
    batchBar.classList.add("is-visible");
  } else {
    batchBar.classList.remove("is-visible");
  }
}

function wireBatchActions() {
  const batchBar = document.getElementById("batch-action-bar");
  const countEl = document.getElementById("batch-bar-count");
  const enableBtn = document.getElementById("batch-enable-btn");
  const disableBtn = document.getElementById("batch-disable-btn");
  const exportBtn = document.getElementById("batch-export-btn");
  const cancelBtn = document.getElementById("batch-cancel-btn");

  document.addEventListener("change", function (e) {
    if (e.target.classList.contains("select-all-checkbox")) {
      const table = document.getElementById(e.target.dataset.table);
      if (!table) return;
      const visibleRows = Array.from(table.tBodies[0].rows).filter(r => !r.classList.contains("is-hidden") && !r.classList.contains("empty-row"));
      visibleRows.forEach(function (r) {
        const cb = r.querySelector(".row-checkbox");
        if (cb) cb.checked = e.target.checked;
      });
      updateBatchBar();
    } else if (e.target.classList.contains("row-checkbox")) {
      updateBatchBar();
    }
  });

  if (cancelBtn) {
    cancelBtn.addEventListener("click", function () {
      document.querySelectorAll(".row-checkbox, .select-all-checkbox").forEach(cb => cb.checked = false);
      updateBatchBar();
    });
  }

  async function setBatchAutoresize(desired) {
    const selected = getSelectedCheckboxes();
    if (!selected.length || batchUpdateInFlight) return;
    batchUpdateInFlight = true;
    if (enableBtn) enableBtn.disabled = true;
    if (disableBtn) disableBtn.disabled = true;
    if (cancelBtn) cancelBtn.disabled = true;
    if (exportBtn) exportBtn.disabled = true;

    let succeeded = 0;
    let failed = 0;

    const promises = selected.map(async function (cb) {
      const kind = cb.dataset.kind;
      const id = cb.dataset.id;
      const row = cb.closest("tr");
      try {
        const resp = await postJSON("/api/" + kind + "/" + encodeURIComponent(id) + "/autoresize", { autoresize: desired });
        if (!resp.ok) throw new Error();
        succeeded++;
        if (row) {
          row.dataset.autoresize = String(desired);
          const toggle = row.querySelector(".autoresize-toggle");
          if (toggle) toggle.checked = desired;
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
        }
      } catch (err) {
        failed++;
      }
    });

    await Promise.all(promises);
    batchUpdateInFlight = false;
    if (enableBtn) enableBtn.disabled = false;
    if (disableBtn) disableBtn.disabled = false;
    if (cancelBtn) cancelBtn.disabled = false;
    if (exportBtn) exportBtn.disabled = false;

    updateVolumesKPI();
    updateInstancesKPI();
    showToast((desired ? "Enabled" : "Disabled") + " autoresize on " + succeeded + " items" + (failed ? " (" + failed + " failed)" : ""), failed ? "error" : "success");
    document.querySelectorAll(".row-checkbox, .select-all-checkbox").forEach(cb => cb.checked = false);
    updateBatchBar();
  }

  if (enableBtn) enableBtn.addEventListener("click", () => setBatchAutoresize(true));
  if (disableBtn) disableBtn.addEventListener("click", () => setBatchAutoresize(false));
  if (exportBtn) {
    exportBtn.addEventListener("click", function () {
      const selected = getSelectedCheckboxes();
      if (!selected.length) return;
      const table = selected[0].closest("table");
      if (!table) return;
      const headerCells = Array.from(table.tHead.rows[0].cells).filter(c => !c.classList.contains("select-col"));
      const headers = headerCells.map(th => th.textContent.trim()).filter(h => h.toLowerCase() !== "autoresize");

      const rowsData = selected.map(function (cb) {
        const row = cb.closest("tr");
        const dataCells = Array.from(row.cells).filter(c => !c.classList.contains("select-col"));
        return headers.map((h, i) => '"' + (dataCells[i] ? dataCells[i].textContent.trim().replace(/"/g, '""') : "") + '"');
      });

      const csvContent = [headers.map(h => '"' + h + '"').join(","), ...rowsData.map(r => r.join(","))].join("\n");
      const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "spaceshift-selected.csv";
      a.click();
      URL.revokeObjectURL(url);
      showToast("Exported " + selected.length + " rows to CSV", "success");
    });
  }
}

function wireAuditPresets() {
  const form = document.getElementById("audit-filters-form");
  const sinceInput = document.getElementById("audit-since");
  const untilInput = document.getElementById("audit-until");
  if (!form || !sinceInput || !untilInput) return;

  function setDateRange(daysAgo) {
    const now = new Date();
    const untilStr = now.toISOString().slice(0, 10);
    let sinceStr = "";
    if (daysAgo !== null) {
      const past = new Date(now.getTime() - daysAgo * 24 * 60 * 60 * 1000);
      sinceStr = past.toISOString().slice(0, 10);
    }
    sinceInput.value = sinceStr;
    untilInput.value = daysAgo !== null ? untilStr : "";
    form.submit();
  }

  document.querySelectorAll(".btn-preset").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const range = btn.dataset.range;
      if (range === "today") setDateRange(0);
      else if (range === "24h") setDateRange(1);
      else if (range === "7d") setDateRange(7);
      else if (range === "30d") setDateRange(30);
      else if (range === "all") setDateRange(null);
    });
  });
}

function wireAuditDetailModal() {
  const modal = document.getElementById("detail-modal");
  const overlay = document.getElementById("detail-modal-overlay");
  const closeBtn = document.getElementById("detail-modal-close");
  const metaEl = document.getElementById("detail-modal-meta");
  const contentEl = document.getElementById("detail-modal-content");
  if (!modal || !contentEl) return;

  function closeModal() {
    modal.classList.add("is-hidden");
  }

  if (overlay) overlay.addEventListener("click", closeModal);
  if (closeBtn) closeBtn.addEventListener("click", closeModal);

  document.addEventListener("click", function (e) {
    if (e.target.closest("a, button, input, select, label")) return;
    const row = e.target.closest(".audit-row");
    if (!row) return;
    const lines = [];
    if (row.dataset.target) lines.push("Target: " + row.dataset.target);
    if (row.dataset.account) lines.push("Account: " + row.dataset.account);
    if (row.dataset.from) lines.push("From: " + row.dataset.from);
    if (row.dataset.change) lines.push("Change: " + row.dataset.change);
    const detailRaw = row.dataset.detail;
    if (detailRaw) {
      try {
        lines.push("Detail: " + JSON.stringify(JSON.parse(detailRaw), null, 2));
      } catch (_) {
        // malformed JSON in data-detail - skip it, the rest of the event still shows.
      }
    }
    let formattedTs = row.dataset.ts || "";
    if (formattedTs) {
      try {
        const d = new Date(formattedTs);
        if (!isNaN(d.getTime())) {
          formattedTs = d.toLocaleString(undefined, {
            year: "numeric", month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit", second: "2-digit"
          });
        }
      } catch (_) {}
    }
    metaEl.textContent = "Actor: " + (row.dataset.who || "") + " | Action: " + (row.dataset.action || "") + " | " + formattedTs;
    contentEl.textContent = lines.length ? lines.join("\n") : "No further detail recorded for this event.";
    modal.classList.remove("is-hidden");
  });
}

function wireSettingsTabs() {
  const tabs = document.querySelectorAll(".settings-tab-btn");
  if (!tabs.length) return;

  // The click handler below sets the active tab directly, but the
  // IntersectionObserver scroll-spy further down also reacts to the
  // smooth-scroll that click triggers - on a page this short, several
  // sections pass through its detection band during that single
  // animation, so it can overwrite the just-clicked tab with whichever
  // one it last saw mid-scroll. Suppressing it for the scroll's duration
  // stops it fighting the click; it still drives the highlight correctly
  // once the user scrolls manually afterward.
  let suppressSpy = false;
  let suppressTimer = null;

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(t => t.classList.remove("is-active"));
      tab.classList.add("is-active");
      suppressSpy = true;
      clearTimeout(suppressTimer);
      suppressTimer = setTimeout(function () { suppressSpy = false; }, 700);
      const target = document.getElementById(tab.dataset.target);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(function (entries) {
      if (suppressSpy) return;
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          tabs.forEach(function (t) {
            t.classList.toggle("is-active", t.dataset.target === entry.target.id);
          });
        }
      });
    }, { rootMargin: "-120px 0px -60% 0px" });

    document.querySelectorAll(".settings-group").forEach(function (g) {
      observer.observe(g);
    });
  }
}

function updateVolumesKPI() {
  const summary = document.getElementById("volumes-summary");
  if (!summary) return;
  const rows = Array.from(document.querySelectorAll("#volumes-tbody tr[data-volume]"));
  if (!rows.length) return;
  const watchedRows = rows.filter(function (r) {
    return r.dataset.watchedReason === "own" || r.dataset.watchedReason === "instance";
  });
  const watchedCount = watchedRows.length;
  const unwatchedCount = rows.length - watchedCount;

  const watchedEl = document.getElementById("kpi-vol-watched");
  if (watchedEl) watchedEl.textContent = String(watchedCount);
  const unwatchedEl = document.getElementById("kpi-vol-unwatched");
  if (unwatchedEl) unwatchedEl.textContent = String(unwatchedCount);
  const totalEl = document.getElementById("kpi-vol-total");
  if (totalEl) totalEl.textContent = String(rows.length);

  const distCard = document.getElementById("volumes-dist-card");
  if (distCard) {
    let totalGb = 0;
    let watchedGb = 0;
    rows.forEach(function (r) {
      const sizeCell = r.querySelector(".num");
      const sizeGb = sizeCell ? (parseInt(sizeCell.textContent.replace(/[^0-9]/g, ""), 10) || 0) : 0;
      totalGb += sizeGb;
      if (r.dataset.watchedReason === "own" || r.dataset.watchedReason === "instance") {
        watchedGb += sizeGb;
      }
    });
    const unwatchedGb = totalGb - watchedGb;
    const watchedPct = totalGb > 0 ? Math.round((watchedGb / totalGb) * 100) : 0;
    const unwatchedPct = 100 - watchedPct;

    const barWatched = distCard.querySelector(".dist-segment.watched");
    const barUnwatched = distCard.querySelector(".dist-segment.unwatched");
    const headerPct = distCard.querySelector(".storage-dist-header span");
    const legend = distCard.querySelector(".storage-dist-legend");

    if (barWatched) barWatched.style.width = watchedPct + "%";
    if (barUnwatched) barUnwatched.style.width = unwatchedPct + "%";
    if (headerPct) headerPct.textContent = watchedPct + "% Watched";
    if (legend) {
      legend.innerHTML = '<span><span class="dot-green"></span> Watched: <strong>' + watchedGb + ' GB</strong> (' + watchedPct + '%)</span>' +
        '<span><span class="dot-gray"></span> Excluded / Unwatched: <strong>' + unwatchedGb + ' GB</strong> (' + unwatchedPct + '%)</span>';
    }
  }
}

function updateInstancesKPI() {
  const summary = document.getElementById("instances-summary");
  if (!summary) return;
  const rows = Array.from(document.querySelectorAll("#instances-tbody tr[data-instance]"));
  if (!rows.length) return;
  const watchedCount = rows.filter(function (r) {
    return r.dataset.autoresize === "true";
  }).length;

  const watchedEl = document.getElementById("kpi-inst-watched");
  if (watchedEl) watchedEl.textContent = String(watchedCount);
  const totalEl = document.getElementById("kpi-inst-total");
  if (totalEl) totalEl.textContent = String(rows.length);
}

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
            updateVolumesKPI();
          } else if (kind === "instances") {
            updateInstancesKPI();
          }
          if (typeof showToast === "function") {
            showToast((kind === "volumes" ? "Volume " : "Instance ") + id + " autoresize " + (desired ? "enabled" : "disabled"), "success");
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
        // One line per enabled channel, so a failing one is named next to the working ones.
        const channels = result.data.channels || {};
        statusEl.textContent = Object.keys(channels).length
          ? Object.keys(channels).map(function (name) {
              return name + ": " + (channels[name] === null ? "sent" : channels[name]);
            }).join(" · ")
          : (result.data.error || "Failed");
        statusEl.className = "config-status " + (result.ok ? "ok" : "error");
        btn.disabled = false;
      });
  });
}

