"use strict";

// Spaceshift dashboard - generic, page-agnostic utilities: CSRF/postJSON,
// timestamps, usage meters, table filtering, the slide-over drawer, toast,
// clipboard copy, table sorting, CSV export, theme toggle, and the shortcuts
// modal. Loaded before dashboard-features.js and dashboard-init.js, which
// both call into functions defined here. Split from dashboard.js.

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
    return Array.from(table.tBodies[0].rows).filter(r => r.dataset.volume !== undefined || r.dataset.instance !== undefined);
  }
  const search = document.getElementById(searchId);
  const selects = selectIds.map(id => document.getElementById(id)).filter(Boolean);
  const countEl = document.getElementById(countId);
  const pagerEl = document.getElementById(pagerId);
  const prefix = tableId.replace("-table", "");
  const activeFiltersBar = document.getElementById(prefix + "-active-filters");
  const searchWrapper = search ? search.closest(".search-wrapper") : null;
  const clearSearchBtn = searchWrapper ? searchWrapper.querySelector(".search-clear-btn") : null;
  let page = 1;

  if (clearSearchBtn && search) {
    clearSearchBtn.addEventListener("click", function () {
      search.value = "";
      searchWrapper.classList.remove("has-val");
      applyFromStart();
      search.focus();
    });
  }

  function renderActiveChips() {
    if (!activeFiltersBar) return;
    const chips = [];
    const q = (search && search.value || "").trim();
    if (q) {
      chips.push({
        label: 'Search: "' + q + '"',
        clear: function () {
          search.value = "";
          if (searchWrapper) searchWrapper.classList.remove("has-val");
          applyFromStart();
        }
      });
    }

    selects.forEach(function (sel) {
      if (sel.value) {
        const optionText = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : sel.value;
        const fieldName = sel.dataset.field || "Filter";
        chips.push({
          label: fieldName.charAt(0).toUpperCase() + fieldName.slice(1) + ": " + optionText,
          clear: function () {
            sel.value = "";
            applyFromStart();
          }
        });
      }
    });

    if (chips.length > 0) {
      activeFiltersBar.innerHTML = "";
      chips.forEach(function (c) {
        const chip = document.createElement("span");
        chip.className = "filter-chip";
        chip.textContent = c.label + " ";
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "filter-chip-remove";
        rm.textContent = "✕";
        rm.setAttribute("aria-label", "Remove filter");
        rm.onclick = c.clear;
        chip.appendChild(rm);
        activeFiltersBar.appendChild(chip);
      });

      const clearAll = document.createElement("button");
      clearAll.type = "button";
      clearAll.className = "filter-clear-all";
      clearAll.textContent = "Clear all filters";
      clearAll.onclick = function () {
        if (search) {
          search.value = "";
          if (searchWrapper) searchWrapper.classList.remove("has-val");
        }
        selects.forEach(s => s.value = "");
        applyFromStart();
      };
      activeFiltersBar.appendChild(clearAll);
      activeFiltersBar.classList.add("has-filters");
    } else {
      activeFiltersBar.innerHTML = "";
      activeFiltersBar.classList.remove("has-filters");
    }
  }

  function matches(row) {
    const q = (search && search.value || "").trim().toLowerCase();
    if (q) {
      const volMatch = row.dataset.volume && row.dataset.volume.includes(q);
      const instMatch = row.dataset.instance && row.dataset.instance.includes(q);
      const rowTextMatch = row.textContent.toLowerCase().includes(q);
      if (!volMatch && !instMatch && !rowTextMatch) return false;
    }
    for (const sel of selects) {
      if (sel.value && row.dataset[sel.dataset.field] !== sel.value) return false;
    }
    return true;
  }

  function apply() {
    if (searchWrapper && search) {
      searchWrapper.classList.toggle("has-val", Boolean(search.value.trim()));
    }
    renderActiveChips();
    const rows = getRows();
    const matched = rows.filter(matches);
    const totalPages = Math.max(1, Math.ceil(matched.length / PAGE_SIZE));
    if (page > totalPages) page = totalPages;
    const start = (page - 1) * PAGE_SIZE;
    const pageRows = new Set(matched.slice(start, start + PAGE_SIZE));

    for (const row of rows) {
      row.classList.toggle("is-hidden", !pageRows.has(row));
    }

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
        label.textContent = (start + 1) + "–" + Math.min(start + PAGE_SIZE, matched.length) + " of " + matched.length;
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

// ---- Dialog focus management ---------------------------------------------------

// Every dialog (the drawer and both modals) goes through these two calls:
// opening moves focus inside and makes everything behind it inert (so Tab
// and screen-reader browsing can't wander behind the overlay); closing
// undoes that and puts focus back on whatever opened it.
const _dialogStack = [];
const _INERTABLE = [".shell", "#batch-action-bar", "#drawer-root", "#shortcuts-modal", "#detail-modal"];

function _applyInert() {
  const top = _dialogStack.length ? _dialogStack[_dialogStack.length - 1].el : null;
  _INERTABLE.forEach(function (sel) {
    const el = document.querySelector(sel);
    if (el) el.inert = !!top && !el.contains(top) && !top.contains(el);
  });
}

function dialogOpen(el, focusEl) {
  if (_dialogStack.some(function (d) { return d.el === el; })) return;
  _dialogStack.push({ el: el, opener: document.activeElement });
  _applyInert();
  (focusEl || el).focus({ preventScroll: true });
}

function dialogClose(el) {
  const i = _dialogStack.findIndex(function (d) { return d.el === el; });
  if (i === -1) return;
  const opener = _dialogStack.splice(i, 1)[0].opener;
  _applyInert();
  if (opener && opener !== document.body && document.contains(opener)) opener.focus({ preventScroll: true });
}

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
  const drawerEl = document.getElementById("drawer");
  drawerEl.classList.add("is-open");
  dialogOpen(drawerEl, document.getElementById("drawer-close"));
}

