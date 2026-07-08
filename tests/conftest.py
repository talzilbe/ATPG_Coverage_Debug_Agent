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


@pytest.fixture
def sample_netlist_path() -> str:
    return os.path.join(SAMPLE_DIR, "sample_netlist.v")


@pytest.fixture
def sample_faults_path() -> str:
    return os.path.join(SAMPLE_DIR, "sample_faults.txt")


@pytest.fixture
def sample_constraints_path() -> str:
    return os.path.join(SAMPLE_DIR, "sample_constraints.txt")
