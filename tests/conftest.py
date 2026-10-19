"""Shared pytest fixtures and path helpers for the test suite."""

from __future__ import annotations

import os
import sys

import pytest

# Ensure the package on x:\ root is importable when tests run from anywhere.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SAMPLE_DIR = os.path.join(_ROOT, "sample_data")


@pytest.fixture(autouse=True)
def _isolated_user_profile_dir(tmp_path, monkeypatch):
    """Keep a developer's imported launch profiles out of every test."""
    monkeypatch.setenv("ATPG_USER_PROFILE_DIR", str(tmp_path / "user_profiles"))
    # A developer's real fix ledger would re-rank every plan under test.
    monkeypatch.setenv("ATPG_FIX_HISTORY", str(tmp_path / "fix_history.json"))
    # Running pytest inside a project setup must not change launch scripts.
    for name in ("CTH_SESSION_ID", "CTH_ENV_INDICATOR", "CTH_SETUP_CMD", "WORKAREA"):
        monkeypatch.delenv(name, raising=False)
    # Setting a netlist would otherwise search real disks around it.
    monkeypatch.setenv("ATPG_INPUT_DISCOVERY", "0")
    # The shared Cth.ai catalogue would add the free models to every panel.
    monkeypatch.setenv("ATPG_CTHAI_CATALOG", str(tmp_path / "no_cthai.json"))


@pytest.fixture
def sample_netlist_path() -> str:
    return os.path.join(SAMPLE_DIR, "sample_netlist.v")


@pytest.fixture
def sample_faults_path() -> str:
    return os.path.join(SAMPLE_DIR, "sample_faults.txt")


@pytest.fixture
def sample_constraints_path() -> str:
    return os.path.join(SAMPLE_DIR, "sample_constraints.txt")