function closeDrawer() {
  const overlay = document.getElementById("drawer-overlay");
  const drawer = document.getElementById("drawer");
  if (overlay) overlay.classList.remove("is-open");
  if (drawer && drawer.classList.contains("is-open")) {
    drawer.classList.remove("is-open");
    dialogClose(drawer);
  }
}

// ---- Toast Notifications ---------------------------------------------------

function showToast(message, type) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = "toast" + (type ? " toast-" + type : "");
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(function () {
    toast.classList.add("toast-out");
    setTimeout(function () { toast.remove(); }, 160);
  }, 2400);
}

function copyToClipboard(text, btn) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(function () {
    btn.classList.add("is-copied");
    showToast("Copied to clipboard", "success");
    setTimeout(function () { btn.classList.remove("is-copied"); }, 1400);
  });
}

// The setup guide's "Copy" buttons next to a policy snippet.
function wireCodeCopyButtons(root) {
  root.querySelectorAll(".code-copy-btn").forEach(function (btn) {
    if (btn.dataset.copyWired) return;
    btn.dataset.copyWired = "1";
    btn.addEventListener("click", function () {
      const target = document.getElementById(btn.dataset.copyTarget);
      if (target) copyToClipboard(target.textContent, btn);
    });
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
  // A limit cell holds an input, a hidden "80% (default)" and a visible "(default)" note;
  // the hidden span already says it all, so reading the whole cell repeated the note.
  const limit = cell && cell.querySelector(".limit-text");
  const text = limit ? limit.textContent.trim() : cell && cell.textContent.trim();
  return text || "–";
}

function wireInlineCopyButtons(root) {
  root.querySelectorAll(".btn-inline-copy").forEach(function (btn) {
    if (btn.dataset.wired) return;
    btn.dataset.wired = "1";
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      const text = btn.dataset.copy;
      if (!text || !navigator.clipboard) return;
      navigator.clipboard.writeText(text).then(function () {
        btn.classList.add("is-copied");
        showToast("Copied " + text, "success");
        setTimeout(function () { btn.classList.remove("is-copied"); }, 1400);
      });
    });
  });
}

