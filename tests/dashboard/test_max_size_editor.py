"""The per-volume / per-instance max size editor: same rules as the
threshold editor (see test_threshold_editor.py), for max_size_gb."""

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


def _client_as(app, state, username: str, role: str):
    user = state.create_user(username, hash_password(PASSWORD))
    state.set_role(user.id, user.username, "dev", role, None)
    client = app.test_client()
    assert sign_in(client, username=username, password=PASSWORD).status_code == 302
    return client


def _audit(state, action: AuditAction):
    return state.list_audit(AuditFilters(action=action), limit=20)


def test_sets_and_clears_a_volume_max_size(client, state, fleet):
    assert client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 1000}).status_code == 200
    assert state.get_volume("vol-dev").max_size_gb == 1000

    response = client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": None})

    assert response.status_code == 200 and response.get_json()["max_size_gb"] is None
    assert state.get_volume("vol-dev").max_size_gb is None


def test_sets_and_clears_an_instance_max_size(client, state, fleet):
    assert client.post("/api/instances/i-dev/max-size", json={"max_size_gb": 750}).status_code == 200
    assert state.get_instance("i-dev").max_size_gb == 750

    assert client.post("/api/instances/i-dev/max-size", json={"max_size_gb": None}).status_code == 200
    assert state.get_instance("i-dev").max_size_gb is None


@pytest.mark.parametrize("value", [0, -1, 65537, "500", True, 99.5, [500]])
def test_rejects_values_that_are_not_a_whole_gb_from_1_to_65536(client, state, fleet, value):
    assert client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": value}).status_code == 400
    assert state.get_volume("vol-dev").max_size_gb is None
    assert _audit(state, AuditAction.VOLUME_MAX_SIZE) == []


def test_accepts_the_boundaries(client, state, fleet):
    assert client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 1}).status_code == 200
    assert client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 65536}).status_code == 200


def test_a_body_for_the_other_limit_is_rejected(client, state, fleet):
    assert client.post("/api/volumes/vol-dev/max-size", json={"threshold_percent": 80}).status_code == 400
    assert client.post("/api/volumes/vol-dev/threshold", json={"max_size_gb": 500}).status_code == 400


def test_unknown_ids_are_404(client, state, fleet):
    assert client.post("/api/volumes/vol-nope/max-size", json={"max_size_gb": 500}).status_code == 404
    assert client.post("/api/instances/i-nope/max-size", json={"max_size_gb": 500}).status_code == 404


def test_each_change_is_audited_with_old_and_new_value(client, state, fleet):
    client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 1000})
    client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": None})
    client.post("/api/instances/i-dev/max-size", json={"max_size_gb": 750})

    rows = _audit(state, AuditAction.VOLUME_MAX_SIZE)
    assert [(r.before, r.after) for r in reversed(rows)] == [
        ({"max_size_gb": None}, {"max_size_gb": 1000}),
        ({"max_size_gb": 1000}, {"max_size_gb": None}),
    ]
    assert rows[0].target_id == "vol-dev" and rows[0].account_name == "dev"
    assert _audit(state, AuditAction.INSTANCE_MAX_SIZE)[0].after == {"max_size_gb": 750}


def test_a_viewer_cannot_change_it(app, state, fleet):
    client = _client_as(app, state, "vic", "viewer")

    assert client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 1000}).status_code == 403
    assert client.post("/api/instances/i-dev/max-size", json={"max_size_gb": 1000}).status_code == 403
    assert state.get_volume("vol-dev").max_size_gb is None


def test_an_operator_can_change_it_only_in_their_account(app, state, fleet):
    client = _client_as(app, state, "olive", "operator")

    assert client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 1000}).status_code == 200
    assert client.post("/api/volumes/vol-prod/max-size", json={"max_size_gb": 1000}).status_code == 404


# ---- Pages ----------------------------------------------------------------


def test_volumes_page_inherits_the_instance_then_the_global_ceiling(client, state, fleet):
    html = client.get("/volumes").data.decode()
    assert (
        'data-field="max-size"' in html and 'data-inherited="500"' in html and 'data-inherited-source="default"' in html
    )

    client.post("/api/instances/i-dev/max-size", json={"max_size_gb": 750})
    html = client.get("/volumes").data.decode()
    assert 'data-inherited="750"' in html and 'data-inherited-source="instance"' in html


def test_volumes_page_shows_the_global_ceiling_override_as_the_default(client, state, fleet):
    state.set_config("resize.max_volume_size_gb", "900")

    assert 'data-inherited="900"' in client.get("/volumes").data.decode()


def test_volumes_page_shows_the_volumes_own_value(client, state, fleet):
    client.post("/api/volumes/vol-dev/max-size", json={"max_size_gb": 1000})

    html = client.get("/volumes").data.decode()

    assert 'value="1000"' in html
    assert '<span class="limit-text" hidden>1000 GB</span>' in html


def test_instances_page_has_a_max_size_editor(client, state, fleet):
    html = client.get("/instances").data.decode()

    assert 'data-field="max-size"' in html and 'data-id="i-dev"' in html and 'data-inherited="500"' in html


def test_a_viewer_sees_the_max_size_as_text_without_an_editor(app, state, fleet):
    state.set_volume_max_size("vol-dev", 1000)
    client = _client_as(app, state, "vic", "viewer")

    assert "limit-input" not in client.get("/volumes").data.decode()
    assert "1000 GB" in client.get("/volumes").data.decode()
