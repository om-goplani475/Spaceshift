"""Tests for controller.dashboard."""

from controller.dashboard import create_app
from controller.state.base import ResizeStatus


def test_index_with_no_records(sqlite_state):
    client = create_app(sqlite_state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"No resize activity yet" in response.data


def test_index_renders_records(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 120)
    sqlite_state.update_status("vol-1", ResizeStatus.EXTEND_COMPLETED)

    client = create_app(sqlite_state).test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"vol-1" in response.data


def test_api_status_returns_json(sqlite_state):
    sqlite_state.create_if_absent("vol-1", "i-1", 100, 120)

    client = create_app(sqlite_state).test_client()
    response = client.get("/api/status")

    assert response.status_code == 200
    data = response.get_json()
    assert data[0]["volume_id"] == "vol-1"
