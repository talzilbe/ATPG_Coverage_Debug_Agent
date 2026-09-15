"""Regression tests for the offline analyzer's census, metrics and parsers.

Two layers, deliberately separated:

* **Structural tests** assert properties that must hold on *every* partition:
  the census reconciles, percentages are re-derivable, an unknown token is
  loud, a class name inside a path never becomes a class, format sniffing
  ignores the file name, a combinational neighbour is not a scan boundary.
  None of them mentions a design, a cell library or a fault count.
* **Golden-file tests** are fixture-driven. Each folder under
  ``tests/fixtures/partitions`` holds its own inputs plus an ``expected.json``.
  Adding a partition to the suite must be data only — if it needs a code
  change, the generalisation is incomplete, and
  :func:`test_adding_a_partition_is_data_only` guards exactly that.
"""

from __future__ import annotations

import bz2
import gzip
import json
import lzma
import os
from pathlib import Path

import pytest

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.config.analysis_config import (
    AnalysisConfig,
    CoverageRole,
    set_config,
)
from atpg_coverage_debug_agent.diagnostics import (
    CensusMismatch,
    ThresholdExceeded,
)
from atpg_coverage_debug_agent.models import RootCause
from atpg_coverage_debug_agent.analysis.statistics import compute_statistics
from atpg_coverage_debug_agent.parser.constraint_parser import (
    parse_constraints_ex,
)
from atpg_coverage_debug_agent.parser.fault_parser import (
    detect_compression,
    parse_fault_list,
    parse_fault_list_ex,
    parse_fault_list_file_ex,
)
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "partitions"


def _partition_dirs():
    if not FIXTURE_ROOT.is_dir():
        return []
    return sorted(p for p in FIXTURE_ROOT.iterdir()
                  if (p / "expected.json").is_file())


@pytest.fixture(autouse=True)
def _default_config():
    """Every test starts from the documented defaults, not a leaked override."""
    set_config(AnalysisConfig())
    yield
    set_config(None)


def _mtfi(rows, fmt="Identifier, Class, Location", collapsing="FALSE"):
    body = "\n".join(f"      {r};" for r in rows)
    return (
        "FaultInformation {\n version : 1;\n FaultType (Stuck) {\n"
        "  FaultList {\n"
        f"   FaultCollapsing : {collapsing};\n"
        f"   Format : {fmt};\n"
        '   Instance ("") {\n'
        f"{body}\n   }}\n  }}\n }}\n}}\n"
    )


# ---------------------------------------------------------------------------
# Structural: the class map is complete and data-driven
# ---------------------------------------------------------------------------
def test_every_standard_class_is_recognised_so_unknown_stays_zero():
    """The five classes that were silently bucketed must now be known.

    ``UU``, ``BL``, ``RE``, ``PT`` and ``PU`` are legitimate Tessent classes.
    An incomplete map put 133,402 faults of one real partition into a silent
    catch-all and moved the reported coverage by roughly two points.
    """
    rows = [f'0,  {cls},  "/top/u{i}/y"'
            for i, cls in enumerate(("DS", "DI", "PT", "PU", "UU", "TI",
                                     "BL", "RE", "AU", "UO", "UC"))]
    result = parse_fault_list_ex(_mtfi(rows))
    assert result.unrecognised.count == 0
    stats = compute_statistics(result.records)
    assert stats.unrecognised_count == 0


def test_ti_is_undetectable_not_the_whole_undetectable_population():
    """``TI`` belongs with ``UU``/``BL``/``RE`` in the ``UD`` role."""
    config = AnalysisConfig()
    for token in ("TI", "UU", "BL", "RE"):
        assert config.role_of(token) is CoverageRole.UD
    assert config.role_of("DS") is CoverageRole.DT
    assert config.role_of("PT") is CoverageRole.PD
    assert config.role_of("AU") is CoverageRole.AU
    assert config.role_of("UO") is CoverageRole.ND


def test_unseen_subclass_resolves_to_its_family_not_to_unknown():
    """``AU.XYZ`` is an AU fault even though the subtype is new."""
    config = AnalysisConfig()
    assert config.role_of("AU.XYZ") is CoverageRole.AU
    assert config.role_of("UO.BRAND_NEW") is CoverageRole.ND
    assert config.is_loss_class("UC.NEVER_SEEN") is True


def test_class_map_is_overridable_from_configuration():
    """A class the tool has never heard of is onboarded without code."""
    config = AnalysisConfig.from_dict({"class_roles": {"NC": "UD"}})
    assert config.role_of("NC") is CoverageRole.UD
    assert config.role_of("DS") is CoverageRole.DT  # defaults still present

    set_config(config)
    rows = ['0,  DS,  "/top/u0/y"', '1,  NC,  "/top/u1/y"']
    result = parse_fault_list_ex(_mtfi(rows), config=config)
    assert result.unrecognised.count == 0
    stats = compute_statistics(result.records, config=config)
    assert stats.role(CoverageRole.UD) == 1


