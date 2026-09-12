"""Tests for controller.dashboard."""

import re
from dataclasses import replace

from controller.config import DB_CONFIG_KEYS
from controller.dashboard import create_app
from controller.dashboard.app import _CONFIG_GROUPS
from controller.state.base import ResizeStatus


def test_pages_render_empty_states(state):
    client = create_app(state).test_client()

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


def test_nav_marks_current_page(state):
    client = create_app(state).test_client()

    pages = [("/", "Overview"), ("/instances", "Instances"), ("/volumes", "Volumes"), ("/settings", "Settings")]
    for path, label in pages:
        html = client.get(path).data.decode()
        assert f'aria-current="page">{label}</a>' in html
        # exactly one link is current
        assert html.count('aria-current="page"') == 1


def test_index_renders_records(state):
    state.create_if_absent("vol-1", "i-1", 100, 120)
    state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"vol-1" in response.data


def test_api_status_returns_json(state):
    state.create_if_absent("vol-1", "i-1", 100, 120)

    client = create_app(state).test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.get_json()
    assert data[0]["volume_id"] == "vol-1"


def test_index_renders_scan_history(state):
    state.record_scan("default", "us-east-1", "vol-9", "i-9", 100, 80, 42.0, "below_threshold")

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"vol-9" in response.data
    assert b"below_threshold" in response.data


