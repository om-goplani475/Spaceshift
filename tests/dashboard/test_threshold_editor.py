"""The per-volume / per-instance threshold editor: the API, who may use
it, what it audits, and what the Volumes and Instances pages render."""

from __future__ import annotations

import pytest

from controller.audit import AuditAction, AuditFilters
from controller.dashboard.auth import hash_password
from tests.dashboard.conftest import sign_in

PASSWORD = "a perfectly fine passphrase"


@pytest.fixture
def fleet(state):
    state.create_account("prod", None, ["us-east-1"])
    state.create_account("dev", None, ["us-east-1"])
    for account in ("prod", "dev"):
        state.upsert_instance(account, f"i-{account}", "us-east-1", "t3.micro", "running")
        state.upsert_volume(account, f"vol-{account}", f"i-{account}", "us-east-1", 100, "/dev/xvdf")


def _client_as(app, state, username: str, grants: dict[str, str]):
    user = state.create_user(username, hash_password(PASSWORD))
    for account, role in grants.items():
        state.set_role(user.id, user.username, account, role, None)
    client = app.test_client()
    assert sign_in(client, username=username, password=PASSWORD).status_code == 302
    return client


def _audit(state, action: AuditAction):
    return state.list_audit(AuditFilters(action=action), limit=20)


# ---- API --------------------------------------------------------------------


def test_sets_and_clears_a_volume_threshold(client, state, fleet):
    assert client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 90}).status_code == 200
    assert state.get_volume("vol-dev").threshold_percent == 90

    response = client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": None})

    assert response.status_code == 200 and response.get_json()["threshold_percent"] is None
    assert state.get_volume("vol-dev").threshold_percent is None


def test_sets_and_clears_an_instance_threshold(client, state, fleet):
    assert client.post("/api/instances/i-dev/threshold", json={"threshold_percent": 70}).status_code == 200
    assert state.get_instance("i-dev").threshold_percent == 70

    assert client.post("/api/instances/i-dev/threshold", json={"threshold_percent": None}).status_code == 200
    assert state.get_instance("i-dev").threshold_percent is None


@pytest.mark.parametrize("body", [{"threshold_percent": v} for v in (0, 101, -5, "80", True, 79.5, [80])])
def test_rejects_values_that_are_not_a_whole_percent_from_1_to_100(client, state, fleet, body):
    assert client.post("/api/volumes/vol-dev/threshold", json=body).status_code == 400
    assert state.get_volume("vol-dev").threshold_percent is None
    assert _audit(state, AuditAction.VOLUME_THRESHOLD) == []


@pytest.mark.parametrize("body", [{}, {"threshold": 80}, [], "80"])
def test_rejects_a_body_without_threshold_percent(client, state, fleet, body):
    assert client.post("/api/volumes/vol-dev/threshold", json=body).status_code == 400


def test_accepts_the_boundaries(client, state, fleet):
    assert client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 1}).status_code == 200
    assert client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 100}).status_code == 200


def test_unknown_ids_are_404(client, state, fleet):
    assert client.post("/api/volumes/vol-nope/threshold", json={"threshold_percent": 80}).status_code == 404
    assert client.post("/api/instances/i-nope/threshold", json={"threshold_percent": 80}).status_code == 404


def test_each_change_writes_an_audit_row_with_old_and_new_value(client, state, fleet):
    client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 90})
    client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": None})
    client.post("/api/instances/i-dev/threshold", json={"threshold_percent": 70})

    volume_rows = _audit(state, AuditAction.VOLUME_THRESHOLD)
    assert [(r.before, r.after) for r in reversed(volume_rows)] == [
        ({"threshold_percent": None}, {"threshold_percent": 90}),
        ({"threshold_percent": 90}, {"threshold_percent": None}),
    ]
    assert volume_rows[0].target_id == "vol-dev" and volume_rows[0].account_name == "dev"
    assert _audit(state, AuditAction.INSTANCE_THRESHOLD)[0].after == {"threshold_percent": 70}


# ---- Who may ----------------------------------------------------------------


def test_a_viewer_cannot_change_a_threshold(app, state, fleet):
    client = _client_as(app, state, "vic", {"dev": "viewer"})

    assert client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 90}).status_code == 403
    assert client.post("/api/instances/i-dev/threshold", json={"threshold_percent": 90}).status_code == 403
    assert state.get_volume("vol-dev").threshold_percent is None
    assert len(_audit(state, AuditAction.ACCESS_DENIED)) == 2


def test_an_operator_can_change_it_only_in_their_account(app, state, fleet):
    client = _client_as(app, state, "olive", {"dev": "operator"})

    assert client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 90}).status_code == 200
    # An account they can't read looks like an id that doesn't exist.
    assert client.post("/api/volumes/vol-prod/threshold", json={"threshold_percent": 90}).status_code == 404
    assert state.get_volume("vol-prod").threshold_percent is None


# ---- Pages ------------------------------------------------------------------


def test_volumes_page_shows_an_editor_with_the_inherited_value_as_placeholder(client, state, fleet):
    client.post("/api/instances/i-dev/threshold", json={"threshold_percent": 70})

    html = client.get("/volumes").data.decode()

    assert 'class="limit-input"' in html
    assert 'data-id="vol-dev"' in html and 'data-inherited="70"' in html and 'data-inherited-source="instance"' in html
    assert 'placeholder="70"' in html


def test_the_pages_have_one_update_bar_and_no_per_field_save_buttons(client, state, fleet):
    for path in ("/volumes", "/instances"):
        html = client.get(path).data.decode()

        assert 'id="limits-update-btn"' in html and 'id="limits-discard-btn"' in html
        assert html.count('id="limits-bar"') == 1
        assert "limit-save" not in html and "limit-cancel" not in html


def test_a_viewer_gets_no_inputs_and_no_update_bar(app, state, fleet):
    state.set_volume_threshold("vol-dev", 91)
    client = _client_as(app, state, "vic", {"dev": "viewer"})

    for path in ("/volumes", "/instances"):
        html = client.get(path).data.decode()
        assert 'id="limits-bar"' not in html and "limit-input" not in html


def test_an_operator_of_only_one_account_still_gets_the_bar(app, state, fleet):
    client = _client_as(app, state, "olive", {"dev": "operator"})

    assert 'id="limits-update-btn"' in client.get("/volumes").data.decode()


def test_volumes_page_shows_the_volumes_own_value(client, state, fleet):
    client.post("/api/volumes/vol-dev/threshold", json={"threshold_percent": 91})

    html = client.get("/volumes").data.decode()

    assert 'value="91"' in html
    assert '<span class="limit-text" hidden>91%</span>' in html


def test_instances_page_shows_an_editor_that_inherits_the_default(client, state, fleet):
    html = client.get("/instances").data.decode()

    assert 'data-id="i-dev"' in html and 'data-inherited="80"' in html and 'data-inherited-source="default"' in html


def test_a_viewer_sees_the_threshold_as_text_without_an_editor(app, state, fleet):
    state.set_volume_threshold("vol-dev", 91)
    client = _client_as(app, state, "vic", {"dev": "viewer"})

    for path in ("/volumes", "/instances"):
        assert "limit-input" not in client.get(path).data.decode()
    assert "91%" in client.get("/volumes").data.decode()