def test_a_partition_missing_whole_classes_reports_zeros_not_a_crash():
    """A partition may legally contain any subset of the classes."""
    rows = ['0,  DS,  "/top/u0/y"', '1,  UO,  "/top/u1/y"']
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    roles = stats.metrics()["roles"]
    assert roles == {"DT": 1, "PD": 0, "UD": 0, "AU": 0, "ND": 1}
    assert set(roles) == {"DT", "PD", "UD", "AU", "ND"}, (
        "absent roles must still be reported as zero rows")


# ---------------------------------------------------------------------------
# Structural: unrecognised tokens are loud
# ---------------------------------------------------------------------------
def test_unknown_class_warns_by_name_and_keeps_samples():
    rows = ['0,  DS,  "/top/u0/y"', '1,  ZQ,  "/top/u1/y"',
            '0,  ZQ,  "/top/u2/y"']
    result = parse_fault_list_ex(_mtfi(rows))
    report = result.unrecognised
    assert [t.token for t in report.tokens] == ["ZQ"]
    assert report.tokens[0].count == 2
    assert report.tokens[0].samples, "verbatim samples must be retained"
    assert "/top/u1/y" in report.tokens[0].samples[0]
    assert any("'ZQ'" in w for w in result.warnings)


def test_unknown_class_records_are_never_merged_into_one_bucket():
    """Distinct unknown tokens stay distinct, with a count each."""
    rows = ['0,  ZQ,  "/top/u0/y"', '1,  QX,  "/top/u1/y"',
            '0,  QX,  "/top/u2/y"']
    report = parse_fault_list_ex(_mtfi(rows)).unrecognised
    assert {t.token: t.count for t in report.tokens} == {"ZQ": 1, "QX": 2}


def test_unknown_class_trips_the_configured_threshold():
    rows = ['0,  DS,  "/top/u0/y"', '1,  ZQ,  "/top/u1/y"']
    with pytest.raises(ThresholdExceeded) as excinfo:
        parse_fault_list_ex(_mtfi(rows), enforce=True)
    assert "ZQ" in str(excinfo.value)


def test_threshold_is_configurable_and_can_be_made_non_fatal():
    config = AnalysisConfig.from_dict({
        "unknown_class_threshold_pct": 90.0,
        "unknown_class_fatal": False,
    })
    rows = ['0,  DS,  "/top/u0/y"', '1,  ZQ,  "/top/u1/y"']
    result = parse_fault_list_ex(_mtfi(rows), config=config, enforce=True)
    assert result.unrecognised.count == 1  # reported, not raised


def test_sample_retention_limit_is_configurable():
    config = AnalysisConfig.from_dict({"sample_limit": 2})
    rows = [f'0,  ZQ,  "/top/u{i}/y"' for i in range(10)]
    report = parse_fault_list_ex(_mtfi(rows), config=config).unrecognised
    assert len(report.tokens[0].samples) == 2
    assert report.tokens[0].count == 10


def test_unrecognised_class_is_excluded_from_every_metric():
    rows = ['0,  DS,  "/top/u0/y"', '1,  ZQ,  "/top/u1/y"']
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    assert stats.unrecognised_count == 1
    # 1 DT out of 2 records; the unknown record is in neither numerator nor
    # any role bucket that a metric reads.
    assert stats.metrics()["roles"] == {"DT": 1, "PD": 0, "UD": 0,
                                        "AU": 0, "ND": 0}


# ---------------------------------------------------------------------------
# Structural: census checksum
# ---------------------------------------------------------------------------
def test_census_reconciles_with_the_parsed_record_count():
    rows = [f'0,  {cls},  "/top/u{i}/y"'
            for i, cls in enumerate(("DS", "DI", "PT", "UU", "TI",
                                     "AU", "UO", "UC"))]
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    assert stats.census_balances
    roles = stats.metrics()["roles"]
    assert sum(roles.values()) == stats.total_faults


def test_strict_five_way_checksum_holds_when_nothing_is_unrecognised():
    """``DT + PD + UD + AU + ND == total`` exactly, as specified."""
    rows = [f'0,  {cls},  "/top/u{i}/y"'
            for i, cls in enumerate(("DS", "DI", "PT", "PU", "UU", "TI",
                                     "BL", "RE", "AU", "UO", "UC"))]
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    assert stats.unrecognised_count == 0
    roles = stats.metrics()["roles"]
    assert (roles["DT"] + roles["PD"] + roles["UD"] + roles["AU"]
            + roles["ND"]) == stats.total_faults


