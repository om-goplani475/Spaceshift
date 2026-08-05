"""Tests for controller.dashboard."""

from dataclasses import replace

from controller.dashboard import create_app
from controller.state.base import ResizeStatus


def test_index_with_no_records(state):
    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"No resize activity yet" in response.data
    assert b"No scans recorded yet" in response.data
    assert b"No instances discovered yet" in response.data
    assert b"No volumes discovered yet" in response.data


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


def test_index_renders_instances_and_volumes(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"i-1" in response.data
    assert b"vol-1" in response.data
    assert b"t3.micro" in response.data


# --- Last scan column (Instances/Volumes tabs) --------------------------------


def test_instance_shows_not_yet_scanned_before_any_scan(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)

    assert "not yet scanned" in html[row_start:row_end]


def test_instance_shows_most_recent_scan_decision(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 95.0, "would_resize")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "would_resize" in row  # the newer of the two scans, not the older one
    assert "below_threshold" not in row


def test_instance_last_scan_prefers_ssm_not_ready_when_most_recent(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, None, "ssm_not_ready")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-instance="i-1"')
    row_end = html.index("</tr>", row_start)

    assert "ssm_not_ready" in html[row_start:row_end]


def test_volume_shows_most_recent_scan_decision(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 50.0, "below_threshold")
    state.record_scan("default", "us-east-1", "vol-1", "i-1", 100, 80, 95.0, "resize_triggered")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "resize_triggered" in row
    assert "below_threshold" not in row


def test_volume_shows_not_yet_scanned_before_any_scan(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)

    assert "not yet scanned" in html[row_start:row_end]


def test_index_hides_stopped_instances(state):
    state.upsert_instance("default", "i-running", "us-east-1", "t3.micro", "running")
    state.upsert_instance("default", "i-stopped", "us-east-1", "t3.micro", "stopped")

    client = create_app(state).test_client()
    response = client.get("/")

    assert b"i-running" in response.data
    assert b"i-stopped" not in response.data


def test_api_instances_still_includes_stopped(state):
    state.upsert_instance("default", "i-running", "us-east-1", "t3.micro", "running")
    state.upsert_instance("default", "i-stopped", "us-east-1", "t3.micro", "stopped")

    client = create_app(state).test_client()
    response = client.get("/api/instances")

    instance_ids = {i["instance_id"] for i in response.get_json()}
    assert instance_ids == {"i-running", "i-stopped"}


def test_stats_count_autoresize_enabled_volumes(state):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.upsert_volume("default", "vol-2", "i-1", "us-east-1", 100, "/dev/xvdg")
    state.set_volume_autoresize("vol-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b'<div class="n">1</div><div class="l">Autoresize enabled</div>' in response.data


def test_stats_autoresize_enabled_counts_volumes_watched_via_instance(state):
    # A volume watched only because its instance's flag is set (not its own)
    # used to not move this counter at all - the same gap the Volumes tab's
    # Watched column fixed. It must count here too.
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_instance_autoresize("i-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert b'<div class="n">1</div><div class="l">Autoresize enabled</div>' in response.data


def test_stats_autoresize_enabled_excludes_unattached_even_if_flagged(state):
    state.upsert_volume("default", "vol-1", None, "us-east-1", 100, None)
    state.set_volume_autoresize("vol-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert b'<div class="n">0</div><div class="l">Autoresize enabled</div>' in response.data


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


# --- Settings tab (config-in-DB) ---------------------------------------------


def test_index_renders_settings_tab(state):
    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"Settings" in response.data
    assert b"dry_run" in response.data
    assert b"scheduler.interval_seconds" in response.data


def test_settings_tab_shows_human_readable_labels_and_tooltips(state):
    client = create_app(state).test_client()
    response = client.get("/")
    html = response.data.decode()

    assert "Scan interval (sec)" in html  # label, not the raw "scheduler.interval_seconds" key
    assert "How often a full discovery-and-check cycle runs." in html  # tooltip text


def test_settings_tab_renders_dry_run_last(state):
    client = create_app(state).test_client()
    html = client.get("/").data.decode()

    other_keys = [
        "default_threshold_percent",
        "scheduler.interval_seconds",
        "scheduler.max_concurrent_resizes",
        "scheduler.scan_log_retention_days",
        "resize.max_volume_size_gb",
        "resize.growth_increment_percent",
        "resize.extend_retry.max_attempts",
        "resize.extend_retry.backoff_seconds",
        "resize.stale_lock_timeout_minutes",
    ]
    dry_run_position = html.index('data-key="dry_run"')
    for key in other_keys:
        assert html.index(f'data-key="{key}"') < dry_run_position


def test_settings_tab_has_single_update_button_not_per_row_save(state):
    client = create_app(state).test_client()
    html = client.get("/").data.decode()

    assert 'id="settings-update-btn"' in html
    assert ">Update settings<" in html
    assert ">Save<" not in html  # the old per-row button text is gone


def test_settings_tab_includes_dirty_tracking_and_tooltip_flip_script(state):
    client = create_app(state).test_client()
    html = client.get("/").data.decode()

    assert "function wireConfigDirtyTracking" in html
    assert "function wireTooltipFlip" in html
    assert "function wireSettingsUpdateButton" in html
    assert "tooltip-flip" in html


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


def test_index_has_stable_ids_for_live_refresh_swap(state):
    # refreshLiveData() in the page's own JS re-fetches "/" and swaps these
    # exact ids' innerHTML - if any of them go missing (e.g. someone edits
    # the template and drops an id), live refresh silently stops updating
    # that section instead of erroring, so it's worth pinning here.
    client = create_app(state).test_client()
    html = client.get("/").data.decode()

    for expected_id in [
        'id="dashboard-stats"',
        'id="instances-tbody"',
        'id="volumes-tbody"',
        'id="settings-tbody"',
        'id="resize-tbody"',
        'id="scan-tbody"',
    ]:
        assert expected_id in html


def test_index_includes_live_refresh_script(state):
    client = create_app(state).test_client()
    html = client.get("/").data.decode()

    assert "function refreshLiveData" in html
    assert "setInterval(refreshLiveData" in html
    assert "visibilitychange" in html
    assert "document.hidden" in html


# --- Effective eligibility (Volumes tab Watched/Threshold columns) -----------


def test_volume_watched_via_own_flag(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_volume_autoresize("vol-1", True)

    html = create_app(state).test_client().get("/").data.decode()
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

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" in row
    assert 'data-autoresize="false"' in row  # the toggle itself still reflects the volume's own (unset) flag


def test_volume_not_watched_when_neither_flag_set(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert ">No<" in row


def test_unattached_volume_shows_unattached_even_if_flagged(state):
    state.upsert_volume("default", "vol-1", None, "us-east-1", 100, None)
    state.set_volume_autoresize("vol-1", True)  # flagged, but nothing to extend the filesystem on

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Unattached" in row


def test_threshold_shows_own_value_without_annotation(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state._volumes["vol-1"] = replace(state._volumes["vol-1"], threshold_percent=42)

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "42%" in row
    assert "eligibility-note" not in row


def test_threshold_falls_back_to_instance_with_annotation(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state._instances["i-1"] = replace(state._instances["i-1"], threshold_percent=55)

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "55%" in row
    assert '<span class="eligibility-note">(instance)</span>' in row


def test_threshold_falls_back_to_global_default_with_annotation(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "80%" in row  # dataclass default
    assert '<span class="eligibility-note">(default)</span>' in row


def test_threshold_default_reflects_app_config_override(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_config("default_threshold_percent", "65")

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "65%" in row


def test_threshold_default_falls_back_on_invalid_app_config_override(state):
    state.upsert_instance("default", "i-1", "us-east-1", "t3.micro", "running")
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.set_config("default_threshold_percent", "not-a-number")

    html = create_app(state).test_client().get("/").data.decode()
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

    html = create_app(state).test_client().get("/").data.decode()
    row_start = html.index('data-volume="vol-1"')
    row_end = html.index("</tr>", row_start)
    row = html[row_start:row_end]

    assert "Yes" in row
    assert "via instance" in row