def test_api_scans_returns_json_newest_first(state):
    state.record_scan("default", "us-east-1", "vol-0", "i-1", 100, 80, 10, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 10, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-2", "i-1", 100, 80, 10, "below_threshold")

    client = create_app(state).test_client()
    response = client.get("/api/scans")

    assert response.status_code == 200
    data = response.get_json()
    assert [e["volume_id"] for e in data] == ["vol-2", "vol-1", "vol-0"]
    assert data[0]["account_name"] == "default"


def test_instances_and_volumes_pages_render_records(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    client = create_app(state).test_client()

    instances = client.get("/instances")
    assert instances.status_code == 200
    assert b"i-1" in instances.data
    assert b"t3.micro" in instances.data

    volumes = client.get("/volumes")
    assert volumes.status_code == 200
    assert b"vol-1" in volumes.data


# --- Last scan column (Instances/Volumes pages) -------------------------------


def test_instance_shows_not_yet_scanned_before_any_scan(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")

    html = create_app(state).test_client().get("/instances").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)

    assert "not yet scanned" in html[row_start:row_end]


def test_instance_shows_most_recent_scan_decision(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 95.0, "would_resize")

    html = create_app(state).test_client().get("/instances").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "would_resize" in row  # the newer of the two scans, not the older one
    assert "below_threshold" not in row


def test_instance_last_scan_prefers_ssm_not_ready_when_most_recent(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, None, "ssm_not_ready")

    html = create_app(state).test_client().get("/instances").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)

    assert "ssm_not_ready" in html[row_start:row_end]


def test_volume_shows_most_recent_scan_decision(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 95.0, "resize_triggered")

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "resize_triggered" in row
    assert "below_threshold" not in row


def test_volume_shows_not_yet_scanned_before_any_scan(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)

    assert "not yet scanned" in html[row_start:row_end]


def test_instances_page_hides_stopped_instances(state):
    state.upsert_instance("default", "i-running", "us-east-1", "t3.micro", "running")
    state.upsert_instance("default", "i-stopped", "us-east-1", "t3.micro", "stopped")

    client = create_app(state).test_client()
    response = client.get("/instances")

    assert b"i-running" in response.data
    assert b"i-stopped" not in response.data


def test_api_instances_still_includes_stopped(state):
    state.upsert_instance("default", "i-running", "us-east-1", "t3.micro", "running")
    state.upsert_instance("default", "i-stopped", "us-east-1", "t3.micro", "stopped")

    client = create_app(state).test_client()
    response = client.get("/api/instances")

    instance_ids = {i["instance_id"] for i in response.get_json()}
    assert instance_ids == {"i-running", "i-stopped"}


def _stat(html: str, label: str) -> int:
    """Read one stat tile's number by its label, so tests don't pin the
    tile's exact markup."""
    match = re.search(rf'<div class="l">{re.escape(label)}</div><div class="n">(\d+)</div>', html)
    assert match, f"no stat tile labelled {label!r}"
    return int(match.group(1))


def test_stats_count_autoresize_enabled_volumes(state):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.upsert_volume("default", "vol-2", "i-1", "us-east-1", 100, "/dev/xvdg")
    state.set_volume_autoresize("vol-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert _stat(response.data.decode(), "Autoresize enabled") == 1


def test_stats_autoresize_enabled_counts_volumes_watched_via_instance(state):
    # A volume watched only because its instance's flag is set (not its own)
    # used to not move this counter at all - the same gap the Volumes tab's
    # Watched column fixed. It must count here too.
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert _stat(response.data.decode(), "Autoresize enabled") == 1


def test_stats_autoresize_enabled_excludes_unattached_even_if_flagged(state):
    state.upsert_volume("default", "vol-1", None, "us-east-1", 100, None)
    state.set_volume_autoresize("vol-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert _stat(response.data.decode(), "Autoresize enabled") == 0


def test_api_instances_and_volumes_return_json(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    client = create_app(state).test_client()

    instances = client.get("/api/instances").get_json()
    volumes = client.get("/api/volumes").get_json()

    assert instances[0]["instance_id"] == "i-1"
    assert instances[0]["account_name"] == "default"
    assert volumes[0]["volume_id"] == "vol-1"
    assert volumes[0]["autoresize"] is False
    assert volumes[0]["account_name"] == "default"


def test_toggle_instance_autoresize(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    client = create_app(state).test_client()

    response = client.post("/api/instances/i-1/autoresize", json={"autoresize": True})

    assert response.status_code == 200
    assert response.get_json() == {"instance_id": "i-1", "autoresize": True}
    assert state.list_instances()[0].autoresize is True


def test_toggle_volume_autoresize(state):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    client = create_app(state).test_client()

    response = client.post("/api/volumes/vol-1/autoresize", json={"autoresize": True})

    assert response.status_code == 200
    assert response.get_json() == {"volume_id": "vol-1", "autoresize": True}
    assert state.list_volumes()[0].autoresize is True


def test_toggle_unknown_instance_returns_404(state):
    client = create_app(state).test_client()
    response = client.post("/api/instances/i-nonexistent/autoresize", json={"autoresize": True})
    assert response.status_code == 404


def test_toggle_unknown_volume_returns_404(state):
    client = create_app(state).test_client()
    response = client.post("/api/volumes/vol-nonexistent/autoresize", json={"autoresize": True})
    assert response.status_code == 404


def test_toggle_with_invalid_body_returns_400(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    client = create_app(state).test_client()

    response = client.post("/api/instances/i-1/autoresize", json={"autoresize": "not-a-bool"})

    assert response.status_code == 400


# --- Settings page (config-in-DB) --------------------------------------------


def test_settings_page_renders_every_key(state):
    client = create_app(state).test_client()
    response = client.get("/settings")

    assert response.status_code == 200
    for key in DB_CONFIG_KEYS:
        assert f'data-key="{key}"'.encode() in response.data


def test_settings_page_shows_labels_keys_and_descriptions_inline(state):
    client = create_app(state).test_client()
    html = client.get("/settings").data.decode()

    assert "Scan interval (sec)" in html  # human label
    assert "scheduler.interval_seconds" in html  # the key SQL/API use, still visible
    assert "How often a full discovery-and-check cycle runs." in html  # description, not hidden in a tooltip
    assert "data-tooltip" not in html


def test_settings_page_puts_dry_run_first(state):
    # dry_run is the one setting a first-time operator must see before
    # anything else, so the Safety group leads the page.
    client = create_app(state).test_client()
    html = client.get("/settings").data.decode()

    dry_run_position = html.index('data-key="dry_run"')
    for key in DB_CONFIG_KEYS:
        if key != "dry_run":
            assert html.index(f'data-key="{key}"') > dry_run_position


def test_settings_groups_cover_every_key_exactly_once():
    grouped = [key for _, keys in _CONFIG_GROUPS for key in keys]
    assert sorted(grouped) == sorted(DB_CONFIG_KEYS)
    assert len(grouped) == len(set(grouped))


def test_settings_page_has_single_update_button_not_per_row_save(state):
    client = create_app(state).test_client()
    html = client.get("/settings").data.decode()

    assert 'id="settings-update-btn"' in html
    assert ">Update settings<" in html
    assert ">Save<" not in html  # the old per-row button text is gone


def test_page_links_its_static_assets(state):
    # The stylesheet and script live in static/ now - if either link breaks
    # (renamed file, wrong url_for), the page still renders 200 but unstyled
    # and inert, which nothing else would catch.
    client = create_app(state).test_client()
    html = client.get("/").data.decode()

    assert '<link rel="stylesheet" href="/static/css/dashboard.css">' in html
    assert '<script src="/static/js/dashboard.js"></script>' in html
    assert client.get("/static/css/dashboard.css").status_code == 200
    assert client.get("/static/js/dashboard.js").status_code == 200


def test_settings_script_has_dirty_tracking_and_batched_update(state):
    client = create_app(state).test_client()
    js = client.get("/static/js/dashboard.js").data.decode()

    assert "function wireConfigDirtyTracking" in js
    assert "function wireSettingsUpdateButton" in js
    assert "#settings-list .setting[data-dirty" in js


def test_api_config_lists_all_known_keys_with_defaults(state):
    client = create_app(state).test_client()
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.get_json()
    by_key = {row["key"]: row for row in data}
    assert by_key["dry_run"]["value"] is None
    assert by_key["dry_run"]["default"] == "true"
    assert by_key["dry_run"]["label"] == "Dry run"
    assert by_key["default_threshold_percent"]["default"] == "80"
    assert by_key["scheduler.interval_seconds"]["default"] == "300"


def test_api_config_reflects_current_override(state):
    state.set_config("dry_run", "false")
    client = create_app(state).test_client()

    response = client.get("/api/config")

    by_key = {row["key"]: row for row in response.get_json()}
    assert by_key["dry_run"]["value"] == "false"


def test_set_config_valid_value_persists(state):
    client = create_app(state).test_client()

    response = client.post("/api/config/dry_run", json={"value": "false"})

    assert response.status_code == 200
    assert response.get_json() == {"key": "dry_run", "value": "false"}
    assert state.get_config("dry_run") == "false"


def test_set_config_invalid_value_rejected_and_not_persisted(state):
    client = create_app(state).test_client()

    response = client.post("/api/config/scheduler.interval_seconds", json={"value": "not-a-number"})

    assert response.status_code == 400
    assert "error" in response.get_json()
    assert state.get_config("scheduler.interval_seconds") is None


def test_set_config_out_of_range_value_rejected(state):
    client = create_app(state).test_client()

    response = client.post("/api/config/default_threshold_percent", json={"value": "150"})

    assert response.status_code == 400
    assert state.get_config("default_threshold_percent") is None


def test_set_config_unknown_key_returns_404(state):
    client = create_app(state).test_client()

    response = client.post("/api/config/credentials.mode", json={"value": "static"})

    assert response.status_code == 404


def test_set_config_invalid_body_returns_400(state):
    client = create_app(state).test_client()

    response = client.post("/api/config/dry_run", json={"value": 123})

    assert response.status_code == 400


def test_set_config_does_not_clobber_other_overrides(state):
    client = create_app(state).test_client()

    client.post("/api/config/dry_run", json={"value": "false"})
    client.post("/api/config/scheduler.interval_seconds", json={"value": "60"})

    assert state.get_config("dry_run") == "false"
    assert state.get_config("scheduler.interval_seconds") == "60"


def test_set_config_overwrites_existing_value(state):
    client = create_app(state).test_client()

    client.post("/api/config/dry_run", json={"value": "false"})
    response = client.post("/api/config/dry_run", json={"value": "true"})

    assert response.status_code == 200
    assert state.get_config("dry_run") == "true"


# --- Live refresh (client-side polling) --------------------------------------


def test_pages_have_stable_ids_for_live_refresh_swap(state):
    # refreshLiveData() in dashboard.js re-fetches the current page and
    # swaps these exact ids' innerHTML - if one goes missing (e.g. a
    # template edit drops an id), live refresh silently stops updating
    # that section instead of erroring, so it's worth pinning here.
    client = create_app(state).test_client()

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


def test_live_refresh_fetches_the_current_page(state):
    # Each page is its own route now - refreshing "/" from /instances
    # would swap in nothing (the ids don't match) and look like a dead poll.
    js = create_app(state).test_client().get("/static/js/dashboard.js").data.decode()
    assert "fetch(location.pathname)" in js


def test_script_includes_live_refresh(state):
    client = create_app(state).test_client()
    js = client.get("/static/js/dashboard.js").data.decode()

    assert "function refreshLiveData" in js
    assert "setInterval(refreshLiveData" in js
    assert "visibilitychange" in js
    assert "document.hidden" in js


# --- Effective eligibility (Volumes page Watched/Threshold columns) -----------


def test_volume_watched_via_own_flag(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_volume_autoresize("vol-1", True)

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" not in row


def test_volume_watched_via_instance_flag(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)
    # Volume's own flag is untouched (still False) - only the instance is
    # flagged - this is exactly the case that used to be invisible on the
    # Volumes tab (toggle shows off, but the volume is actually watched).

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" in row
    assert 'data-autoresize="false"' in row  # the toggle itself still reflects the volume's own (unset) flag


def test_volume_not_watched_when_neither_flag_set(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert ">No<" in row


def test_unattached_volume_shows_unattached_even_if_flagged(state):
    state.upsert_volume("default", "vol-1", None, "us-east-1", 100, None)
    state.set_volume_autoresize("vol-1", True)  # flagged, but nothing to extend the filesystem on

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Unattached" in row


def test_threshold_shows_own_value_without_annotation(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state._volumes["vol-1"] = replace(state._volumes["vol-1"], threshold_percent=42)

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "42%" in row
    assert "eligibility-note" not in row


def test_threshold_falls_back_to_instance_with_annotation(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state._instances["i-1"] = replace(state._instances["i-1"], threshold_percent=55)

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "55%" in row
    assert '<span class="eligibility-note">(instance)</span>' in row


def test_threshold_falls_back_to_global_default_with_annotation(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "80%" in row  # dataclass default
    assert '<span class="eligibility-note">(default)</span>' in row


def test_threshold_default_reflects_app_config_override(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_config("default_threshold_percent", "65")

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "65%" in row


def test_threshold_default_falls_back_on_invalid_app_config_override(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_config("default_threshold_percent", "not-a-number")

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "80%" in row  # falls back to the dataclass default, not a crash


def test_watched_uses_full_instance_list_even_when_instance_is_stopped(state):
    # The Instances tab hides stopped instances, but discovery.py's own
    # eligibility resolution doesn't filter by instance state at all - the
    # Volumes tab's Watched/Threshold columns must match that real
    # behavior, not the Instances tab's display-only filtering.
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "stopped")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)

    html = create_app(state).test_client().get("/volumes").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" in row
