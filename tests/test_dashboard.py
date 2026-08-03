"""Tests for controller.dashboard."""

from controller.dashboard import create_app
from controller.state.base import ResizeStatus


def test_index_with_no_records(state):
    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"No resize activity yet" in response.data
    assert b"No scans recorded yet" in response.data


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
    state.record_scan("us-east-1", "vol-9", "i-9", 100, 80, 42.0, "below_threshold")

    client = create_app(state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"vol-9" in response.data
    assert b"below_threshold" in response.data


def test_api_scans_returns_json_newest_first(state):
    state.record_scan("us-east-1", "vol-0", "i-1", 100, 80, 10, "below_threshold")
    state.record_scan("us-east-1", "vol-1", "i-1", 100, 80, 10, "below_threshold")
    state.record_scan("us-east-1", "vol-2", "i-1", 100, 80, 10, "below_threshold")

    client = create_app(state).test_client()
    response = client.get("/api/scans")

    assert response.status_code == 200
    data = response.get_json()
    assert [e["volume_id"] for e in data] == ["vol-2", "vol-1", "vol-0"]