def test_a_short_census_raises_rather_than_being_quietly_balanced():
    rows = ['0,  DS,  "/top/u0/y"']
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    stats.role_counts["DT"] = 0  # simulate a bucket losing records
    assert not stats.census_balances
    with pytest.raises(CensusMismatch):
        stats.validate_census()


# ---------------------------------------------------------------------------
# Structural: metrics
# ---------------------------------------------------------------------------
def test_every_percentage_is_rederivable_from_its_counts():
    rows = ([f'0,  DS,  "/top/d{i}/y"' for i in range(10)]
            + [f'0,  PT,  "/top/p{i}/y"' for i in range(4)]
            + [f'0,  UU,  "/top/u{i}/y"' for i in range(6)]
            + [f'0,  AU,  "/top/a{i}/y"' for i in range(5)]
            + [f'0,  UO,  "/top/o{i}/y"' for i in range(5)])
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    m = stats.metrics()
    roles, credit, fu = m["roles"], m["detected_credit"], m["total_faults"]

    assert credit == roles["DT"] + m["posdet_credit"] * roles["PD"]
    assert m["test_coverage"] == pytest.approx(
        100.0 * credit / (fu - roles["UD"]), abs=1e-4)
    assert m["fault_coverage"] == pytest.approx(100.0 * credit / fu, abs=1e-4)
    assert m["atpg_effectiveness"] == pytest.approx(
        100.0 * (credit + roles["UD"] + roles["AU"]) / fu, abs=1e-4)


def test_posdet_credit_is_configurable_and_reported():
    rows = ['0,  DS,  "/top/u0/y"', '0,  PT,  "/top/u1/y"']
    config = AnalysisConfig.from_dict({"posdet_credit": 1.0})
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records,
                               config=config)
    assert stats.metrics()["posdet_credit"] == 1.0
    assert stats.detected_credit == 2.0


def test_undetectable_faults_leave_the_test_coverage_denominator():
    rows = (['0,  DS,  "/top/d0/y"']
            + [f'0,  UU,  "/top/u{i}/y"' for i in range(9)])
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    assert stats.test_coverage == pytest.approx(100.0)   # 1 / (10 - 9)
    assert stats.fault_coverage == pytest.approx(10.0)   # 1 / 10


def test_empty_fault_list_produces_valid_output_not_a_division_by_zero():
    stats = compute_statistics([])
    assert stats.total_faults == 0
    assert stats.test_coverage is None
    assert stats.fault_coverage is None
    assert stats.atpg_effectiveness is None
    assert stats.metrics()["census_balances"] is True


def test_single_record_fault_list_is_valid():
    stats = compute_statistics(
        parse_fault_list_ex(_mtfi(['0,  DS,  "/top/u0/y"'])).records)
    assert stats.total_faults == 1
    assert stats.test_coverage == pytest.approx(100.0)


def test_a_fully_undetectable_partition_reports_no_test_coverage():
    """``FU == UD`` has no testable population, so the metric is undefined."""
    rows = [f'0,  UU,  "/top/u{i}/y"' for i in range(4)]
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    assert stats.test_coverage is None
    assert stats.fault_coverage == pytest.approx(0.0)


def test_a_list_with_zero_undetectable_faults_is_valid():
    rows = ['0,  DS,  "/top/u0/y"', '0,  UO,  "/top/u1/y"']
    stats = compute_statistics(parse_fault_list_ex(_mtfi(rows)).records)
    assert stats.role(CoverageRole.UD) == 0
    assert stats.test_coverage == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Structural: the class is read by field, never by substring
# ---------------------------------------------------------------------------
def test_class_tokens_inside_a_location_path_never_become_classes():
    """A path containing "AU", "DS" or "TI" must not shift any count.

    On one real partition ``grep -c 'AU'`` returned 16,463 against a correct
    15,227 because 1,236 instance paths contained the letters "AU". The trap is
    generic: it applies to every short class token on every design.
    """
    clean = [f'0,  DS,  "/top/blk{i}/u{i}/y"' for i in range(4)]
    colliding = [
        '0,  DS,  "/top/AU_named_block/u0/y"',
        '1,  DS,  "/top/blk/DS_TI_RE_wrapper/u1/y"',
        '0,  DS,  "/top/PT_PU_BL_UU/u2/y"',
        '1,  DS,  "/top/blk/u_AU_TC_helper/y"',
    ]
    baseline = compute_statistics(
        parse_fault_list_ex(_mtfi(clean)).records)
    collided = compute_statistics(
        parse_fault_list_ex(_mtfi(clean + colliding)).records)

    assert baseline.role(CoverageRole.DT) == 4
    assert collided.role(CoverageRole.DT) == 8
    assert collided.role(CoverageRole.AU) == 0
    assert collided.role(CoverageRole.UD) == 0
    assert collided.unrecognised_count == 0


