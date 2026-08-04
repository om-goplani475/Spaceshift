"""Optional read-only status dashboard - shows recent resize activity and
scan history from the state backend. Entirely separate from the scheduler
process; run it only if you want visibility beyond the logs.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template_string, request

from controller.state.base import TERMINAL_STATUSES, ResizeStatus, StateBackend

_SCAN_LIST_LIMIT = 200

_PAGE_TEMPLATE = """
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ebs-auto-resize status</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f7f9; --panel: #ffffff; --border: #e3e6ea; --text: #1c2128;
    --muted: #6e7781; --accent: #0969da; --green: #1a7f37; --red: #cf222e; --amber: #9a6700;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3;
      --muted: #8b949e; --accent: #4493f8; --green: #3fb950; --red: #f85149; --amber: #d29922;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    margin: 0; padding: 2rem clamp(1rem, 4vw, 3rem);
  }
  h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
  .subtitle { color: var(--muted); margin: 0 0 1.75rem; font-size: 0.9rem; }
  h2 { font-size: 1.05rem; margin: 2rem 0 0.6rem; }
  h2 small { color: var(--muted); font-weight: normal; font-size: 0.8rem; }

  .stats { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 0.5rem; }
  .stat {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 0.85rem 1.1rem; min-width: 140px; flex: 1;
  }
  .stat .n { font-size: 1.6rem; font-weight: 600; line-height: 1.2; }
  .stat .l { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }
  .stat.warn .n { color: var(--red); }

  .filters { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.6rem; }
  .filters input, .filters select {
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 0.4rem 0.65rem; font-size: 0.85rem; font-family: inherit;
  }
  .filters input { flex: 1; min-width: 180px; }
  .filters select { min-width: 120px; }
  .filters .count {
    color: var(--muted); font-size: 0.8rem; align-self: center; margin-left: auto; white-space: nowrap;
  }

  .pager {
    display: flex; align-items: center; justify-content: flex-end; gap: 0.6rem;
    margin-top: 0.6rem; font-size: 0.82rem; color: var(--muted);
  }
  .pager button {
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 0.3rem 0.7rem; font-size: 0.82rem; font-family: inherit; cursor: pointer;
  }
  .pager button:disabled { opacity: 0.4; cursor: default; }

  .panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    overflow-x: auto;
  }
  table { border-collapse: collapse; width: 100%; font-size: 0.88rem; white-space: nowrap; }
  th, td { text-align: left; padding: 0.55rem 0.9rem; border-bottom: 1px solid var(--border); }
  th {
    color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase;
    letter-spacing: 0.03em; position: sticky; top: 0; background: var(--panel);
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
  tbody tr.is-hidden { display: none; }
  td.mono, .id { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.83rem; }
  td.empty { color: var(--muted); text-align: center; padding: 1.25rem; }

  .badge {
    display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
    font-size: 0.75rem; font-weight: 600; white-space: nowrap;
  }
  .badge-green { background: color-mix(in srgb, var(--green) 15%, transparent); color: var(--green); }
  .badge-red { background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red); }
  .badge-amber { background: color-mix(in srgb, var(--amber) 15%, transparent); color: var(--amber); }
  .badge-muted { background: color-mix(in srgb, var(--muted) 15%, transparent); color: var(--muted); }

  .switch { position: relative; display: inline-block; width: 34px; height: 20px; vertical-align: middle; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch .slider {
    position: absolute; inset: 0; background: var(--border); cursor: pointer;
    transition: background 0.15s; border-radius: 999px;
  }
  .switch .slider::before {
    content: ""; position: absolute; height: 14px; width: 14px; left: 3px; bottom: 3px;
    background: var(--panel); transition: transform 0.15s; border-radius: 50%;
  }
  .switch input:checked + .slider { background: var(--green); }
  .switch input:checked + .slider::before { transform: translateX(14px); }
  .switch input:disabled + .slider { opacity: 0.5; cursor: default; }

  .tabs { display: flex; gap: 0.25rem; margin: 2rem 0 0.75rem; border-bottom: 1px solid var(--border); }
  .tab-btn {
    background: none; border: none; padding: 0.55rem 1rem; font-size: 0.9rem; font-weight: 600;
    color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px;
    font-family: inherit;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .tab-panel h2 { margin-top: 0; }
</style>

<h1>ebs-auto-resize</h1>
<p class="subtitle">Live resize activity and scan history from the controller.</p>

<div class="stats">
  <div class="stat"><div class="n">{{ stats.tracked }}</div><div class="l">Volumes scanned</div></div>
  <div class="stat"><div class="n">{{ stats.autoresize_enabled }}</div><div class="l">Autoresize enabled</div></div>
  <div class="stat"><div class="n">{{ stats.completed }}</div><div class="l">Resizes completed</div></div>
  <div class="stat"><div class="n">{{ stats.in_progress }}</div><div class="l">In progress</div></div>
  <div class="stat {{ 'warn' if stats.needs_attention else '' }}">
    <div class="n">{{ stats.needs_attention }}</div><div class="l">Needs attention</div>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" data-tab="instances-panel">Instances</button>
  <button class="tab-btn" data-tab="volumes-panel">Volumes</button>
</div>

<div class="tab-panel active" id="instances-panel">
<h2>Instances <small>toggle autoresize to make an instance's volumes eligible</small></h2>
<div class="filters">
  <input type="text" id="instances-search" placeholder="Search instance ID...">
  <select id="instances-account-filter">
    <option value="">All accounts</option>
    {% for account in instance_accounts %}<option value="{{ account }}">{{ account }}</option>{% endfor %}
  </select>
  <select id="instances-region-filter">
    <option value="">All regions</option>
    {% for region in instance_regions %}<option value="{{ region }}">{{ region }}</option>{% endfor %}
  </select>
  <select id="instances-autoresize-filter">
    <option value="">Any autoresize</option>
    <option value="true">Enabled</option>
    <option value="false">Disabled</option>
  </select>
  <span class="count" id="instances-count"></span>
</div>
<div class="panel">
<table id="instances-table">
  <thead><tr>
    <th>Instance</th><th>Account</th><th>Region</th><th>Type</th><th>State</th><th>Threshold</th>
    <th>Autoresize</th><th>Last seen</th>
  </tr></thead>
  <tbody>
  {% for i in instances %}
  <tr data-volume="" data-instance="{{ i.instance_id|lower }}" data-account="{{ i.account_name }}"
      data-region="{{ i.region }}" data-autoresize="{{ i.autoresize|lower }}">
    <td class="mono">{{ i.instance_id }}</td>
    <td>{{ i.account_name }}</td>
    <td>{{ i.region }}</td>
    <td>{{ i.instance_type }}</td>
    <td>{{ i.state }}</td>
    <td>{{ (i.threshold_percent ~ "%") if i.threshold_percent is not none else "default" }}</td>
    <td>
      <label class="switch">
        <input type="checkbox" class="autoresize-toggle" data-kind="instances" data-id="{{ i.instance_id }}"
               {{ "checked" if i.autoresize }}>
        <span class="slider"></span>
      </label>
    </td>
    <td class="ts" data-ts="{{ i.last_seen_at.isoformat() }}"></td>
  </tr>
  {% else %}
  <tr><td class="empty" colspan="8">No instances discovered yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>
<div class="pager" id="instances-pager"></div>
</div>

<div class="tab-panel" id="volumes-panel">
<h2>Volumes <small>toggle autoresize per-volume, overrides its instance</small></h2>
<div class="filters">
  <input type="text" id="volumes-search" placeholder="Search volume or instance ID...">
  <select id="volumes-account-filter">
    <option value="">All accounts</option>
    {% for account in volume_accounts %}<option value="{{ account }}">{{ account }}</option>{% endfor %}
  </select>
  <select id="volumes-region-filter">
    <option value="">All regions</option>
    {% for region in volume_regions %}<option value="{{ region }}">{{ region }}</option>{% endfor %}
  </select>
  <select id="volumes-autoresize-filter">
    <option value="">Any autoresize</option>
    <option value="true">Enabled</option>
    <option value="false">Disabled</option>
  </select>
  <span class="count" id="volumes-count"></span>
</div>
<div class="panel">
<table id="volumes-table">
  <thead><tr>
    <th>Volume</th><th>Instance</th><th>Account</th><th>Region</th><th>Size</th><th>Device</th>
    <th>Threshold</th><th>Autoresize</th><th>Last seen</th>
  </tr></thead>
  <tbody>
  {% for v in volumes %}
  <tr data-volume="{{ v.volume_id|lower }}" data-instance="{{ (v.instance_id or '')|lower }}"
      data-account="{{ v.account_name }}" data-region="{{ v.region }}" data-autoresize="{{ v.autoresize|lower }}">
    <td class="mono">{{ v.volume_id }}</td>
    <td class="mono">{{ v.instance_id or "-" }}</td>
    <td>{{ v.account_name }}</td>
    <td>{{ v.region }}</td>
    <td>{{ v.size_gb }}GB</td>
    <td>{{ v.device_name or "-" }}</td>
    <td>{{ (v.threshold_percent ~ "%") if v.threshold_percent is not none else "default" }}</td>
    <td>
      <label class="switch">
        <input type="checkbox" class="autoresize-toggle" data-kind="volumes" data-id="{{ v.volume_id }}"
               {{ "checked" if v.autoresize }}>
        <span class="slider"></span>
      </label>
    </td>
    <td class="ts" data-ts="{{ v.last_seen_at.isoformat() }}"></td>
  </tr>
  {% else %}
  <tr><td class="empty" colspan="9">No volumes discovered yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>
<div class="pager" id="volumes-pager"></div>
</div>

<h2>Resize activity</h2>
<div class="filters">
  <input type="text" id="resize-search" placeholder="Search volume or instance...">
  <select id="resize-status-filter">
    <option value="">All statuses</option>
    {% for status in resize_statuses %}<option value="{{ status }}">{{ status }}</option>{% endfor %}
  </select>
  <span class="count" id="resize-count"></span>
</div>
<div class="panel">
<table id="resize-table">
  <thead><tr>
    <th>Volume</th><th>Instance</th><th>Status</th><th>Size</th><th>Extend attempts</th><th>Updated</th>
  </tr></thead>
  <tbody>
  {% for r in records %}
  <tr data-volume="{{ r.volume_id|lower }}" data-instance="{{ r.instance_id|lower }}"
      data-status="{{ r.status.value }}">
    <td class="mono">{{ r.volume_id }}</td>
    <td class="mono">{{ r.instance_id }}</td>
    <td><span class="badge {{ status_badge(r.status) }}">{{ r.status.value }}</span></td>
    <td>{{ r.from_size_gb }}GB &rarr; {{ r.to_size_gb }}GB</td>
    <td>{{ r.extend_attempts }}</td>
    <td class="ts" data-ts="{{ r.updated_at.isoformat() }}"></td>
  </tr>
  {% else %}
  <tr><td class="empty" colspan="6">No resize activity yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>
<div class="pager" id="resize-pager"></div>

<h2>Scan history <small>most recent {{ scan_limit }} scans, newest first</small></h2>
<div class="filters">
  <input type="text" id="scan-search" placeholder="Search volume or instance...">
  <select id="scan-account-filter">
    <option value="">All accounts</option>
    {% for account in scan_accounts %}<option value="{{ account }}">{{ account }}</option>{% endfor %}
  </select>
  <select id="scan-region-filter">
    <option value="">All regions</option>
    {% for region in scan_regions %}<option value="{{ region }}">{{ region }}</option>{% endfor %}
  </select>
  <select id="scan-size-filter">
    <option value="">All sizes</option>
    {% for size in scan_sizes %}<option value="{{ size }}">{{ size }}GB</option>{% endfor %}
  </select>
  <select id="scan-decision-filter">
    <option value="">All decisions</option>
    {% for decision in scan_decisions %}<option value="{{ decision }}">{{ decision }}</option>{% endfor %}
  </select>
  <span class="count" id="scan-count"></span>
</div>
<div class="panel">
<table id="scan-table">
  <thead><tr>
    <th>Volume</th><th>Instance</th><th>Account</th><th>Region</th><th>Size</th><th>Usage</th>
    <th>Threshold</th><th>Decision</th><th>Checked</th>
  </tr></thead>
  <tbody>
  {% for s in scans %}
  <tr data-volume="{{ s.volume_id|lower }}" data-instance="{{ s.instance_id|lower }}"
      data-account="{{ s.account_name }}" data-region="{{ s.region }}" data-size="{{ s.size_gb }}"
      data-decision="{{ s.decision }}">
    <td class="mono">{{ s.volume_id }}</td>
    <td class="mono">{{ s.instance_id }}</td>
    <td>{{ s.account_name }}</td>
    <td>{{ s.region }}</td>
    <td>{{ s.size_gb }}GB</td>
    <td>{{ (s.percent_used ~ "%") if s.percent_used is not none else "-" }}</td>
    <td>{{ s.threshold_percent }}%</td>
    <td><span class="badge {{ decision_badge(s.decision) }}">{{ s.decision }}</span></td>
    <td class="ts" data-ts="{{ s.timestamp.isoformat() }}"></td>
  </tr>
  {% else %}
  <tr><td class="empty" colspan="9">No scans recorded yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>
<div class="pager" id="scan-pager"></div>

<script>
document.querySelectorAll(".ts").forEach(function (el) {
  var d = new Date(el.dataset.ts);
  el.textContent = d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
});

const PAGE_SIZE = 25;

function wireFilters(tableId, searchId, selectIds, countId, pagerId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const rows = Array.from(table.tBodies[0].rows).filter(r => r.dataset.volume !== undefined);
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
    const matched = rows.filter(matches);
    const totalPages = Math.max(1, Math.ceil(matched.length / PAGE_SIZE));
    if (page > totalPages) page = totalPages;
    const start = (page - 1) * PAGE_SIZE;
    const pageRows = new Set(matched.slice(start, start + PAGE_SIZE));

    for (const row of rows) {
      row.classList.toggle("is-hidden", !pageRows.has(row));
    }
    if (countEl) {
      const shown = matched.length === 0 ? 0 : start + 1;
      const shownEnd = Math.min(start + PAGE_SIZE, matched.length);
      countEl.textContent = matched.length
        ? shown + "-" + shownEnd + " of " + matched.length + " (" + rows.length + " total)"
        : "0 of " + rows.length;
    }
    if (pagerEl) {
      pagerEl.innerHTML = "";
      if (totalPages > 1) {
        const prev = document.createElement("button");
        prev.textContent = "Prev";
        prev.disabled = page <= 1;
        prev.onclick = function () { page--; apply(); };
        const label = document.createElement("span");
        label.textContent = "Page " + page + " of " + totalPages;
        const next = document.createElement("button");
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
}

wireFilters("resize-table", "resize-search", ["resize-status-filter"], "resize-count", "resize-pager");
document.getElementById("resize-status-filter").dataset.field = "status";

wireFilters(
  "scan-table", "scan-search",
  ["scan-account-filter", "scan-region-filter", "scan-size-filter", "scan-decision-filter"],
  "scan-count", "scan-pager"
);
document.getElementById("scan-account-filter").dataset.field = "account";
document.getElementById("scan-region-filter").dataset.field = "region";
document.getElementById("scan-size-filter").dataset.field = "size";
document.getElementById("scan-decision-filter").dataset.field = "decision";

wireFilters(
  "instances-table", "instances-search",
  ["instances-account-filter", "instances-region-filter", "instances-autoresize-filter"],
  "instances-count", "instances-pager"
);
document.getElementById("instances-account-filter").dataset.field = "account";
document.getElementById("instances-region-filter").dataset.field = "region";
document.getElementById("instances-autoresize-filter").dataset.field = "autoresize";

wireFilters(
  "volumes-table", "volumes-search",
  ["volumes-account-filter", "volumes-region-filter", "volumes-autoresize-filter"],
  "volumes-count", "volumes-pager"
);
document.getElementById("volumes-account-filter").dataset.field = "account";
document.getElementById("volumes-region-filter").dataset.field = "region";
document.getElementById("volumes-autoresize-filter").dataset.field = "autoresize";

document.querySelectorAll(".autoresize-toggle").forEach(function (toggle) {
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
        alert("Failed to update autoresize for " + id + " - try again.");
      })
      .finally(function () {
        toggle.disabled = false;
      });
  });
});

