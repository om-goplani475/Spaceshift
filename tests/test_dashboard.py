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
