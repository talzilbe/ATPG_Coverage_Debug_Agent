"""Tests for the Tessent fault-list parser."""

from __future__ import annotations

from atpg_coverage_debug_agent.models import FaultClass
from atpg_coverage_debug_agent.parser.fault_parser import (
    normalize_object,
    parse_fault_list,
)


def test_parses_class_value_object():
    records, warnings = parse_fault_list("AU 1 top/u_alu/U5/Y")
    assert len(records) == 1
    r = records[0]
    assert r.fault_class is FaultClass.AU
    assert r.fault_object == "top/u_alu/U5/Y"
    assert r.normalized_object == "top/u_alu/U5/Y"
    assert r.fault_type == "1"


def test_object_first_then_class():
    records, _ = parse_fault_list("top/u_ctrl/U4/Y UO")
    assert records[0].fault_class is FaultClass.UO
    assert records[0].fault_object == "top/u_ctrl/U4/Y"


def test_comments_and_blank_lines_skipped():
    text = "# comment\n\n// another\nUC 0 a/b/c\n"
    records, _ = parse_fault_list(text)
    assert len(records) == 1
    assert records[0].fault_class is FaultClass.UC


def test_unknown_class_warns_but_keeps_record():
    records, warnings = parse_fault_list("ZZ 0 some/path")
    assert records[0].fault_class is FaultClass.UNKNOWN
    assert any("unrecognised fault class" in w for w in warnings)


def test_normalize_dot_and_leading_slash():
    assert normalize_object("/top.u_alu.U1.Y") == "top/u_alu/U1/Y"


def test_coverage_loss_flag():
    records, _ = parse_fault_list("DS 0 a/b\nAU 0 c/d\n")
    flags = {r.fault_class.value: r.is_coverage_loss for r in records}
    assert flags["DS"] is False
    assert flags["AU"] is True
