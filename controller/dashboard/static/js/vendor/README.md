Vendored third-party JS, so `docker compose up` serves everything from this
container - no CDN, no build step, no extra containers.

- `apexcharts.min.js` - [ApexCharts](https://apexcharts.com) 3.54.1, MIT licensed.
  Used for the Overview page's usage-breakdown and resize-activity charts.

To update: download the new `dist/apexcharts.min.js` from the matching
npm/jsdelivr release and replace the file in place.
