"use strict";

// Renders the Overview page's two ApexCharts from data attributes the
// template put on each container - kept out of an inline <script> block
// since the dashboard's CSP has no script-src 'unsafe-inline'.
(function () {
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function isLight() {
    var forced = document.documentElement.getAttribute("data-theme");
    if (forced) return forced === "light";
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
  }

  // Same ink as the rest of the page: the charts read the CSS tokens, so they
  // follow the theme and the colour meanings (green ok, amber warn, red
  // critical, ultramarine = resizing) match the badges and the bay plan.
  function baseChartTheme() {
    return {
      theme: { mode: isLight() ? "light" : "dark" },
      chart: { background: "transparent", toolbar: { show: false }, fontFamily: cssVar("--font-sans") || "inherit", foreColor: cssVar("--text-2") },
      grid: { borderColor: cssVar("--border") },
      legend: { show: false },
    };
  }

  var STATUS_COLOR = {
    extend_completed: "--green",
    extend_failed_needs_attention: "--red",
    extend_failed: "--amber-fill",
    modify_failed: "--amber-fill",
  };

  var usageEl = document.querySelector("#chart-usage");
  if (usageEl) {
    var breakdown = JSON.parse(usageEl.dataset.breakdown || "{}");
    var usageTotal = (breakdown.healthy || 0) + (breakdown.warning || 0) + (breakdown.critical || 0);
    if (usageTotal > 0 && window.ApexCharts) {
      new ApexCharts(usageEl, Object.assign({}, baseChartTheme(), {
        chart: Object.assign({}, baseChartTheme().chart, { type: "donut", height: 220 }),
        series: [breakdown.healthy || 0, breakdown.warning || 0, breakdown.critical || 0],
        labels: ["Healthy", "Warning", "Critical"],
        colors: [cssVar("--green"), cssVar("--amber-fill"), cssVar("--red")],
        dataLabels: { enabled: false },
        stroke: { width: 0 },
        plotOptions: { pie: { donut: { size: "72%", labels: { show: true, total: { show: true, label: "Volumes" } } } } },
      })).render();
    } else {
      usageEl.innerHTML = '<div class="chart-empty">No usage data yet.</div>';
    }
  }

  var activityEl = document.querySelector("#chart-activity");
  if (activityEl) {
    var activity = JSON.parse(activityEl.dataset.activity || "[]");
    if (activity.length > 0 && window.ApexCharts) {
      new ApexCharts(activityEl, Object.assign({}, baseChartTheme(), {
        chart: Object.assign({}, baseChartTheme().chart, { type: "bar", height: Math.max(180, 56 * activity.length + 40) }),
        series: [{ name: "Resizes", data: activity.map(function (a) { return a.count; }) }],
        xaxis: { categories: activity.map(function (a) { return a.status.replace(/_/g, " "); }), labels: { formatter: function (v) { return Math.round(v); } }, tickAmount: 4 },
        plotOptions: { bar: { borderRadius: 0, horizontal: true, barHeight: "55%", distributed: true } },
        colors: activity.map(function (a) { return cssVar(STATUS_COLOR[a.status] || "--resizing-fill"); }),
        dataLabels: { enabled: false },
      })).render();
    } else {
      activityEl.innerHTML = '<div class="chart-empty">No resize activity yet.</div>';
    }
  }
})();