function wireRowDrawer(tableId) {
  const table = document.getElementById(tableId);
  if (!table || table.dataset.drawerWired) return;
  table.dataset.drawerWired = "1";
  table.addEventListener("click", function (e) {
    if (e.target.closest("input, select, button, a, label, .btn-inline-copy, .select-col")) return;
    const row = e.target.closest("tbody tr");
    if (!row || !table.contains(row) || row.classList.contains("empty-row")) return;
    const headerCells = Array.from(table.tHead.rows[0].cells).filter(c => !c.classList.contains("select-col") && !c.classList.contains("action-col"));
    const dataCells = Array.from(row.cells).filter(c => !c.classList.contains("select-col") && !c.classList.contains("action-col"));
    const headers = headerCells.map(th => th.textContent.trim());
    const idCell = dataCells[0];
    const id = (idCell && idCell.dataset.id) || (idCell && idCell.textContent.trim());
    if (!id) return;
    const fields = headers
      .slice(1)
      .map((k, i) => ({ k: k, v: _drawerCellValue(k, dataCells[i + 1], row) }))
      .filter((f) => f.k !== "Name");
    const nameIndex = headers.indexOf("Name");
    let title = id;
    if (nameIndex > 0 && dataCells[nameIndex]) {
      const n = dataCells[nameIndex].textContent.trim();
      if (n && n !== "–") title = n;
    } else if (idCell && idCell.dataset.id && idCell.textContent.trim() !== id) {
      title = idCell.textContent.trim();
    }
    openDrawer(id, title, fields);
  });
}

function wireAllDrawers() {
  wireDrawerChrome();
  DRAWER_TABLES.forEach(wireRowDrawer);
}

function markClickableRows() {
  DRAWER_TABLES.forEach(function (id) {
    const table = document.getElementById(id);
    if (!table) return;
    Array.from(table.tBodies[0].rows).forEach(function (r) {
      if (r.classList.contains("empty-row")) return;
      r.classList.add("is-clickable");
      r.tabIndex = 0;
    });
  });
}