def test_flat_format_does_not_read_a_class_out_of_a_path():
    records, _ = parse_fault_list("UO 1 /top/AU_block/DS_cell/u1/y")
    assert len(records) == 1
    assert records[0].dotted_class == "UO"
    assert records[0].fault_object == "/top/AU_block/DS_cell/u1/y"


# ---------------------------------------------------------------------------
# Structural: the file declares its own layout
# ---------------------------------------------------------------------------
def test_a_non_default_format_field_order_parses_correctly():
    text = _mtfi(['"/top/u0/y",  AU.TC,  0', '"/top/u1/y",  UO,  1'],
                 fmt="Location, Class, Identifier")
    result = parse_fault_list_ex(text)
    assert result.header.format_fields == ["location", "class", "identifier"]
    assert [r.dotted_class for r in result.records] == ["AU.TC", "UO"]
    assert [r.fault_object for r in result.records] == ["/top/u0/y",
                                                        "/top/u1/y"]
    assert [r.fault_type for r in result.records] == ["0", "1"]


def test_header_facts_are_propagated_not_assumed():
    result = parse_fault_list_ex(
        _mtfi(['0,  DS,  "/top/u0/y"'], collapsing="TRUE"))
    header = result.header
    assert header.version == "1"
    assert header.fault_collapsing is True
    assert header.collapsing_label == "collapsed"
    assert header.fault_models == ["Stuck"]


def test_undeclared_collapsing_is_reported_as_undeclared_not_guessed():
    text = (
        "FaultInformation {\n FaultType (Stuck) {\n  FaultList {\n"
        "   Format : Identifier, Class, Location;\n"
        '   Instance ("") {\n      0,  DS,  "/top/u0/y";\n   }\n  }\n }\n}\n'
    )
    header = parse_fault_list_ex(text).header
    assert header.fault_collapsing is None
    assert "not declared" in header.collapsing_label


def test_several_fault_models_get_a_census_each():
    text = (
        "FaultInformation {\n version : 1;\n"
        " FaultType (Stuck) {\n  FaultList {\n"
        "   Format : Identifier, Class, Location;\n"
        '   Instance ("") {\n      0,  DS,  "/top/u0/y";\n'
        '      0,  AU,  "/top/u1/y";\n   }\n  }\n }\n'
        " FaultType (Transition) {\n  FaultList {\n"
        "   Format : Identifier, Class, Location;\n"
        '   Instance ("") {\n      1,  UO,  "/top/u2/y";\n   }\n  }\n }\n}\n'
    )
    header = parse_fault_list_ex(text).header
    assert header.fault_models == ["Stuck", "Transition"]
    assert sum(header.per_model_counts["Stuck"].values()) == 2
    assert sum(header.per_model_counts["Transition"].values()) == 1


def test_a_missing_format_line_is_warned_about_not_silently_assumed():
    text = (
        "FaultInformation {\n FaultType (Stuck) {\n  FaultList {\n"
        '   Instance ("") {\n      0,  DS,  "/top/u0/y";\n   }\n  }\n }\n}\n'
    )
    result = parse_fault_list_ex(text)
    assert any("no 'Format :'" in w for w in result.warnings)
    assert len(result.records) == 1


# ---------------------------------------------------------------------------
# Structural: compression is sniffed, never inferred from the name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("suffix,writer", [
    (".mtfi", None),
    (".mtfi.gz", gzip.compress),
    (".mtfi", gzip.compress),          # gzip content, misleading plain name
    (".mtfi.gz", None),                # plain content, misleading .gz name
    (".mtfi.bz2", bz2.compress),
    (".mtfi.xz", lzma.compress),
])
def test_format_sniffing_ignores_the_file_name(tmp_path, suffix, writer):
    """Identical content yields an identical census however it is stored.

    One real partition shipped uncompressed ASCII named ``*.faults.mtfi.gz``
    and every extension-based reader failed on it.
    """
    rows = ['0,  DS,  "/top/u0/y"', '1,  AU.TC,  "/top/u1/y"',
            '0,  UU,  "/top/u2/y"']
    payload = _mtfi(rows).encode("utf-8")
    path = tmp_path / f"faults{suffix}"
    path.write_bytes(writer(payload) if writer else payload)

    result = parse_fault_list_file_ex(str(path))
    stats = compute_statistics(result.records)
    assert [r.dotted_class for r in result.records] == ["DS", "AU.TC", "UU"]
    assert stats.metrics()["roles"] == {"DT": 1, "PD": 0, "UD": 1,
                                        "AU": 1, "ND": 0}


def test_detect_compression_reads_magic_bytes(tmp_path):
    plain = tmp_path / "a.gz"
    plain.write_bytes(b"FaultInformation {\n}\n")
    assert detect_compression(str(plain)) == "none"

    zipped = tmp_path / "b.txt"
    zipped.write_bytes(gzip.compress(b"FaultInformation {\n}\n"))
    assert detect_compression(str(zipped)) == "gzip"


