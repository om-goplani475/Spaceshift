"use strict";

// Renders the Overview page's two ApexCharts from data attributes the
// template put on each container - kept out of an inline <script> block
// since the dashboard's CSP has no script-src 'unsafe-inline'.
(function () {
  function baseChartTheme() {
    var light = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
    return {
      theme: { mode: light ? "light" : "dark" },
      chart: { background: "transparent", toolbar: { show: false }, fontFamily: "inherit" },
      grid: { borderColor: "#2a2f39" },
      legend: { show: false },
    };
  }

  var usageEl = document.querySelector("#chart-usage");
  if (usageEl) {
    var breakdown = JSON.parse(usageEl.dataset.breakdown || "{}");
    var usageTotal = (breakdown.healthy || 0) + (breakdown.warning || 0) + (breakdown.critical || 0);
    if (usageTotal > 0 && window.ApexCharts) {
      new ApexCharts(usageEl, Object.assign({}, baseChartTheme(), {
        chart: Object.assign({}, baseChartTheme().chart, { type: "donut", height: 220 }),
        series: [breakdown.healthy || 0, breakdown.warning || 0, breakdown.critical || 0],
        labels: ["Healthy", "Warning", "Critical"],
        colors: ["#35d399", "#f0b93a", "#ff6b6b"],
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
        chart: Object.assign({}, baseChartTheme().chart, { type: "bar", height: 220 }),
        series: [{ name: "Resizes", data: activity.map(function (a) { return a.count; }) }],
        xaxis: { categories: activity.map(function (a) { return a.status; }) },
        plotOptions: { bar: { borderRadius: 4, columnWidth: "45%", distributed: true } },
        colors: ["#6c8dff", "#35d399", "#f0b93a", "#ff6b6b", "#7c5cff"],
        dataLabels: { enabled: false },
      })).render();
    } else {
      activityEl.innerHTML = '<div class="chart-empty">No resize activity yet.</div>';
    }
  }
})();
