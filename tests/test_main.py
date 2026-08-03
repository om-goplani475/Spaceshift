"""Tests for controller.main.

main() loops forever via scheduler.run_forever(), so nothing else in the
suite exercises it - a stale config field reference in its startup logging
(config.connection.mode, removed when SSH support was dropped) only
surfaced by actually running the container. This is a regression guard for
that class of bug.
"""

from unittest.mock import MagicMock, patch

import pytest

from controller.config import Config
from controller.main import build_state_backend, main


def test_main_startup_does_not_crash_before_run_forever():
    # A real Config instance, not a MagicMock - a MagicMock would silently
    # allow access to any attribute, including ones that no longer exist,
    # which is exactly how the config.connection.mode bug went unnoticed.
    with (
        patch("controller.main.load_config", return_value=Config.from_dict({})),
        patch("controller.main.build_state_backend") as build_state_backend,
        patch("controller.main.Notifier"),
        patch("controller.main.Scheduler") as scheduler_cls,
    ):
        state = MagicMock()
        build_state_backend.return_value = state
        scheduler_cls.return_value.run_forever.side_effect = RuntimeError("stop after startup")

        try:
            main()
        except RuntimeError:
            pass

        state.close.assert_called_once()


def test_build_state_backend_requires_password(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        build_state_backend()


def test_build_state_backend_defaults_to_compose_service_name(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "changeme")
    with patch("controller.main.PostgresStateBackend") as backend_cls:
        build_state_backend()
    backend_cls.assert_called_once_with("postgresql://ebs_auto_resize:changeme@postgres:5432/ebs_auto_resize")


def test_build_state_backend_reads_host_and_password_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PASSWORD", "supersecret")
    with patch("controller.main.PostgresStateBackend") as backend_cls:
        build_state_backend()
    backend_cls.assert_called_once_with("postgresql://ebs_auto_resize:supersecret@localhost:5432/ebs_auto_resize")
