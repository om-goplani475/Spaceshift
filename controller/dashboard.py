"""Optional read-only status dashboard - shows recent resize activity from
the state backend. Entirely separate from the scheduler process; run it
only if you want visibility beyond the logs.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template_string

from controller.state.base import StateBackend

_PAGE_TEMPLATE = """
<!doctype html>
<title>ebs-auto-resize status</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.4rem 0.8rem; border-bottom: 1px solid #ddd; }
  .status-EXTEND_COMPLETED { color: #1a7f37; }
  .status-EXTEND_FAILED_NEEDS_ATTENTION { color: #cf222e; font-weight: bold; }
  .status-EXTEND_FAILED { color: #bf8700; }
</style>
<h1>ebs-auto-resize status</h1>
<table>
  <tr><th>Volume</th><th>Instance</th><th>Status</th><th>Size</th><th>Extend attempts</th><th>Updated</th></tr>
  {% for r in records %}
  <tr class="status-{{ r.status.value }}">
    <td>{{ r.volume_id }}</td>
    <td>{{ r.instance_id }}</td>
    <td>{{ r.status.value }}</td>
    <td>{{ r.from_size_gb }}GB &rarr; {{ r.to_size_gb }}GB</td>
    <td>{{ r.extend_attempts }}</td>
    <td>{{ r.updated_at }}</td>
  </tr>
  {% else %}
  <tr><td colspan="6">No resize activity yet.</td></tr>
  {% endfor %}
</table>
"""


def create_app(state: StateBackend) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(_PAGE_TEMPLATE, records=state.list_all())

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

    return app


if __name__ == "__main__":
    from controller.main import build_state_backend

    logging.basicConfig(level=logging.INFO)
    dashboard_state = build_state_backend()
    create_app(dashboard_state).run(host="0.0.0.0", port=8080)