def test_parsing_streams_rather_than_loading_the_file(tmp_path):
    """The file entry point must accept a stream, not require a whole string."""
    rows = [f'0,  DS,  "/top/u{i}/y"' for i in range(500)]
    path = tmp_path / "big.mtfi"
    path.write_text(_mtfi(rows))
    with open(path, "r", encoding="utf-8") as handle:
        result = parse_fault_list_ex(handle)
    assert len(result.records) == 500


# ---------------------------------------------------------------------------
# Structural: scan heuristics
# ---------------------------------------------------------------------------
_COMBINATIONAL_NEIGHBOURS = """
module top (input a, input clk, output y);
  gcell_and2     u_and (.a(a),  .b(a), .y(n0));
  gcell_inv      u_inv (.a(n0), .y(n1));
  gcell_seq_scan u_ff  (.d(n1), .clk(clk), .se(1'b0), .si(1'b0), .so(), .q(n2));
  gcell_buf      u_buf (.a(n2), .y(y));
endmodule
"""


def _analyse(tmp_path, netlist_text, faults_text, constraints_text=None):
    netlist = tmp_path / "n.v"
    netlist.write_text(netlist_text)
    faults = tmp_path / "f.mtfi"
    faults.write_text(faults_text)
    constraints = None
    if constraints_text is not None:
        constraints = tmp_path / "c.do"
        constraints.write_text(constraints_text)
    return run_analysis(str(netlist), str(faults),
                        str(constraints) if constraints else None)


def test_a_combinational_neighbour_never_triggers_a_scan_boundary(tmp_path):
    """Every combinational cell is "non-scan"; that is not a boundary.

    The old predicate fired whenever any neighbour was non-scan, which on one
    real partition meant an AND gate, an inverter and a buffer inflating the
    two scan-boundary categories across roughly ten thousand faults.
    """
    report = _analyse(tmp_path, _COMBINATIONAL_NEIGHBOURS,
                      _mtfi(['0,  UO,  "/top/u_ff/d"']))
    result = report.fault_results[0]
    assert result.scan_boundary_involved is False
    assert result.root_cause not in (RootCause.SCAN_TO_NON_SCAN,
                                     RootCause.NON_SCAN_PROPAGATION)


_DANGLING_CHAIN = """
module top (input a, input clk, input se, input si, output y);
  gcell_seq_scan u_ff (.d(a), .clk(clk), .se(se), .si(si),
                       .so(SYNOPSYS_UNCONNECTED_7), .q(y));
endmodule
"""


def test_a_dangling_scan_out_is_reported_as_not_chain_connected(tmp_path):
    report = _analyse(tmp_path, _DANGLING_CHAIN,
                      _mtfi(['0,  UO,  "/top/u_ff/d"']))
    result = report.fault_results[0]
    assert result.root_cause is RootCause.SCAN_NOT_CHAIN_CONNECTED
    assert result.root_cause is not RootCause.SCAN_TO_NON_SCAN
    assert any("not chain-connected" in c
               for c in result.inferred_conclusions)


def test_unconnected_net_convention_is_configurable(tmp_path):
    netlist = """
module top (input a, input clk, input se, input si, output y);
  gcell_seq_scan u_ff (.d(a), .clk(clk), .se(se), .si(si),
                       .so(nc_house_style_42), .q(y));
endmodule
"""
    set_config(AnalysisConfig())
    report = _analyse(tmp_path, netlist, _mtfi(['0,  UO,  "/top/u_ff/d"']))
    assert (report.fault_results[0].root_cause
            is not RootCause.SCAN_NOT_CHAIN_CONNECTED)

    set_config(AnalysisConfig.from_dict(
        {"unconnected_net_patterns": ["nc_house_style_*"]}))
    report = _analyse(tmp_path, netlist, _mtfi(['0,  UO,  "/top/u_ff/d"']))
    assert (report.fault_results[0].root_cause
            is RootCause.SCAN_NOT_CHAIN_CONNECTED)


def test_a_library_with_its_own_scan_pin_names_is_classified_by_config(tmp_path):
    """Scan-ness comes from the pin list under a configurable vocabulary."""
    netlist = """
module top (input a, input clk, output y);
  vendor_flop u_ff (.dat(a), .clkin(clk), .shiftctl(1'b0),
                    .chain_data_in(1'b0), .chain_data_out(y), .q(y));
endmodule
"""
    faults = _mtfi(['0,  UO,  "/top/u_ff/dat"'])

    set_config(AnalysisConfig())
    report = _analyse(tmp_path, netlist, faults)
    assert report.fault_results[0].scan_cell_state == "non_scan"

    set_config(AnalysisConfig.from_dict({
        "scan_in_pins": ["chain_data_in"],
        "scan_out_pins": ["chain_data_out"],
        "shift_enable_pins": ["shiftctl"],
        "clock_pins": ["clkin"],
    }))
    report = _analyse(tmp_path, netlist, faults)
    assert report.fault_results[0].scan_cell_state == "scan"


