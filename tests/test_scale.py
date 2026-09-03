"""Scale behaviour on inputs the size of a real design.

The demo dataset has 118 faults and 5 constraints, so an accidentally
quadratic loop is invisible in every other test. These generate a design large
enough for the shape of the algorithm to show, and assert both that the
analysis still finishes and that the hot paths stay near-linear.

Timings are deliberately generous: this guards against a change of *order*,
not against a slow machine.
"""

from __future__ import annotations

import time

import pytest

from atpg_coverage_debug_agent.analysis.connectivity import ConnectivityModel
from atpg_coverage_debug_agent.analysis.mapper import FaultMapper
from atpg_coverage_debug_agent.analysis.root_cause import RootCauseEngine
from atpg_coverage_debug_agent.analysis.summarizer import build_report
from atpg_coverage_debug_agent.parser.constraint_parser import parse_constraints
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list
from atpg_coverage_debug_agent.parser.verilog_parser import parse_verilog

BLOCKS = 40
CELLS_PER_BLOCK = 50
#: 40 x 50 = 2000 instances, 4000 faults, 400 constraints. Large enough for a
#: quadratic term to dominate, small enough to stay a unit test.
CONSTRAINTS = 400

_SUBCLASSES = ["AU.TC", "AU.PC", "UO.AAB", "AU.SEQ", "DS"]


def _netlist_text() -> str:
    lines = ["module leaf (input A, input B, output Y);", "endmodule", ""]
    for b in range(BLOCKS):
        lines.append(f"module blk{b} (input clk, input din, output dout);")
        for c in range(CELLS_PER_BLOCK):
            src = "din" if c == 0 else f"n{c - 1}"
            dst = "dout" if c == CELLS_PER_BLOCK - 1 else f"n{c}"
            lines.append(f"  wire {dst};" if dst != "dout" else "")
            lines.append(f"  leaf u{c} (.A({src}), .B(clk), .Y({dst}));")
        lines.append("endmodule")
        lines.append("")
    lines.append("module top (input clk, input din, output dout);")
    for b in range(BLOCKS):
        lines.append(f"  wire b{b}_out;")
        lines.append(f"  blk{b} ub{b} (.clk(clk), .din(din), .dout(b{b}_out));")
    lines.append("endmodule")
    return "\n".join(lines)


def _fault_text() -> str:
    rows = []
    for b in range(BLOCKS):
        for c in range(CELLS_PER_BLOCK):
            cls = _SUBCLASSES[(b + c) % len(_SUBCLASSES)]
            for stuck in ("0", "1"):
                # Full path from the top module, as a real fault list quotes
                # it. Every leaf name here repeats across all 40 blocks, so
                # the mapper has to disambiguate rather than name-match.
                rows.append(f"{cls} {stuck} /top/ub{b}/u{c}/Y")
    return "\n".join(rows)


def _constraint_text() -> str:
    # Most constrained signals do not exist in the design, which is the normal
    # case for a shared constraint file and the case a full scan pays for.
    rows = [f"add_input_constraints unrelated_sig_{i} C0"
            for i in range(CONSTRAINTS - BLOCKS)]
    rows += [f"add_input_constraints top/ub{b}/u0/A C1" for b in range(BLOCKS)]
    return "\n".join(rows)


@pytest.fixture(scope="module")
def big_inputs():
    netlist = parse_verilog(_netlist_text())
    faults, _ = parse_fault_list(_fault_text())
    constraints, _ = parse_constraints(_constraint_text())
    return netlist, faults, constraints


def test_the_generated_design_is_actually_large(big_inputs):
    """Guard the guard: if generation breaks, the timings mean nothing."""
    netlist, faults, constraints = big_inputs
    conn = ConnectivityModel(netlist)
    assert len(conn.instances) >= 2000
    assert len(faults) >= 4000
    assert len(constraints) >= CONSTRAINTS - BLOCKS


def test_the_generated_faults_actually_map(big_inputs):
    """An unmapped fixture would exercise the early-exit path and prove little.

    Every leaf name repeats across all 40 blocks, so this also pins the
    hierarchy disambiguation working on full top-anchored paths.
    """
    netlist, faults, _constraints = big_inputs
    mapper = FaultMapper(ConnectivityModel(netlist))
    loss = [f for f in faults if f.is_coverage_loss]
    unresolved = sum(1 for f in loss
                     if mapper.map_object(f.fault_object).confidence.value
                     == "unresolved")
    assert unresolved == 0, f"{unresolved}/{len(loss)} faults did not map"


def test_a_full_analysis_of_a_large_design_completes(big_inputs):
    netlist, faults, constraints = big_inputs
    start = time.perf_counter()
    report = build_report(netlist, faults, constraints, [])
    elapsed = time.perf_counter() - start

    assert report.fault_results, "the run produced no analysed faults"
    assert report.statistics.total_faults == len(faults)
    assert elapsed < 60.0, f"full analysis took {elapsed:.1f}s"


