"""Tests for the /audit page and /api/audit (JSON and CSV export)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from controller.audit import AuditAction, AuditActor, AuditEvent
from tests.dashboard.conftest import ADMIN_USERNAME


def _seed(state, n: int = 3):
    state.upsert_volume("prod", "vol-1", "i-1", "us-east-1", 100, "/dev/xvdf")
    actor = AuditActor.user(1, ADMIN_USERNAME, "10.0.0.1")
    for i in range(n):
        state.set_volume_autoresize("vol-1", i % 2 == 0, audit=actor)
    state.record_audit(
        AuditEvent(AuditActor.anonymous("10.0.0.9", "mallory"), AuditAction.AUTH_LOGIN_FAILED, "user", "mallory")
    )
    state.set_config("dry_run", "false", audit=actor)


def test_audit_page_renders_rows_with_change_and_actor(client, state):
    _seed(state)
    html = client.get("/audit").data.decode()
    assert "off → on" in html and "— → false" in html
    assert 'title="auth.login_failed"' in html and "Sign-in failed" in html
    # the typed name is the target of a failed login, never shown as the actor
    assert '<span class="muted-note">Anonymous</span>' in html
    assert '<span class="mono">mallory</span>' not in html
    assert 'id="audit-tbody"' in html  # live-refresh swap target


def test_audit_page_filters_narrow_and_export_link_carries_them(client, state):
    _seed(state)
    html = client.get("/audit?actor=alice&action=volume.autoresize").data.decode()
    assert "mallory" not in html and html.count("Autoresize toggled (volume)</span>") == 3
    assert 'href="/api/audit?format=csv&amp;actor=alice&amp;action=volume.autoresize"' in html
    assert ">Clear<" in html

    assert "Nothing matches" in client.get("/audit?target=nothing-here").data.decode()
    assert client.get("/audit?since=not-a-date").status_code == 400
    assert client.get("/audit?action=made.up").status_code == 400
    assert client.get("/audit?before=abc").status_code == 400


def test_audit_page_keyset_paging(client, state, monkeypatch):
    from controller.dashboard import audit_pages

    monkeypatch.setattr(audit_pages, "_AUDIT_PAGE_SIZE", 2)
    _seed(state, n=4)  # 6 rows total
    page1 = client.get("/audit").data.decode()
    assert ">Older<" in page1 and ">Newest<" not in page1
    older = page1.split('href="')[1:]
    older_url = next(u.split('"')[0] for u in older if "before=" in u)
    page2 = client.get(older_url.replace("&amp;", "&")).data.decode()
    assert ">Newest<" in page2 and ">Older<" in page2


def test_date_filters(client, state):
    _seed(state)
    today = datetime.now(UTC).date().isoformat()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert "vol-1" in client.get(f"/audit?since={today}&until={today}").data.decode()  # until is through that day
    assert "No activity" not in client.get(f"/audit?since={today}").data.decode()
    assert "Nothing matches" in client.get(f"/audit?since={tomorrow}").data.decode()


def test_api_audit_json_paging(client, state):
    _seed(state, n=4)  # 4 toggles + 1 failed login + 1 config, plus the fixture's own sign-in = 7
    first = client.get("/api/audit?limit=4").get_json()
    assert len(first["rows"]) == 4 and first["next_before"] is not None
    second = client.get(f"/api/audit?limit=4&before={first['next_before']}").get_json()
    assert len(second["rows"]) == 3 and second["next_before"] is None
    ids = [r["id"] for r in first["rows"] + second["rows"]]
    assert ids == sorted(ids, reverse=True)
    assert first["rows"][0]["actor_username"] == ADMIN_USERNAME
    assert client.get("/api/audit?limit=0").status_code == 400
    assert client.get("/api/audit?limit=1001").status_code == 400


def test_csv_export_streams_every_matching_row_with_rfc4180_quoting(client, state):
    _seed(state)
    state.record_audit(
        AuditEvent(
            AuditActor.cli(),
            AuditAction.USER_CREATE,
            "user",
            'weird,"name"\nwith newline',
            detail={"command": "create-admin"},
        )
    )
    response = client.get("/api/audit?format=csv&actor=alice")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert response.headers["Content-Disposition"].startswith('attachment; filename="spaceshift-audit-')
    rows = list(csv.reader(io.StringIO(response.data.decode())))
    assert rows[0][:3] == ["id", "created_at", "actor_type"]
    # header + alice's rows (3 toggles, 1 config, her sign-in); the filter applies to the export too
    assert len(rows) == 1 + 5

    everything = list(csv.reader(io.StringIO(client.get("/api/audit?format=csv").data.decode())))
    weird = next(r for r in everything if r[6].startswith("weird"))
    assert weird[6] == 'weird,"name"\nwith newline'  # survived quoting
    assert weird[11] == '{"command":"create-admin"}'


def test_audit_routes_require_login(app):
    anonymous = app.test_client()
    assert anonymous.get("/audit").status_code == 302
    assert anonymous.get("/api/audit?format=csv").status_code == 401