function initThemeToggle() {
  const toggleBtn = document.getElementById("theme-toggle");
  if (!toggleBtn) return;
  const savedTheme = localStorage.getItem("spaceshift-theme");
  if (savedTheme) {
    document.documentElement.setAttribute("data-theme", savedTheme);
  }
  toggleBtn.addEventListener("click", function () {
    const current = document.documentElement.getAttribute("data-theme");
    let next;
    if (current === "light") {
      next = "dark";
    } else if (current === "dark") {
      next = "light";
    } else {
      next = window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("spaceshift-theme", next);
    showToast("Theme set to " + next, "info");
  });
}

// ---- Table Sorting ---------------------------------------------------------

function wireTableSorting() {
  document.querySelectorAll("table").forEach(function (table) {
    const thead = table.tHead;
    if (!thead || table.dataset.sortWired) return;
    table.dataset.sortWired = "1";
    const headers = Array.from(thead.rows[0].cells);

    headers.forEach(function (th, colIdx) {
      if (th.textContent.trim().toLowerCase() === "autoresize") return;
      th.classList.add("is-sortable");

      th.addEventListener("click", function () {
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const rows = Array.from(tbody.rows).filter(r => !r.classList.contains("empty-row"));
        if (rows.length <= 1) return;

        const isAsc = th.classList.contains("sort-asc");
        headers.forEach(h => h.classList.remove("sort-asc", "sort-desc"));
        th.classList.add(isAsc ? "sort-desc" : "sort-asc");
        const dir = isAsc ? -1 : 1;

        rows.sort(function (a, b) {
          const cellA = (a.cells[colIdx] && a.cells[colIdx].textContent.trim()) || "";
          const cellB = (b.cells[colIdx] && b.cells[colIdx].textContent.trim()) || "";

          const numA = parseFloat(cellA.replace(/[^0-9.-]/g, ""));
          const numB = parseFloat(cellB.replace(/[^0-9.-]/g, ""));
          if (!isNaN(numA) && !isNaN(numB) && cellA.match(/\d/) && cellB.match(/\d/)) {
            return (numA - numB) * dir;
          }
          return cellA.localeCompare(cellB) * dir;
        });

        rows.forEach(r => tbody.appendChild(r));
        const controller = tableControllers[table.id];
        if (controller && controller.apply) controller.apply();
      });
    });
  });
}

// ---- CSV Export ------------------------------------------------------------

function wireExportCSVButtons() {
  document.querySelectorAll(".export-csv-btn").forEach(function (btn) {
    if (btn.dataset.wired) return;
    btn.dataset.wired = "1";
    btn.addEventListener("click", function () {
      const tableId = btn.dataset.table;
      const filename = btn.dataset.filename || "export.csv";
      const table = document.getElementById(tableId);
      if (!table) return;

      const headers = Array.from(table.tHead.rows[0].cells).map(th => th.textContent.trim()).filter(h => h.toLowerCase() !== "autoresize");
      const rows = Array.from(table.tBodies[0].rows).filter(r => !r.classList.contains("empty-row") && !r.classList.contains("is-hidden"));

      if (rows.length === 0) {
        showToast("No visible data to export", "error");
        return;
      }

      const csvRows = [headers.map(h => '"' + h.replace(/"/g, '""') + '"').join(",")];

      rows.forEach(function (row) {
        const cells = Array.from(row.cells).map(c => c.textContent.trim().replace(/\s+/g, " "));
        const rowData = cells.slice(0, headers.length).map(v => '"' + v.replace(/"/g, '""') + '"');
        csvRows.push(rowData.join(","));
      });

      const blob = new Blob([csvRows.join("\n")], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast("Exported " + rows.length + " rows to CSV", "success");
    });
  });
}

// ---- Shortcuts Modal & Keyboard Handlers -----------------------------------

function wireShortcutsModal() {
  const modal = document.getElementById("shortcuts-modal");
  if (!modal || modal.dataset.wired) return;
  modal.dataset.wired = "1";

  function openShortcuts() {
    modal.classList.remove("is-hidden");
    dialogOpen(modal, document.getElementById("shortcuts-close"));
  }
  function closeShortcuts() {
    if (modal.classList.contains("is-hidden")) return;
    modal.classList.add("is-hidden");
    dialogClose(modal);
  }

  const closeBtn = document.getElementById("shortcuts-close");
  const overlay = document.getElementById("shortcuts-overlay");
  if (closeBtn) closeBtn.addEventListener("click", closeShortcuts);
  if (overlay) overlay.addEventListener("click", closeShortcuts);

  document.addEventListener("keydown", function (e) {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || document.activeElement.isContentEditable) {
      return;
    }
    if (e.key === "?" || (e.shiftKey && e.key === "?") || (e.key === "/" && e.shiftKey)) {
      e.preventDefault();
      modal.classList.contains("is-hidden") ? openShortcuts() : closeShortcuts();
    } else if (e.key === "Escape") {
      closeShortcuts();
    } else if (e.key === "t" || e.key === "T") {
      const toggleBtn = document.getElementById("theme-toggle");
      if (toggleBtn) toggleBtn.click();
    } else if (e.key === "r" || e.key === "R") {
      e.preventDefault();
      showToast("Refreshing data…", "info");
      refreshLiveData();
    }
  });
}

function initKeyboardShortcuts() {
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && !e.shiftKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && !document.activeElement.isContentEditable) {
      const searchBox = document.querySelector("input[type='search']");
      if (searchBox) {
        e.preventDefault();
        searchBox.focus();
        searchBox.select();
      }
    }
  });
}


// Rows that open a drawer or a detail dialog on click do the same on
// Enter or Space when focused, so they aren't mouse-only.
// A scrolling table or code block with nothing focusable inside can't be
// scrolled from the keyboard. Give an overflowing one a tab stop and a name;
// regions that already contain a link, button or field are left alone.
const _FOCUSABLE = "a[href], button, input:not([type=hidden]), select, textarea, [tabindex]";

function markScrollRegions(root) {
  (root || document).querySelectorAll(".panel, .code-block pre").forEach(function (el) {
    const overflows = el.scrollWidth > el.clientWidth + 1;
    if (overflows && !el.querySelector(_FOCUSABLE) && !el.hasAttribute("tabindex")) {
      const scope = el.closest(".step-body") || el.closest("section");
      const heading = scope && scope.querySelector(".step-title, h2");
      let name = heading ? heading.textContent.trim() : el.matches("pre") ? "Code" : "Table";
      // Several blocks under one heading need distinct names to be told apart.
      const siblings = scope ? Array.from(scope.querySelectorAll(".code-block pre")) : [];
      if (el.matches("pre") && siblings.length > 1) name += ", code " + (siblings.indexOf(el) + 1);
      el.tabIndex = 0;
      el.setAttribute("role", "region");
      el.setAttribute("aria-label", name + " (scrolls sideways)");
      el.dataset.scrollRegion = "1";
    } else if (!overflows && el.dataset.scrollRegion) {
      el.removeAttribute("tabindex");
      el.removeAttribute("role");
      el.removeAttribute("aria-label");
      delete el.dataset.scrollRegion;
    }
  });
}

function wireRowKeyboard() {
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest && e.target.closest("tr.is-clickable, tr.audit-row");
    if (!row || e.target !== row) return;
    e.preventDefault();
    row.click();
  });
}