def test_scan_status_is_never_decided_from_a_name(tmp_path):
    """A cell called "sdff" with no scan pins is not a scan cell."""
    netlist = """
module top (input a, input clk, output y);
  sdff_scan_muxdff u_ff (.d(a), .clk(clk), .q(y));
endmodule
"""
    report = _analyse(tmp_path, netlist, _mtfi(['0,  UO,  "/top/u_ff/d"']))
    assert report.fault_results[0].scan_cell_state == "non_scan"


# ---------------------------------------------------------------------------
# Structural: constraint dofile grammar
# ---------------------------------------------------------------------------
def test_an_unparsable_directive_is_recorded_and_counted_never_dropped():
    result = parse_constraints_ex(
        "add_input_constraints pin_a C0\n"
        "site_specific_wrapper_command top/blk/u0\n")
    assert len(result.records) == 2
    unknown = [r for r in result.records if r.kind == "unknown"]
    assert len(unknown) == 1
    assert unknown[0].resolved is False
    assert unknown[0].line_number == 2
    assert [t.token for t in result.unrecognised.tokens] == [
        "site_specific_wrapper_command"]


def test_a_partly_parsed_file_never_reads_as_an_unconstrained_design():
    result = parse_constraints_ex("frobnicate top/blk/u0\n")
    assert result.unresolved_count == 1
    assert any("NOT proven unconstrained" in w for w in result.warnings)


def test_line_continuations_and_comments_are_handled():
    result = parse_constraints_ex(
        "# a comment\n"
        "add_cell_constraints T1 \\\n"
        "    top/blk/u0 \\\n"
        "    top/blk/u1\n")
    signals = {r.signal for r in result.records if r.signal}
    assert signals == {"top/blk/u0", "top/blk/u1"}
    assert all(r.value == "1" for r in result.records if r.signal)


def test_variables_are_substituted_and_a_missing_one_is_unresolved():
    result = parse_constraints_ex(
        "set BLK top/core\n"
        "add_input_constraints ${BLK}/pin_a C1\n"
        "add_input_constraints $NOT_SET/pin_b C0\n")
    resolved = [r for r in result.records if r.resolved]
    assert any(r.signal == "top/core/pin_a" for r in resolved)
    assert any(not r.resolved for r in result.records)


def test_dofile_includes_are_followed_relative_to_the_including_file(tmp_path):
    (tmp_path / "inner.do").write_text("add_input_constraints inner_pin C1\n")
    outer = tmp_path / "outer.do"
    outer.write_text("dofile inner.do\nadd_input_constraints outer_pin C0\n")

    from atpg_coverage_debug_agent.parser.constraint_parser import (
        parse_constraints_file_ex,
    )
    result = parse_constraints_file_ex(str(outer))
    signals = {r.signal for r in result.records}
    assert {"inner_pin", "outer_pin"} <= signals
    assert len(result.files) == 2


def test_a_missing_include_is_unresolved_not_ignored(tmp_path):
    outer = tmp_path / "outer.do"
    outer.write_text("dofile nowhere.do\n")
    from atpg_coverage_debug_agent.parser.constraint_parser import (
        parse_constraints_file_ex,
    )
    result = parse_constraints_file_ex(str(outer))
    assert result.unresolved_count == 1
    assert "not found" in result.records[0].notes


def test_an_unevaluated_conditional_is_recorded_and_its_body_flagged():
    result = parse_constraints_ex(
        'if { $MODE == "internal" } { add_input_constraints int_pin C1 }\n')
    conditional = [r for r in result.records if r.kind == "conditional"]
    assert len(conditional) == 1
    assert conditional[0].resolved is False
    inner = [r for r in result.records if r.signal == "int_pin"]
    assert inner and inner[0].conditional is True


def test_collections_expand_against_the_netlist():
    netlist = parse_verilog("""
module top (input a, output y);
  gcell_and2 u_hold_a (.a(a), .b(a), .y(n0));
  gcell_and2 u_hold_b (.a(n0), .b(a), .y(y));
  gcell_and2 u_other  (.a(a), .b(a), .y(n1));
endmodule
""")
    result = parse_constraints_ex(
        "add_cell_constraints T0 [get_cells -hier u_hold_* ]\n",
        netlist=netlist)
    signals = {r.signal for r in result.records}
    assert signals == {"u_hold_a", "u_hold_b"}
    assert all(r.resolved for r in result.records)


