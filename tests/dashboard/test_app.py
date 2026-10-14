"""Tests for controller.dashboard."""

import re
from dataclasses import replace
from decimal import Decimal

from controller.config import DB_CONFIG_KEYS
from controller.dashboard import create_app
from controller.dashboard.config_support import _CONFIG_GROUPS
from controller.state.base import ResizeStatus
from tests.dashboard.conftest import sign_in


class _FakeCostEstimator:
    """Returns a fixed price for (region, volume_type), or None for
    anything else - stands in for the real AWS-Pricing-API-backed
    CostEstimator in tests, which never hit the network."""

    def __init__(self, price: Decimal | None = Decimal("0.08")):
        self._price = price

    def price_per_gb_month(self, region, volume_type):
        if volume_type is None:
            return None
        return self._price


def _client_with_cost_estimator(state, auth_settings, admin, cost_estimator):
    app = create_app(state, auth_settings, cost_estimator=cost_estimator)
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    assert sign_in(client).status_code == 302
    return client


def test_pages_render_empty_states(state, client):

    overview = client.get("/")
    assert overview.status_code == 200
    assert b"No resize activity yet" in overview.data
    assert b"No scans recorded yet" in overview.data

    instances = client.get("/instances")
    assert instances.status_code == 200
    assert b"No instances discovered yet" in instances.data

    volumes = client.get("/volumes")
    assert volumes.status_code == 200
    assert b"No volumes discovered yet" in volumes.data


def test_nav_marks_current_page(state, client):

    pages = [("/", "Dashboard"), ("/instances", "Instances"), ("/volumes", "Volumes"), ("/settings", "Settings")]
    for path, label in pages:
        html = client.get(path).data.decode()
        # The current nav link carries aria-current and, somewhere inside
        # it (after its icon), the page's label - not pinned to the exact
        # markup between them.
        assert re.search(rf'aria-current="page"[^>]*>.*?{re.escape(label)}</span></a>', html)
        # exactly one link is current
        assert html.count('aria-current="page"') == 1


def test_live_indicator_has_no_ticking_age_text(client):
    html = client.get("/").data.decode()
    assert 'id="live-label"' in html
    assert 'id="live-age"' not in html


def test_instances_page_shows_the_name_tag_or_a_dash(state, client):
    state.upsert_instance("default", "i-named", "us-east-1", "t3.micro", "running", name="web-1")
    state.upsert_instance("default", "i-unnamed", "us-east-1", "t3.micro", "running")

    html = client.get("/instances").data.decode()

    assert "web-1" in html
    row = html.split('data-instance="i-unnamed"')[1].split("</tr>")[0]
    assert "–" in row  # en dash placeholder for an instance with no Name tag


def test_volumes_page_shows_the_volumes_own_and_its_instances_name_tag(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running", name="web-1")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", name="data-1")

    html = client.get("/volumes").data.decode()
    row = html.split('data-volume="vol-1"')[1].split("</tr>")[0]

    assert "data-1" in row  # the volume's own Name tag
    assert "web-1" in row  # its attached instance's Name tag


def test_watched_toggle_carries_the_instance_autoresize_flag_for_optimistic_ui_updates(state, client):
    # instances.html has no Watched column to update optimistically, but
    # volumes.html does - the row needs to tell dashboard.js whether the
    # attached instance's own autoresize is on, since turning the volume's
    # own toggle off still leaves it watched "via instance" when it is.
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.set_instance_autoresize("i-1", True)
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = client.get("/volumes").data.decode()

    assert 'data-instance-autoresize="true"' in html


def test_index_renders_records(state, client):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)

    response = client.get("/")

    assert response.status_code == 200
    assert b"vol-1" in response.data


def test_api_status_returns_json(state, client):
    state.create_if_absent("vol-1", "i-1", 100, 120)

    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.get_json()
    assert data[0]["volume_id"] == "vol-1"


def test_index_renders_scan_history(state, client):
    state.record_scan("default", "us-east-1", "vol-9", "i-9", 100, 80, 42.0, "below_threshold")

    response = client.get("/")

    assert response.status_code == 200
    assert b"vol-9" in response.data
    assert b"below_threshold" in response.data
    # A hover tooltip on the decision badge, so "below_threshold" reads as
    # more than an internal code name.
    assert b"Usage is under the threshold" in response.data


