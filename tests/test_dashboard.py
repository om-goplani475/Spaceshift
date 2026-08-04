"""Tests for controller.dashboard."""

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


def test_stats_count_autoresize_enabled_volumes(state):
    state.upsert_volume("default", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    state.upsert_volume("default", "vol-2", "i-1", "us-east-1", 100, "/dev/xvdg")
    state.set_volume_autoresize("vol-1", True)

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b'<div class="n">1</div><div class="l">Autoresize enabled</div>' in response.data


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


def test_api_config_lists_all_known_keys_with_defaults(state):
    client = create_app(state).test_client()
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.get_json()
    by_key = {row["key"]: row for row in data}
    assert by_key["dry_run"] == {"key": "dry_run", "value": None, "default": "true"}
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