def test_a_collection_without_a_netlist_is_unresolved_not_empty():
    result = parse_constraints_ex(
        "add_cell_constraints T0 [get_cells -hier u_hold_* ]\n")
    assert result.unresolved_count == 1
    assert "no netlist" in result.records[0].notes


def test_directive_vocabulary_is_extendable_from_configuration():
    config = AnalysisConfig.from_dict({
        "constraint_directives": {"site_specific_wrapper_command": "constrain"}
    })
    result = parse_constraints_ex(
        "site_specific_wrapper_command top/blk/u0\n", config=config)
    assert result.records[0].kind == "constrain"
    assert result.unrecognised.count == 0


def test_option_flags_are_captured_and_their_arguments_are_not_signals():
    result = parse_constraints_ex(
        "add_clocks 0 clk1 -pulse_always\nset_atpg_limits -abort_limit 500\n")
    clock = [r for r in result.records if r.kind == "clock"][0]
    assert clock.signal == "clk1"
    assert "-pulse_always" in clock.options
    limit = [r for r in result.records if r.kind == "limit"][0]
    assert limit.options["-abort_limit"] == "500"
    assert limit.signal is None


# ---------------------------------------------------------------------------
# Golden-file tests, one per onboarded partition
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("partition", _partition_dirs(),
                         ids=lambda p: p.name)
def test_partition_census_and_metrics_match_the_fixture(partition):
    expected = json.loads((partition / "expected.json").read_text())
    result = parse_fault_list_file_ex(str(partition / "faults.mtfi"))
    stats = compute_statistics(result.records)
    m = stats.metrics()

    assert stats.total_faults == expected["total_faults"]
    assert m["roles"] == expected["roles"]
    assert stats.unrecognised_count == expected["unrecognised_class_records"]
    assert stats.census_balances
    assert m["posdet_credit"] == expected["posdet_credit"]
    assert m["detected_credit"] == pytest.approx(expected["detected_credit"])
    assert m["effectiveness_posdet_count"] == \
        expected["effectiveness_posdet_count"]
    for key in ("test_coverage", "fault_coverage", "atpg_effectiveness"):
        assert m[key] == pytest.approx(expected[key], abs=1e-3), key

    actual_classes = {}
    for stat in stats.subclass_stats:
        actual_classes[stat.subclass_id] = stat.count
    assert actual_classes == expected["class_counts"]

    header = result.header
    assert header.fault_collapsing == expected["fault_collapsing"]
    assert header.fault_models == expected["fault_models"]
    assert header.format_fields == expected["format_fields"]
    assert header.version == expected["declared_version"]


@pytest.mark.parametrize("partition", _partition_dirs(),
                         ids=lambda p: p.name)
def test_partition_analyses_end_to_end(partition):
    expected = json.loads((partition / "expected.json").read_text())
    constraints = partition / "constraints.do"
    report = run_analysis(
        str(partition / "netlist.v"),
        str(partition / "faults.mtfi"),
        str(constraints) if constraints.is_file() else None,
    )
    assert report.summary.total_faults == expected["total_faults"]
    assert report.statistics.census_balances
    assert report.fault_list_header is not None
    assert report.analysis_config is not None

    con_expected = expected.get("constraints")
    if con_expected and constraints.is_file():
        status = report.constraint_diagnostics
        assert status is not None
        assert status["unresolved"] >= con_expected["min_unresolved"]
        tokens = {t["token"] for t in
                  (status["unrecognised"] or {}).get("tokens", [])}
        assert set(con_expected["unrecognised_directive_tokens"]) <= tokens


def test_adding_a_partition_is_data_only(tmp_path):
    """A new partition must need inputs plus expected values, nothing else.

    The check is mechanical: copy an existing fixture, perturb only its data,
    and the same test code must produce the new expected numbers. If this ever
    needs a source change, the generalisation is incomplete.
    """
    partitions = _partition_dirs()
    assert partitions, "at least one golden partition fixture must exist"
    source = partitions[0]

    new = tmp_path / "synth_b"
    new.mkdir()
    (new / "netlist.v").write_text((source / "netlist.v").read_text())
    # Same shape, entirely different data: different classes and paths.
    (new / "faults.mtfi").write_text(_mtfi([
        '0,  DS,  "/other_top/other_blk/inst_0/pin"',
        '1,  DS,  "/other_top/other_blk/inst_1/pin"',
        '0,  BL,  "/other_top/other_blk/inst_2/pin"',
        '1,  AU.PC,  "/other_top/other_blk/inst_3/pin"',
    ]))
    stats = compute_statistics(
        parse_fault_list_file_ex(str(new / "faults.mtfi")).records)
    assert stats.metrics()["roles"] == {"DT": 2, "PD": 0, "UD": 1,
                                        "AU": 1, "ND": 0}
    assert stats.census_balances
    assert stats.test_coverage == pytest.approx(200.0 / 3, abs=1e-3)