document.querySelectorAll(".tab-btn").forEach(function (btn) {
  btn.addEventListener("click", function () {
    document.querySelectorAll(".tab-btn").forEach(function (b) { b.classList.remove("active"); });
    document.querySelectorAll(".tab-panel").forEach(function (p) { p.classList.remove("active"); });
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.add("active");
  });
});
</script>
"""

_STATUS_BADGES = {
    ResizeStatus.EXTEND_COMPLETED: "badge-green",
    ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION: "badge-red",
    ResizeStatus.EXTEND_FAILED: "badge-amber",
}

_DECISION_BADGES = {
    "would_resize": "badge-green",
    "resize_triggered": "badge-green",
    "needs_attention": "badge-red",
    "ssm_not_ready": "badge-amber",
    "resize_skipped": "badge-amber",
}


def _status_badge(status: ResizeStatus) -> str:
    return _STATUS_BADGES.get(status, "badge-muted")


def _decision_badge(decision: str) -> str:
    return _DECISION_BADGES.get(decision, "badge-muted")


def _compute_stats(records, scans, volumes) -> dict[str, int]:
    return {
        "tracked": len({s.volume_id for s in scans}),
        "autoresize_enabled": sum(1 for v in volumes if v.autoresize),
        "completed": sum(1 for r in records if r.status == ResizeStatus.EXTEND_COMPLETED),
        "needs_attention": sum(1 for r in records if r.status == ResizeStatus.EXTEND_FAILED_NEEDS_ATTENTION),
        "in_progress": sum(1 for r in records if r.status not in TERMINAL_STATUSES),
    }


def create_app(state: StateBackend) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        records = state.list_all()
        scans = state.list_scans(limit=_SCAN_LIST_LIMIT)
        instances = state.list_instances()
        volumes = state.list_volumes()
        return render_template_string(
            _PAGE_TEMPLATE,
            records=records,
            scans=scans,
            instances=instances,
            volumes=volumes,
            scan_limit=_SCAN_LIST_LIMIT,
            stats=_compute_stats(records, scans, volumes),
            status_badge=_status_badge,
            decision_badge=_decision_badge,
            resize_statuses=sorted({r.status.value for r in records}),
            scan_accounts=sorted({s.account_name for s in scans}),
            scan_regions=sorted({s.region for s in scans}),
            scan_sizes=sorted({s.size_gb for s in scans}),
            scan_decisions=sorted({s.decision for s in scans}),
            instance_accounts=sorted({i.account_name for i in instances}),
            instance_regions=sorted({i.region for i in instances}),
            volume_accounts=sorted({v.account_name for v in volumes}),
            volume_regions=sorted({v.region for v in volumes}),
        )

    @app.get("/api/status")
    def api_status():
        return jsonify(
            [
                {
                    "volume_id": r.volume_id,
                    "instance_id": r.instance_id,
                    "status": r.status.value,
                    "from_size_gb": r.from_size_gb,
                    "to_size_gb": r.to_size_gb,
                    "extend_attempts": r.extend_attempts,
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in state.list_all()
            ]
        )

    @app.get("/api/scans")
    def api_scans():
        return jsonify(
            [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "account_name": s.account_name,
                    "region": s.region,
                    "volume_id": s.volume_id,
                    "instance_id": s.instance_id,
                    "size_gb": s.size_gb,
                    "threshold_percent": s.threshold_percent,
                    "percent_used": s.percent_used,
                    "decision": s.decision,
                }
                for s in state.list_scans(limit=_SCAN_LIST_LIMIT)
            ]
        )

    @app.get("/api/instances")
    def api_instances():
        return jsonify(
            [
                {
                    "instance_id": i.instance_id,
                    "account_name": i.account_name,
                    "region": i.region,
                    "instance_type": i.instance_type,
                    "state": i.state,
                    "autoresize": i.autoresize,
                    "threshold_percent": i.threshold_percent,
                    "last_seen_at": i.last_seen_at.isoformat(),
                }
                for i in state.list_instances()
            ]
        )

    @app.get("/api/volumes")
    def api_volumes():
        return jsonify(
            [
                {
                    "volume_id": v.volume_id,
                    "instance_id": v.instance_id,
                    "account_name": v.account_name,
                    "region": v.region,
                    "size_gb": v.size_gb,
                    "device_name": v.device_name,
                    "autoresize": v.autoresize,
                    "threshold_percent": v.threshold_percent,
                    "last_seen_at": v.last_seen_at.isoformat(),
                }
                for v in state.list_volumes()
            ]
        )

    @app.post("/api/instances/<instance_id>/autoresize")
    def set_instance_autoresize(instance_id: str):
        autoresize = _parse_autoresize_body()
        if autoresize is None:
            return jsonify({"error": "body must be JSON {'autoresize': true|false}"}), 400
        if not state.set_instance_autoresize(instance_id, autoresize):
            return jsonify({"error": f"no known instance {instance_id!r}"}), 404
        return jsonify({"instance_id": instance_id, "autoresize": autoresize})

    @app.post("/api/volumes/<volume_id>/autoresize")
    def set_volume_autoresize(volume_id: str):
        autoresize = _parse_autoresize_body()
        if autoresize is None:
            return jsonify({"error": "body must be JSON {'autoresize': true|false}"}), 400
        if not state.set_volume_autoresize(volume_id, autoresize):
            return jsonify({"error": f"no known volume {volume_id!r}"}), 404
        return jsonify({"volume_id": volume_id, "autoresize": autoresize})

    return app


def _parse_autoresize_body() -> bool | None:
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("autoresize"), bool):
        return None
    return body["autoresize"]


if __name__ == "__main__":
    from controller.main import build_state_backend

    logging.basicConfig(level=logging.INFO)
    dashboard_state = build_state_backend()
    create_app(dashboard_state).run(host="0.0.0.0", port=8080)