def test_constraint_matching_does_not_scale_with_constraint_count(big_inputs):
    """The cost of a fault must not grow with constraints it cannot match.

    A full scan of the constraint index per fault passes every other test in
    this suite and is quadratic here. Multiplying the *unrelated* constraints
    by 100 while holding the faults fixed should barely move the runtime.

    The threshold is calibrated against both implementations: the leaf-indexed
    version measures ~1x, the old full scan ~9x.
    """
    netlist, faults, _constraints = big_inputs
    conn = ConnectivityModel(netlist)
    mapper = FaultMapper(conn)
    loss = [f for f in faults if f.is_coverage_loss][:1500]

    def _time_with(constraint_count: int) -> float:
        text = "\n".join(f"add_input_constraints unrelated_sig_{i} C0"
                         for i in range(constraint_count))
        records, _ = parse_constraints(text)
        engine = RootCauseEngine(conn, mapper, records)
        start = time.perf_counter()
        for fault in loss:
            engine.analyze_fault(fault)
        return time.perf_counter() - start

    small = _time_with(50)
    if small < 0.005:
        pytest.skip("baseline too fast to time reliably on this machine")
    large = _time_with(5000)

    assert large < small * 4.0, (
        f"{small:.3f}s with 50 constraints vs {large:.3f}s with 5000 "
        f"({large / small:.1f}x) - constraint matching looks like it scales "
        "with the constraint count")


def test_narrowed_constraint_search_matches_a_full_scan(big_inputs):
    """The index is an optimisation, so it must return identical verdicts.

    Every rule in the matcher either compares whole paths or leaf names, and a
    component-aligned suffix shares its leaf - which is what makes narrowing
    by leaf safe. This asserts that reasoning against the real thing.
    """
    netlist, faults, _constraints = big_inputs
    conn = ConnectivityModel(netlist)
    mapper = FaultMapper(conn)

    text = "\n".join(
        [f"add_input_constraints unrelated_sig_{i} C0" for i in range(200)]
        # Full paths, partial paths and bare leaf names all in one file, so
        # each match rule is exercised.
        + [f"add_input_constraints top/ub{b}/u0/A C1" for b in range(BLOCKS)]
        + [f"add_input_constraints ub3/u5/Y C0", "add_input_constraints A C0",
           "add_input_constraints n10 C1"]
    )
    records, _ = parse_constraints(text)

    narrowed = RootCauseEngine(conn, mapper, records)
    full = RootCauseEngine(conn, mapper, records)
    # Make the reference engine scan everything, as the original did.
    full._constraint_by_leaf = _ScanAllLeaves(list(full._constraint_index))

    matched_any = False
    for fault in [f for f in faults if f.is_coverage_loss]:
        a = narrowed.analyze_fault(fault)
        b = full.analyze_fault(fault)
        assert a.root_cause == b.root_cause, fault.fault_object
        assert a.constraint_related == b.constraint_related, fault.fault_object
        assert a.observed_facts == b.observed_facts, fault.fault_object
        assert a.evidence == b.evidence, fault.fault_object
        matched_any = matched_any or a.constraint_related
    assert matched_any, "no fault matched a constraint - the test proves nothing"


class _ScanAllLeaves(dict):
    """Stands in for the leaf index and offers every constraint for any leaf."""

    def __init__(self, signals):
        super().__init__()
        self._signals = signals

    def get(self, _key, _default=None):
        return self._signals


def test_driver_resolution_is_cached(big_inputs):
    """The same net must not be re-walked for every fault that touches it."""
    netlist, _faults, _constraints = big_inputs
    conn = ConnectivityModel(netlist)

    first = conn.resolve_driver("blk0", "n10")
    calls = {"n": 0}
    original = conn._resolve_driver_uncached

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    conn._resolve_driver_uncached = _counting  # type: ignore[assignment]
    for _ in range(500):
        assert conn.resolve_driver("blk0", "n10") == first
    assert calls["n"] == 0, "resolve_driver recomputed a cached resolution"


def test_caching_does_not_change_driver_resolution(big_inputs):
    """A cache that returns a different answer is worse than no cache."""
    netlist, _faults, _constraints = big_inputs
    cached = ConnectivityModel(netlist)
    for module, net in (("blk0", "n10"), ("blk1", "dout"), ("top", "b0_out"),
                        ("blk2", "din"), ("top", "nonexistent_net")):
        fresh = ConnectivityModel(netlist)
        expected = fresh._resolve_driver_uncached(module, net, 24)
        assert cached.resolve_driver(module, net) == expected
        # Second call comes from the cache and must still agree.
        assert cached.resolve_driver(module, net) == expected