def test_scan_history_shows_the_name_tag_instead_of_the_id_when_one_exists(state, client):
    state.upsert_instance("default", "i-9", "us-east-1", "t3.micro", "running", name="db-1")
    state.upsert_volume("default", "vol-9", "i-9", "us-east-1", 100, "/dev/xvdf", name="data-9")
    state.record_scan("default", "us-east-1", "vol-9", "i-9", 100, 80, 42.0, "below_threshold")

    html = client.get("/").data.decode()
    row = html.split('data-volume="vol-9"')[1].split("</tr>")[0]

    assert "data-9" in row and "db-1" in row
    assert 'data-id="vol-9"' in row  # the real id stays available for the drawer/CLI command


def test_scan_history_falls_back_to_the_id_when_untagged(state, client):
    state.record_scan("default", "us-east-1", "vol-9", "i-9", 100, 80, 42.0, "below_threshold")

    html = client.get("/").data.decode()
    row = html.split('data-volume="vol-9"')[1].split("</tr>")[0]

    assert "vol-9" in row and "i-9" in row


def test_api_scans_returns_json_newest_first(state, client):
    state.record_scan("default", "us-east-1", "vol-0", "i-1", 100, 80, 10, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 10, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-2", "i-1", 100, 80, 10, "below_threshold")

    response = client.get("/api/scans")

    assert response.status_code == 200
    data = response.get_json()
    assert [e["volume_id"] for e in data] == ["vol-2", "vol-1", "vol-0"]
    assert data[0]["account_name"] == "default"


def test_instances_and_volumes_pages_render_records(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    instances = client.get("/instances")
    assert instances.status_code == 200
    assert b"i-1" in instances.data
    assert b"t3.micro" in instances.data

    volumes = client.get("/volumes")
    assert volumes.status_code == 200
    assert b"vol-1" in volumes.data


# --- Last scan column (Instances/Volumes pages) -------------------------------


def test_instance_shows_not_yet_scanned_before_any_scan(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")

    html = client.get("/instances").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)

    assert "not yet scanned" in html[row_start:row_end]


def test_instance_shows_most_recent_scan_decision(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 95.0, "would_resize")

    html = client.get("/instances").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "would_resize" in row  # the newer of the two scans, not the older one
    assert "below_threshold" not in row


def test_instance_last_scan_prefers_ssm_not_ready_when_most_recent(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, None, "ssm_not_ready")

    html = client.get("/instances").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)

    assert "ssm_not_ready" in html[row_start:row_end]


def test_volume_shows_most_recent_scan_decision(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 95.0, "resize_triggered")

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "resize_triggered" in row
    assert "below_threshold" not in row


def test_volume_shows_not_yet_scanned_before_any_scan(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)

    assert "not yet scanned" in html[row_start:row_end]


def test_instances_page_hides_stopped_instances(state, client):
    state.upsert_instance("default", "i-running", "us-east-1", "t3.micro", "running")
    state.upsert_instance("default", "i-stopped", "us-east-1", "t3.micro", "stopped")

    response = client.get("/instances")

    assert b"i-running" in response.data
    assert b"i-stopped" not in response.data


def test_api_instances_still_includes_stopped(state, client):
    state.upsert_instance("default", "i-running", "us-east-1", "t3.micro", "running")
    state.upsert_instance("default", "i-stopped", "us-east-1", "t3.micro", "stopped")

    response = client.get("/api/instances")

    instance_ids = {i["instance_id"] for i in response.get_json()}
    assert instance_ids == {"i-running", "i-stopped"}


def _stat(html: str, label: str) -> int:
    """Read one KPI tile's number by its label, so tests don't pin the
    tile's exact markup."""
    match = re.search(rf'class="l[^"]*">{re.escape(label)}</span>.*?class="n[^"]*">(\d+)</div>', html, re.DOTALL)
    assert match, f"no stat tile labelled {label!r}"
    return int(match.group(1))


def test_stats_count_autoresize_enabled_volumes(state, client):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.upsert_volume("default", "vol-2", "i-1", "us-east-1", 100, "/dev/xvdg")
    state.set_volume_autoresize("vol-1", True)

    response = client.get("/")

    assert response.status_code == 200
    assert _stat(response.data.decode(), "Autoresize enabled") == 1


def test_stats_autoresize_enabled_counts_volumes_watched_via_instance(state, client):
    # A volume watched only because its instance's flag is set (not its own)
    # used to not move this counter at all - the same gap the Volumes tab's
    # Watched column fixed. It must count here too.
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)

    response = client.get("/")

    assert _stat(response.data.decode(), "Autoresize enabled") == 1


def test_stats_autoresize_enabled_excludes_unattached_even_if_flagged(state, client):
    state.upsert_volume("default", "vol-1", None, "us-east-1", 100, None)
    state.set_volume_autoresize("vol-1", True)

    response = client.get("/")

    assert _stat(response.data.decode(), "Autoresize enabled") == 0


