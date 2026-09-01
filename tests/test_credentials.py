"""Tests for the persisted GitHub token used by the Copilot CLI backend."""

from __future__ import annotations

import os
import stat

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from atpg_coverage_debug_agent.config import credentials


@pytest.fixture
def cred_path(tmp_path, monkeypatch):
    path = tmp_path / "creds" / "credentials.json"
    # Ignore any real keyring on the host so the file fallback is exercised.
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    return path


def test_save_then_load_round_trip(cred_path):
    assert credentials.save_github_token("gho_secret", cred_path) is True
    assert credentials.load_github_token(cred_path) == "gho_secret"


def test_saved_file_is_owner_only(cred_path):
    credentials.save_github_token("gho_secret", cred_path)
    assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cred_path.parent.stat().st_mode) == 0o700


def test_world_readable_file_is_ignored(cred_path):
    credentials.save_github_token("gho_secret", cred_path)
    os.chmod(cred_path, 0o644)
    assert credentials.load_github_token(cred_path) == ""


def test_missing_file_returns_empty(cred_path):
    assert credentials.load_github_token(cred_path) == ""


def test_empty_token_clears_the_store(cred_path):
    credentials.save_github_token("gho_secret", cred_path)
    credentials.save_github_token("   ", cred_path)
    assert not cred_path.exists()
    assert credentials.load_github_token(cred_path) == ""


def test_clear_is_idempotent(cred_path):
    credentials.clear_github_token(cred_path)
    credentials.clear_github_token(cred_path)
    assert credentials.load_github_token(cred_path) == ""


def test_corrupt_file_returns_empty(cred_path):
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text("not json")
    os.chmod(cred_path, 0o600)
    assert credentials.load_github_token(cred_path) == ""


# -- per-user isolation ------------------------------------------------------

def test_store_lives_under_the_process_owner_home():
    pwd = pytest.importorskip("pwd")
    home = pwd.getpwuid(os.getuid()).pw_dir
    assert str(credentials._DEFAULT_CREDENTIALS_FILE).startswith(home)


def test_home_dir_ignores_a_hijacked_home_env(monkeypatch, tmp_path):
    pwd = pytest.importorskip("pwd")
    monkeypatch.setenv("HOME", str(tmp_path / "someone_else"))
    assert str(credentials._home_dir()) == pwd.getpwuid(os.getuid()).pw_dir


def test_file_owned_by_another_user_is_ignored(cred_path, monkeypatch):
    credentials.save_github_token("gho_secret", cred_path)
    # Pretend we are now a different account on the same host.
    monkeypatch.setattr(os, "getuid", lambda: os.stat(cred_path).st_uid + 1)
    assert credentials.load_github_token(cred_path) == ""


def test_save_refuses_a_directory_owned_by_another_user(cred_path, monkeypatch):
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "getuid",
                        lambda: os.stat(cred_path.parent).st_uid + 1)
    assert credentials.save_github_token("gho_secret", cred_path) is False
    assert not cred_path.exists()


def test_clear_refuses_a_file_owned_by_another_user(cred_path, monkeypatch):
    credentials.save_github_token("gho_secret", cred_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(cred_path).st_uid + 1)
    credentials.clear_github_token(cred_path)
    assert cred_path.exists()


# -- GUI wiring --------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the credential store at a temp file for the whole panel."""
    path = tmp_path / "creds" / "credentials.json"
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.setattr(credentials, "_DEFAULT_CREDENTIALS_FILE", path)
    return path


def test_panel_autofills_saved_token_on_construction(qapp, isolated_store):
    credentials.save_github_token("gho_saved", isolated_store)
    panel = AgentPanel()
    assert panel.auth_token_edit.text() == "gho_saved"
    assert panel.auth_remember_check.isChecked()
    # It also reaches the agent config that runs the CLI.
    assert panel.current_config().cli_token == "gho_saved"


def test_panel_persists_token_when_remember_is_on(qapp, isolated_store):
    panel = AgentPanel()
    panel.auth_token_edit.setText("gho_new")
    panel._persist_token()
    assert credentials.load_github_token(isolated_store) == "gho_new"


def test_panel_does_not_persist_when_remember_is_off(qapp, isolated_store):
    panel = AgentPanel()
    panel.auth_remember_check.setChecked(False)
    panel.auth_token_edit.setText("gho_new")
    panel._persist_token()
    assert credentials.load_github_token(isolated_store) == ""


def test_forget_token_clears_field_and_store(qapp, isolated_store):
    credentials.save_github_token("gho_saved", isolated_store)
    panel = AgentPanel()
    panel.on_forget_token()
    assert panel.auth_token_edit.text() == ""
    assert not panel.auth_remember_check.isChecked()
    assert credentials.load_github_token(isolated_store) == ""


def test_token_is_never_written_to_settings(qapp, isolated_store):
    panel = AgentPanel()
    panel.auth_token_edit.setText("gho_secret")
    cfg = panel.export_settings()
    assert "gho_secret" not in str(cfg)
    assert cfg["remember_token"] is True