# ---------------------------------------------------------------------------
# Generality guards
# ---------------------------------------------------------------------------
def _profile_tokens():
    """Every identifying value in the launch profiles present on this machine.

    Derived rather than listed, for two reasons: a new profile is covered with
    no test edit, and a site's real tool paths, project names and licence
    servers never have to be written into a committed source file to be
    guarded against.

    Only the value fields are collected. The flag fields (``-proj``, ``-cfg``,
    ``-ward`` ...) are the vocabulary the package is supposed to contain, and
    the sanitised ``example`` template is skipped because its placeholders are
    deliberately ordinary words.
    """
    value_keys = {"executable", "proj", "cfg"}
    tokens = set()
    for path in sorted((Path(__file__).parent.parent / "profiles").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        name = str(data.get("name") or "").strip().lower()
        if name == "example":
            continue
        for section in ("psetup", "tool"):
            for key, value in dict(data.get(section) or {}).items():
                if key in value_keys and isinstance(value, str):
                    tokens.add(value.strip().lower())
        for value in dict(data.get("environment") or {}).values():
            if isinstance(value, str):
                tokens.add(value.strip().lower())
        if len(name) > 3:
            tokens.add(name)
    return {t for t in tokens if len(t) > 3 and not t.startswith("-")}


def test_no_partition_specific_identifier_is_hard_coded_in_the_package():
    """The bug report's partition is a fixture, never a value in the source.

    Nothing about the design that exposed the defect may leak into the tool:
    not its name, not its cell library prefixes, not its fault counts.  The
    same rule covers launch profiles: a project name, a site tool path or a
    licence server belongs in ``profiles/*.json``, never in the package.
    """
    package = Path(__file__).parent.parent / "atpg_coverage_debug_agent"
    forbidden = set((
        "punit", "par_base_punit",
        "g1mtihi", "g1mtilo", "g1mfuz",
        "6340699", "6,340,699", "133402", "133,402",
        "16463", "16,463", "15227", "15,227",
        "89104", "89,104", "29076", "29,076",
        "42731", "42,731", "83450", "83,450",
        "15409", "15,409", "srmsff", "sroutxnnnh",
        # The coverage-metric fixture run: design name, counts and the
        # percentages it pins. These belong in tests/test_coverage_metrics.py.
        "par_fuse", "stuckat_edt_min",
        "767594", "767,594", "860480", "860,480",
        "818619", "818,619", "41861", "41,861",
        "84652", "84,652", "42791", "42,791",
        "691617", "691,617", "75977", "75,977",
        "90.28", "94.51", "89.87", "89.21", "93.77", "99.88", "99.81",
        # Launch-profile values are data, not code, and are read from whatever
        # profiles this machine has rather than named here.
        "cth_psetup",
    )) | _profile_tokens()
    offenders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, (
        "partition-specific identifiers must live in test fixtures, not in "
        f"the package: {offenders}")


def test_every_configurable_convention_is_documented_with_its_default():
    """The config must be able to describe itself, for report auditing."""
    documented = AnalysisConfig().documented_patterns()
    for key in ("class_roles", "posdet_credit", "scan_in_pins",
                "scan_out_pins", "shift_enable_pins", "clock_pins",
                "unconnected_net_patterns", "tie_high_patterns",
                "tie_low_patterns", "constraint_directives",
                "unknown_class_threshold_pct", "sample_limit",
                "effectiveness_posdet_families", "effectiveness_basis",
                "waiver_subclass_patterns", "fault_list_file_patterns",
                "partial_fault_file_patterns",
                "disposition_file_patterns", "phase_file_patterns"):
        assert key in documented, key
    assert documented["source"]


def test_configuration_loads_from_a_json_file(tmp_path):
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps({
        "class_roles": {"NC": "UD"},
        "posdet_credit": 0.25,
        "scan_in_pins": ["chain_in"],
    }))
    config = AnalysisConfig.load(str(path))
    assert config.role_of("NC") is CoverageRole.UD
    assert config.posdet_credit == 0.25
    assert "chain_in" in config.scan_in_pins
    assert "si" in config.scan_in_pins  # merged, not replaced
    assert config.source == str(path)


def test_a_broken_config_file_falls_back_to_defaults_rather_than_aborting(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ this is not json")
    config = AnalysisConfig.load(str(path))
    assert config.role_of("DS") is CoverageRole.DT


def test_replace_defaults_starts_from_an_empty_vocabulary():
    config = AnalysisConfig.from_dict({
        "replace_defaults": True,
        "scan_in_pins": ["only_this"],
    })
    assert config.scan_in_pins == ["only_this"]
    assert config.scan_pin_role("si") is None
