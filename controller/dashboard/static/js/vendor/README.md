Vendored third-party JS, so `docker compose up` serves everything from this
container - no CDN, no build step, no extra containers.

- `apexcharts.min.js` - [ApexCharts](https://apexcharts.com) 3.54.1, MIT licensed.
  Used for the Overview page's usage-breakdown and resize-activity charts.

- `../../css/vendor/apexcharts.css` - the matching 3.54.1 `dist/apexcharts.css`.
  ApexCharts normally injects these styles from JavaScript, which the
  dashboard's CSP (`default-src 'self'`) blocks; without the file, chart
  tooltips render unstyled under the chart and a stray crosshair line shows.

To update: download the new `dist/apexcharts.min.js` and `dist/apexcharts.css`
from the same npm/jsdelivr release and replace both files in place.