def test_api_instances_and_volumes_return_json(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    instances = client.get("/api/instances").get_json()
    volumes = client.get("/api/volumes").get_json()

    assert instances[0]["instance_id"] == "i-1"
    assert instances[0]["account_name"] == "default"
    assert volumes[0]["volume_id"] == "vol-1"
    assert volumes[0]["autoresize"] is False
    assert volumes[0]["account_name"] == "default"


def test_toggle_instance_autoresize(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")

    response = client.post("/api/instances/i-1/autoresize", json={"autoresize": True})

    assert response.status_code == 200
    assert response.get_json() == {"instance_id": "i-1", "autoresize": True}
    assert state.list_instances()[0].autoresize is True


def test_toggle_volume_autoresize(state, client):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    response = client.post("/api/volumes/vol-1/autoresize", json={"autoresize": True})

    assert response.status_code == 200
    assert response.get_json() == {"volume_id": "vol-1", "autoresize": True}
    assert state.list_volumes()[0].autoresize is True


def test_toggle_unknown_instance_returns_404(state, client):
    response = client.post("/api/instances/i-nonexistent/autoresize", json={"autoresize": True})
    assert response.status_code == 404


def test_toggle_unknown_volume_returns_404(state, client):
    response = client.post("/api/volumes/vol-nonexistent/autoresize", json={"autoresize": True})
    assert response.status_code == 404


def test_toggle_with_invalid_body_returns_400(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")

    response = client.post("/api/instances/i-1/autoresize", json={"autoresize": "not-a-bool"})

    assert response.status_code == 400


# --- Settings page (config-in-DB) --------------------------------------------


def test_settings_page_renders_every_key(state, client):
    response = client.get("/settings")

    assert response.status_code == 200
    for key in DB_CONFIG_KEYS:
        assert f'data-key="{key}"'.encode() in response.data


def test_settings_page_shows_labels_and_descriptions_not_raw_keys(state, client):
    html = client.get("/settings").data.decode()

    assert "Scan interval (sec)" in html  # human label
    assert "How often a full discovery-and-check cycle runs." in html  # description, not hidden in a tooltip
    assert "data-tooltip" not in html
    # The dotted key is an internal identifier - it drives data-key for the
    # JS, but isn't rendered as visible text next to the label.
    assert 'class="setting-key"' not in html
    assert ">scheduler.interval_seconds<" not in html


def test_settings_page_puts_dry_run_first(state, client):
    # dry_run is the one setting a first-time operator must see before
    # anything else, so the Safety group leads the page.
    html = client.get("/settings").data.decode()

    dry_run_position = html.index('data-key="dry_run"')
    for key in DB_CONFIG_KEYS:
        if key != "dry_run":
            assert html.index(f'data-key="{key}"') > dry_run_position


def test_discovery_honor_resource_tags_renders_as_a_toggle_not_a_text_input(state, client):
    html = client.get("/settings").data.decode()

    start = html.index('data-key="discovery.honor_resource_tags"')
    end = html.index("</div>", html.index("setting-control", start))
    # The checkbox switch markup (shared with dry_run) - a plain setting
    # would instead render a bare <input type="text" ...> in this block.
    assert 'type="checkbox"' in html[start:end]


def test_settings_groups_cover_every_key_exactly_once():
    grouped = [key for _, keys in _CONFIG_GROUPS for key in keys]
    assert sorted(grouped) == sorted(DB_CONFIG_KEYS)
    assert len(grouped) == len(set(grouped))


def test_settings_page_has_single_update_button_not_per_row_save(state, client):
    html = client.get("/settings").data.decode()

    assert 'id="settings-update-btn"' in html
    assert ">Update settings<" in html
    assert ">Save<" not in html  # the old per-row button text is gone


def test_page_links_its_static_assets(state, client):
    # The stylesheets and script live in static/ now - if any link breaks
    # (renamed file, wrong url_for), the page still renders 200 but unstyled
    html = client.get("/").data.decode()
    for css_file in ("dashboard-base.css", "dashboard-components.css", "dashboard-pages.css"):
        assert re.search(rf'<link rel="stylesheet" href="/static/css/{css_file}(\?[^"]*)?">', html)
        assert client.get(f"/static/css/{css_file}").status_code == 200
    for js_file in ("dashboard-utils.js", "dashboard-features.js", "dashboard-init.js"):
        assert re.search(rf'<script src="/static/js/{js_file}(\?[^"]*)?"></script>', html)
        assert client.get(f"/static/js/{js_file}").status_code == 200


def test_settings_script_has_dirty_tracking_and_batched_update(state, client):
    js = client.get("/static/js/dashboard-features.js").data.decode()

    assert "function wireConfigDirtyTracking" in js
    assert "function wireSettingsUpdateButton" in js
    assert "#settings-list .setting[data-dirty" in js


def test_api_config_lists_all_known_keys_with_defaults(state, client):
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.get_json()
    by_key = {row["key"]: row for row in data}
    assert by_key["dry_run"]["value"] is None
    assert by_key["dry_run"]["default"] == "true"
    assert by_key["dry_run"]["label"] == "Dry run"
    assert by_key["default_threshold_percent"]["default"] == "80"
    assert by_key["scheduler.interval_seconds"]["default"] == "300"


def test_api_config_reflects_current_override(state, client):
    state.set_config("dry_run", "false")

    response = client.get("/api/config")

    by_key = {row["key"]: row for row in response.get_json()}
    assert by_key["dry_run"]["value"] == "false"


def test_set_config_valid_value_persists(state, client):

    response = client.post("/api/config/dry_run", json={"value": "false"})

    assert response.status_code == 200
    assert response.get_json() == {"key": "dry_run", "value": "false", "warnings": []}
    assert state.get_config("dry_run") == "false"


def test_set_config_invalid_value_rejected_and_not_persisted(state, client):

    response = client.post("/api/config/scheduler.interval_seconds", json={"value": "not-a-number"})

    assert response.status_code == 400
    assert "error" in response.get_json()
    assert state.get_config("scheduler.interval_seconds") is None


def test_set_config_out_of_range_value_rejected(state, client):

    response = client.post("/api/config/default_threshold_percent", json={"value": "150"})

    assert response.status_code == 400
    assert state.get_config("default_threshold_percent") is None


def test_set_config_unknown_key_returns_404(state, client):

    response = client.post("/api/config/credentials.mode", json={"value": "static"})

    assert response.status_code == 404


def test_set_config_invalid_body_returns_400(state, client):

    response = client.post("/api/config/dry_run", json={"value": 123})

    assert response.status_code == 400


def test_set_config_does_not_clobber_other_overrides(state, client):

    client.post("/api/config/dry_run", json={"value": "false"})
    client.post("/api/config/scheduler.interval_seconds", json={"value": "60"})

    assert state.get_config("dry_run") == "false"
    assert state.get_config("scheduler.interval_seconds") == "60"


def test_set_config_overwrites_existing_value(state, client):

    client.post("/api/config/dry_run", json={"value": "false"})
    response = client.post("/api/config/dry_run", json={"value": "true"})

    assert response.status_code == 200
    assert state.get_config("dry_run") == "true"


# --- Live refresh (client-side polling) --------------------------------------


def test_pages_have_stable_ids_for_live_refresh_swap(state, client):
    # refreshLiveData() in dashboard.js re-fetches the current page and
    # swaps these exact ids' innerHTML - if one goes missing (e.g. a
    # template edit drops an id), live refresh silently stops updating
    # that section instead of erroring, so it's worth pinning here.

    expected = {
        "/": ['id="dashboard-stats"', 'id="resize-tbody"', 'id="scan-tbody"'],
        "/instances": ['id="instances-tbody"'],
        "/volumes": ['id="volumes-tbody"'],
        "/settings": ['id="settings-list"'],
    }
    for path, ids in expected.items():
        html = client.get(path).data.decode()
        for expected_id in ids:
            assert expected_id in html, f"{path} is missing {expected_id}"


def test_live_refresh_fetches_the_current_page(state, client):
    # Each page is its own route now - refreshing "/" from /instances
    # would swap in nothing (the ids don't match) and look like a dead poll.
    js = client.get("/static/js/dashboard-init.js").data.decode()
    assert "fetch(location.pathname + location.search)" in js


def test_script_includes_live_refresh(state, client):
    js = client.get("/static/js/dashboard-init.js").data.decode()

    assert "function refreshLiveData" in js
    assert "setInterval(refreshLiveData" in js
    assert "visibilitychange" in js
    assert "document.hidden" in js


# --- Effective eligibility (Volumes page Watched/Threshold columns) -----------


def test_volume_watched_via_own_flag(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_volume_autoresize("vol-1", True)

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" not in row


def test_volume_watched_via_instance_flag(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)
    # Volume's own flag is untouched (still False) - only the instance is
    # flagged - this is exactly the case that used to be invisible on the
    # Volumes tab (toggle shows off, but the volume is actually watched).

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" in row
    assert 'data-autoresize="false"' in row  # the toggle itself still reflects the volume's own (unset) flag


def test_volume_not_watched_when_neither_flag_set(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert ">No<" in row


def test_unattached_volume_shows_unattached_even_if_flagged(state, client):
    state.upsert_volume("default", "vol-1", None, "us-east-1", 100, None)
    state.set_volume_autoresize("vol-1", True)  # flagged, but nothing to extend the filesystem on

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Unattached" in row


def test_threshold_shows_own_value_without_annotation(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state._volumes["vol-1"] = replace(state._volumes["vol-1"], threshold_percent=42)

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert '<span class="limit-text" hidden>42%</span>' in row
    assert '<span class="eligibility-note limit-note"></span>' in row  # nothing inherited to annotate


def test_threshold_falls_back_to_instance_with_annotation(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state._instances["i-1"] = replace(state._instances["i-1"], threshold_percent=55)

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "55% (instance)" in row
    assert '<span class="eligibility-note limit-note">(instance)</span>' in row


def test_threshold_falls_back_to_global_default_with_annotation(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "80% (default)" in row  # dataclass default
    assert '<span class="eligibility-note limit-note">(default)</span>' in row


def test_threshold_default_reflects_app_config_override(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_config("default_threshold_percent", "65")

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "65%" in row


def test_threshold_default_falls_back_on_invalid_app_config_override(state, client):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_config("default_threshold_percent", "not-a-number")

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "80%" in row  # falls back to the dataclass default, not a crash


def test_watched_uses_full_instance_list_even_when_instance_is_stopped(state, client):
    # The Instances tab hides stopped instances, but discovery.py's own
    # eligibility resolution doesn't filter by instance state at all - the
    # Volumes tab's Watched/Threshold columns must match that real
    # behavior, not the Instances tab's display-only filtering.
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "stopped")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)

    html = client.get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" in row


def test_overview_shows_an_estimated_monthly_cost_card(state, auth_settings, admin):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", volume_type="gp3")
    client = _client_with_cost_estimator(state, auth_settings, admin, _FakeCostEstimator(Decimal("0.08")))

    html = client.get("/").data.decode()

    assert "$8.00" in html  # 100 GB * $0.08/GB-mo
    assert "Est. monthly EBS spend" in html


def test_overview_cost_card_flags_a_partial_estimate_when_a_volume_has_no_price(state, auth_settings, admin):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", volume_type="gp3")
    state.upsert_volume("default", "vol-2", "i-1", "us-east-1", 50, "/dev/xvdg", volume_type=None)
    client = _client_with_cost_estimator(state, auth_settings, admin, _FakeCostEstimator(Decimal("0.08")))

    html = client.get("/").data.decode()

    assert "~$8.00" in html
    assert "some volumes excluded" in html


def test_overview_cost_card_shows_a_dash_when_nothing_can_be_priced(state, auth_settings, admin):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf", volume_type=None)
    client = _client_with_cost_estimator(state, auth_settings, admin, _FakeCostEstimator())

    html = client.get("/").data.decode()

    assert "&mdash;" in html


def test_resize_row_shows_the_estimated_cost_delta(state, auth_settings, admin):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 120, "/dev/xvdf", volume_type="gp3")
    state.create_if_absent("vol-1", "i-1", 100, 120, "default", "us-east-1")
    client = _client_with_cost_estimator(state, auth_settings, admin, _FakeCostEstimator(Decimal("0.08")))

    html = client.get("/").data.decode()

    assert "+$1.60/mo" in html  # (120 - 100) GB * $0.08/GB-mo


def test_resize_row_shows_no_cost_delta_when_the_volume_has_no_known_price(state, auth_settings, admin):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 120, "/dev/xvdf", volume_type=None)
    state.create_if_absent("vol-1", "i-1", 100, 120, "default", "us-east-1")
    client = _client_with_cost_estimator(state, auth_settings, admin, _FakeCostEstimator())

    html = client.get("/").data.decode()

    assert "/mo</span>" not in html


def test_overview_serves_the_chart_stylesheet_the_csp_would_otherwise_block(state, client):
    # ApexCharts injects its own styles via a <style> tag, which the CSP
    # blocks - without this file the chart tooltip renders unstyled under
    # the chart and a stray crosshair line shows above it.
    response = client.get("/")
    html = response.data.decode()

    assert re.search(r'<link rel="stylesheet" href="/static/css/vendor/apexcharts\.css">', html)
    assert "style-src" not in response.headers["Content-Security-Policy"]  # still no 'unsafe-inline'
    assert client.get("/static/css/vendor/apexcharts.css").status_code == 200
