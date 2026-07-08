"""Tests for the constraint parser."""

from __future__ import annotations

from atpg_coverage_debug_agent.parser.constraint_parser import parse_constraints


def test_force_constraint():
    records, _ = parse_constraints("force sel 0")
    assert records[0].kind == "force"
    assert records[0].signal == "sel"
    assert records[0].value == "0"


def test_tessent_add_input_constraints():
    records, _ = parse_constraints("add_input_constraints test_se C0")
    r = records[0]
    assert r.kind == "constrain"
    assert r.signal == "test_se"
    assert r.value == "0"


def test_clock_reset_keywords():
    records, _ = parse_constraints("clock clk\nreset rst_n\n")
    kinds = {r.kind for r in records}
    assert "clock" in kinds
    assert "reset" in kinds


def test_assignment_syntax():
    records, _ = parse_constraints("scan_en = 0")
    assert records[0].kind == "constant"
    assert records[0].value == "0"


def test_unknown_line_warns():
    records, warnings = parse_constraints("hello world foo bar")
    assert records[0].kind == "unknown"
    assert warnings
