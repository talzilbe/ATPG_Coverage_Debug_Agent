"""Tests for the fault census and the offline-pass -> AI-agent hand-off contract.

Every test here corresponds to a way the hand-off failed in production. The
failure was never a wrong number: it was a *subset presented as a whole*. Six
fault classes were printed under a twenty-five-class total, nothing said the
list was partial, and the reader -- an LLM that had been told the deterministic
pass already did the counting -- closed the arithmetic itself, found a residual
it could not attribute, and invented a fault category to hold it.

So these tests assert three properties, not three behaviours:

1. the census is complete, and proves it by summing to the population;
2. anything the tool prints that is *not* complete says so, and names where the
   complete form lives;
3. a payload too large to send whole reports what it dropped, and drops counts
   last.
"""

from __future__ import annotations

import json
import os

import pytest

from atpg_coverage_debug_agent import mcp_server, session
from atpg_coverage_debug_agent.agent import debug_agent
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.analysis.census import (
    UNCLASSIFIED,
    build_census,
    census_warnings,
)
from atpg_coverage_debug_agent.analysis.statistics import compute_statistics
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.config.analysis_config import (
    AnalysisConfig,
    CoverageRole,
)
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SAMPLE = os.path.join(_ROOT, "sample_data")
_PARTITIONS = os.path.join(_HERE, "fixtures", "partitions")


def _partition_dirs():
    if not os.path.isdir(_PARTITIONS):
        return []
    return [os.path.join(_PARTITIONS, name)
            for name in sorted(os.listdir(_PARTITIONS))
            if os.path.isdir(os.path.join(_PARTITIONS, name))]


def _analyse(folder: str):
    constraints = os.path.join(folder, "constraints.do")
    return run_analysis(
        os.path.join(folder, "netlist.v"),
        os.path.join(folder, "faults.mtfi"),
        constraints if os.path.isfile(constraints) else None,
    )


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(
        os.path.join(_SAMPLE, "demo_netlist.v"),
        os.path.join(_SAMPLE, "demo_faults.mtfi"),
        os.path.join(_SAMPLE, "demo_constraints.do"),
    )


# ---------------------------------------------------------------------------
# (a) The census sums to the total, for every dataset we ship
# ---------------------------------------------------------------------------
def test_census_reconciles_for_the_demo_dataset(demo_report):
    census = build_census(demo_report)
    assert census.counted == census.total_faults
    assert census.delta == 0
    assert census.reconciles
    assert census_warnings(census) == []


@pytest.mark.parametrize("folder", _partition_dirs(),
                         ids=lambda p: os.path.basename(p))
def test_census_reconciles_for_every_partition_fixture(folder):
    census = build_census(_analyse(folder))
    assert census.counted == census.total_faults, (
        f"{os.path.basename(folder)}: the census sums to {census.counted} "
        f"but {census.total_faults} fault(s) were analysed")


@pytest.mark.parametrize("folder", _partition_dirs(),
                         ids=lambda p: os.path.basename(p))
def test_census_holds_every_class_the_fixture_declares(folder):
    """No class may be filtered out of the census, whatever its role.

    ``BL`` and ``RE`` are the ones that went missing in production: real,
    structurally meaningful, and invisible to a coverage-loss view.
    """
    with open(os.path.join(folder, "expected.json"), encoding="utf-8") as fh:
        expected = json.load(fh)
    census = build_census(_analyse(folder))
    for token, count in expected["class_counts"].items():
        entry = census.get(token)
        assert entry is not None, f"{token} is missing from the census"
        assert entry.count == count


def test_every_role_in_the_census_states_whether_it_is_in_scope(demo_report):
    """A class is either a debug target or explicitly explained as not one."""
    for role in build_census(demo_report).roles:
        assert role.scope, f"role {role.role} does not say why it is in scope"


def test_the_census_reports_the_undetectable_classes_it_does_not_debug():
    """UD classes are out of triage scope but never out of census scope."""
    folder = _partition_dirs()[0]
    census = build_census(_analyse(folder))
    ud = next(r for r in census.roles if r.role == CoverageRole.UD.value)
    assert ud.count > 0
    assert {e.subclass for e in ud.entries} >= {"UU", "TI", "BL", "RE"}
    assert "not a debug target" in ud.scope.lower()


# ---------------------------------------------------------------------------
# (b) An unknown class token surfaces, it does not vanish
# ---------------------------------------------------------------------------
_UNKNOWN_CLASS_LIST = """\
FaultInformation {
 FaultType (Stuck) {
  FaultList {
   Format : Identifier, Class, Location;
   Instance ("") {
    0,  DS,      "/top/u_a/y";
    1,  DS,      "/top/u_a/a";
    0,  AU.TC,   "/top/u_b/y";
    1,  ZZ,      "/top/u_c/y";
    0,  ZZ,      "/top/u_c/a";
   }
  }
 }
}
"""


def _unknown_class_records():
    config = AnalysisConfig(unknown_class_fatal=False)
    records, _warnings = parse_fault_list(_UNKNOWN_CLASS_LIST, config=config)
    return records, config


def test_an_unknown_class_token_lands_under_unclassified_not_nowhere():
    records, config = _unknown_class_records()
    stats = compute_statistics(records, config=config)
    census = build_census(_StatsOnly(stats), config=config)

    entry = census.get("ZZ")
    assert entry is not None, "the unknown class was dropped from the census"
    assert entry.count == 2
    assert entry.role == UNCLASSIFIED
    assert entry.subclass == "ZZ", "the verbatim token must survive"


def test_an_unknown_class_token_still_lets_the_census_reconcile():
    records, config = _unknown_class_records()
    census = build_census(_StatsOnly(compute_statistics(records,
                                                        config=config)),
                          config=config)
    assert census.counted == census.total_faults == len(records)


def test_an_unknown_class_token_raises_a_warning_naming_it():
    records, config = _unknown_class_records()
    census = build_census(_StatsOnly(compute_statistics(records,
                                                        config=config)),
                          config=config)
    warnings = census_warnings(census)
    assert warnings, "an unmapped class must be warned about, not absorbed"
    joined = " ".join(warnings)
    assert "ZZ" in joined
    assert "class_roles" in joined, "the warning must say how to fix it"


def test_an_unclassified_class_is_kept_out_of_every_coverage_metric():
    records, config = _unknown_class_records()
    metrics = compute_statistics(records, config=config).metrics()
    assert metrics["unrecognised"] == 2
    assert metrics["roles"][CoverageRole.UNKNOWN.value] \
        if CoverageRole.UNKNOWN.value in metrics["roles"] else True
    # The unknown faults are in FU but credited to nothing.
    assert metrics["total_faults"] == len(records)
    assert metrics["detected_credit"] == 2.0


class _StatsOnly:
    """Minimal stand-in for a report that carries only derived statistics."""

    def __init__(self, statistics):
        self.statistics = statistics
        self.summary = None


# ---------------------------------------------------------------------------
# (c) test_coverage uses the FULL undetectable set, not TI alone
# ---------------------------------------------------------------------------
def test_test_coverage_uses_the_full_undetectable_population():
    """The fixture is chosen so TI-only and full-UD differ materially.

    This is the arithmetic that put a real partition's headline metric ~2
    points out: ``UD`` in ``(DT + c*PD) / (FU - UD)`` is the whole
    undetectable population the class-role map defines, and reading it as the
    single class ``TI`` shrinks the denominator by everything else.
    """
    folder = _partition_dirs()[0]
    report = _analyse(folder)
    stats = report.statistics
    census = build_census(report)

    ti_only = census.get("TI").count
    full_ud = stats.role(CoverageRole.UD)
    assert full_ud > ti_only, "fixture must carry UD classes beyond TI"

    numerator = stats.detected_credit
    total = stats.total_faults
    reported = stats.test_coverage
    with_full_ud = 100.0 * numerator / (total - full_ud)
    with_ti_only = 100.0 * numerator / (total - ti_only)

    assert reported == pytest.approx(with_full_ud)
    assert abs(with_full_ud - with_ti_only) > 1.0, (
        "the fixture no longer distinguishes the two denominators, so this "
        "test cannot fail for the right reason")


def test_the_undetectable_set_is_every_ud_class_not_a_hardcoded_one():
    folder = _partition_dirs()[0]
    census = build_census(_analyse(folder))
    ud_from_census = census.role_count(CoverageRole.UD.value)
    assert ud_from_census == sum(
        census.get(t).count for t in ("UU", "TI", "BL", "RE"))


def test_each_metric_prints_its_formula_and_its_substitution():
    folder = _partition_dirs()[0]
    metrics = _analyse(folder).statistics.metrics()
    for key in ("test_coverage", "fault_coverage", "atpg_effectiveness"):
        spec = metrics["formulas"][key]
        assert spec["formula"]
        # The substitution must contain real numbers, not the symbols again.
        assert "=" in spec["substitution"]
        assert any(ch.isdigit() for ch in spec["substitution"])
    assert "not TI alone" in metrics["ud_definition"]
    assert metrics["basis"]


# ---------------------------------------------------------------------------
# (d) Truncation is structured, and counts are cut last
# ---------------------------------------------------------------------------
def _bulky_payload():
    return {
        "census": {
            "classes": [{"subclass": f"C{i}", "count": i, "role": "ND"}
                        for i in range(25)],
            "reconciliation": {"total_faults": 300, "delta": 0,
                               "reconciles": True},
        },
        "faults": [
            {"fault_object": f"/top/u_{i}/y",
             "evidence": ["x" * 400],
             "observed_facts": ["y" * 400],
             "count": i}
            for i in range(60)
        ],
    }


def test_an_oversized_payload_reports_what_it_dropped(tmp_path):
    payload = _bulky_payload()
    out = mcp_server.shrink_payload(payload, limit=2000,
                                    spill_dir=str(tmp_path), tool_name="demo")
    meta = out["_truncation"]
    assert meta["truncated"] is True
    assert meta["omitted_fields"]
    assert meta["retrieval_hint"]
    assert "NOT A READ RESULT" in meta["warning"]


def test_an_oversized_payload_spills_the_complete_form_to_a_file(tmp_path):
    out = mcp_server.shrink_payload(_bulky_payload(), limit=2000,
                                    spill_dir=str(tmp_path), tool_name="demo")
    spill = out["_truncation"]["spill_path"]
    assert spill and os.path.isfile(spill)
    with open(spill, encoding="utf-8") as fh:
        complete = json.load(fh)
    assert len(complete["faults"]) == 60, "the spill must be the whole payload"


def test_truncation_drops_prose_before_counts(tmp_path):
    out = mcp_server.shrink_payload(_bulky_payload(), limit=2000,
                                    spill_dir=str(tmp_path), tool_name="demo")
    # The census survives intact...
    assert len(out["census"]["classes"]) == 25
    assert out["census"]["reconciliation"]["total_faults"] == 300
    # ...while the prose that made the payload big is gone.
    assert all("evidence" not in row for row in out["faults"])
    assert any("evidence" in field for field in out["_truncation"]["omitted_fields"])


def test_a_payload_that_fits_is_returned_untouched(tmp_path):
    small = {"total": 3, "counts": {"a": 1}}
    out = mcp_server.shrink_payload(small, limit=10000,
                                    spill_dir=str(tmp_path), tool_name="demo")
    assert out == small
    assert "_truncation" not in out


def test_the_mcp_server_truncates_a_real_tool_response(demo_report, tmp_path,
                                                       monkeypatch):
    """End to end: an over-limit tool call still returns complete counts."""
    monkeypatch.setenv("ATPG_MCP_MAX_INLINE_CHARS", "1200")
    monkeypatch.setenv(session.SESSION_DIR_ENV, str(tmp_path))
    state = _server_state(demo_report, tmp_path)

    response = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "report_context", "arguments": {}}}, state)
    data = json.loads(response["result"]["content"][0]["text"])

    assert data["_truncation"]["truncated"] is True
    census = build_census(demo_report)
    assert len(data["census"]["classes"]) == census.class_count, (
        "the census was truncated; counts must be cut last, never first")
    assert data["census"]["reconciliation"]["reconciles"] is True


def _server_state(report, tmp_path):
    evidence = investigate.export_evidence(
        report.fault_results, report.constraints, report.netlist,
        triage=investigate.serialize_triage(report.statistics,
                                            report.selected_categories,
                                            report.recommendations),
        context=investigate.serialize_context(report),
        design=investigate.serialize_design(report.netlist, report),
        stamp=session.stamp("demo", getattr(report, "sources", None)))
    path = os.path.join(str(tmp_path), "evidence.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(evidence, fh)
    return mcp_server.load_state(path)


# ---------------------------------------------------------------------------
# (e) The digest manifest declares its own completeness
# ---------------------------------------------------------------------------
def test_the_manifest_says_complete_when_the_whole_census_is_printed(
        demo_report):
    payload = debug_agent.build_user_payload(demo_report)
    assert "HANDOFF_MANIFEST" in payload
    assert "census_in_digest: complete" in payload
    assert "census_sums_to_total: True" in payload


def test_the_manifest_says_partial_when_the_printed_census_is_a_subset(
        demo_report, monkeypatch):
    monkeypatch.setattr(debug_agent, "MAX_DIGEST_CENSUS_ROWS", 2)
    payload = debug_agent.build_user_payload(demo_report)
    census = build_census(demo_report)

    assert f"census_in_digest: partial (2 of {census.class_count} classes)" \
        in payload
    assert "PARTIAL LIST" in payload
    assert debug_agent.CENSUS_HOME in payload, (
        "a subset must name where the complete census lives")


def test_the_manifest_names_where_the_complete_census_lives(demo_report):
    payload = debug_agent.build_user_payload(demo_report)
    assert f"census_complete_via: {debug_agent.CENSUS_HOME}" in payload


def test_the_digest_no_longer_filters_the_class_list_to_a_fixed_set(
        demo_report):
    """The original defect: a hardcoded six-class filter under a full total."""
    payload = debug_agent.build_user_payload(demo_report)
    for entry in build_census(demo_report).entries:
        assert f"{entry.subclass}: {entry.count}" in payload, (
            f"{entry.subclass} is missing from the digest census")


def test_the_loss_category_block_is_labelled_as_a_subset(demo_report):
    payload = debug_agent.build_user_payload(demo_report)
    assert "COVERAGE-LOSS SUBSET" in payload
    assert "must NOT be summed against" in payload


def test_the_digest_carries_the_metrics_with_their_substitutions(demo_report):
    payload = debug_agent.build_user_payload(demo_report)
    assert "test coverage:" in payload
    assert "(DT + c*PD) / (FU - UD)" in payload


def test_the_manifest_reports_the_fault_table_as_sampled_when_it_is(
        demo_report):
    payload = debug_agent.build_user_payload(demo_report, max_faults=3)
    loss = len(demo_report.fault_results)
    assert f"faults_in_fault_table: 3 of {loss} (sampled)" in payload


# ---------------------------------------------------------------------------
# Tool-state consistency: one parsed-design handle, one answer
# ---------------------------------------------------------------------------
def _target_with_recorded_pin_evidence(report):
    for result in report.fault_results:
        if (result.scan_evidence or "").strip():
            return result.fault.fault_object
    raise AssertionError("the demo recorded no instantiations")


def test_scan_status_answers_out_of_process_from_recorded_pin_evidence(
        demo_report, tmp_path):
    """The failure this prevents: two tools disagreeing about the netlist.

    ``get_fault_detail`` was returning verbatim instantiations while
    ``scan_status`` answered "no parsed netlist is available in this session"
    -- and the agent is required to prove scan status only from
    ``scan_status``, so its primary evidence path was silently closed.
    """
    target = _target_with_recorded_pin_evidence(demo_report)
    state = _server_state(demo_report, tmp_path)

    answer = investigate.run_tool(
        "scan_status", {"target": target},
        fault_results=state["faults"], constraints=state["constraints"],
        netlist=None, adjacency=state["adjacency"],
        context=state.get("context"), design=state.get("design"))

    assert answer["verdict"] in ("scan", "non_scan")
    assert "No parsed netlist is available" not in json.dumps(answer)
    assert "recorded during the analysis pass" in answer["source"]
    assert answer["instantiation"], "the pin evidence must be quoted back"


def test_get_fault_detail_and_scan_status_agree_about_the_same_site(
        demo_report, tmp_path):
    target = _target_with_recorded_pin_evidence(demo_report)
    state = _server_state(demo_report, tmp_path)
    common = dict(fault_results=state["faults"],
                  constraints=state["constraints"], netlist=None,
                  adjacency=state["adjacency"], design=state.get("design"))

    detail = investigate.run_tool("get_fault_detail", {"fault": target},
                                  **common)
    status = investigate.run_tool("scan_status", {"target": target}, **common)

    quoted = detail["faults"][0].get("scan_evidence", "")
    assert quoted, "get_fault_detail quoted no instantiation"
    assert status["verdict"] != "unresolved", (
        "one tool quoted the instantiation while the other refused to read it")


def test_scan_status_never_claims_the_netlist_was_absent_when_it_was_parsed(
        demo_report, tmp_path):
    state = _server_state(demo_report, tmp_path)
    answer = investigate.run_tool(
        "scan_status", {"target": "/no/such/object/anywhere"},
        fault_results=state["faults"], constraints=state["constraints"],
        netlist=None, adjacency=state["adjacency"],
        design=state.get("design"))

    assert answer["verdict"] == "unresolved"
    assert answer["netlist_parsed"] is True
    blockers = " ".join(answer["blockers"])
    assert "netlist WAS parsed" in blockers
    assert "No netlist was parsed" not in blockers


def test_diagnose_unresolved_uses_the_recorded_diagnosis_out_of_process(
        demo_report, tmp_path):
    state = _server_state(demo_report, tmp_path)
    answer = investigate.run_tool(
        "diagnose_unresolved", {},
        fault_results=state["faults"], constraints=state["constraints"],
        netlist=None, adjacency=state["adjacency"],
        design=state.get("design"))
    assert "No netlist was parsed" not in json.dumps(answer)
    assert answer.get("source")


def test_the_design_handle_records_that_the_netlist_was_parsed(demo_report):
    design = investigate.serialize_design(demo_report.netlist, demo_report)
    assert design["netlist_parsed"] is True
    assert design["modules"] > 0
    assert design["instances"] > 0


# ---------------------------------------------------------------------------
# report_context carries the complete census inline
# ---------------------------------------------------------------------------
def test_report_context_carries_the_complete_census(demo_report):
    context = investigate.serialize_context(demo_report)
    answer = investigate.report_context(context)
    census = build_census(demo_report)
    assert len(answer["census"]["classes"]) == census.class_count
    assert answer["census"]["reconciliation"]["reconciles"] is True


def test_report_context_returns_the_census_even_for_another_section(
        demo_report):
    context = investigate.serialize_context(demo_report)
    answer = investigate.report_context(context, section="patterns")
    assert "census" in answer, (
        "the census must not be filterable away; it is what makes a residual "
        "impossible")


def test_report_context_never_abridges_the_census(demo_report):
    context = investigate.serialize_context(demo_report)
    answer = investigate.report_context(context, limit=1)
    census = build_census(demo_report)
    assert len(answer["census"]["classes"]) == census.class_count


def test_report_context_exposes_the_coverage_metrics(demo_report):
    context = investigate.serialize_context(demo_report)
    answer = investigate.report_context(context)
    assert answer["coverage_metrics"]["formulas"]["test_coverage"]


def test_coverage_triage_carries_its_own_sum_check(demo_report):
    triage = investigate.serialize_triage(demo_report.statistics,
                                          demo_report.selected_categories,
                                          demo_report.recommendations)
    answer = investigate.coverage_triage(triage)
    check = answer["census_check"]
    assert check["reconciles"] is True
    assert check["sum_of_category_counts"] == check["total_faults"]
    assert check["delta"] == 0


# ---------------------------------------------------------------------------
# report_handoff_gap: a channel for "these numbers contradict each other"
# ---------------------------------------------------------------------------
def test_report_handoff_gap_is_an_available_tool():
    assert "report_handoff_gap" in investigate.TOOL_SPECS


def test_report_handoff_gap_records_the_inconsistency(demo_report):
    context = investigate.serialize_context(demo_report)
    answer = investigate.report_handoff_gap(
        observed="classes sum to 6207297 against a stated 6340699",
        expected="the two should be equal",
        where="the digest's fault class block",
        context=context)
    assert answer["verdict"] == "handoff_gap"
    assert answer["observed"]
    assert "Do NOT invent a category" in answer["acknowledged"]


def test_report_handoff_gap_hands_back_the_authoritative_census(demo_report):
    context = investigate.serialize_context(demo_report)
    answer = investigate.report_handoff_gap(observed="does not add up",
                                            context=context)
    assert answer["authoritative_census"]["reconciliation"]["reconciles"]


def test_report_handoff_gap_requires_the_observation():
    assert "error" in investigate.report_handoff_gap(observed="  ")


def test_handoff_gap_is_distinct_from_insufficient_evidence():
    """They mean different things and must not be collapsed into one tool."""
    gap = investigate.TOOL_SPECS["report_handoff_gap"]["description"]
    missing = investigate.TOOL_SPECS[
        "report_insufficient_evidence"]["description"]
    assert "contradicts itself" in gap or "contradict" in gap
    assert gap != missing


def test_insufficient_evidence_no_longer_covers_unretrieved_evidence():
    answer = investigate.report_insufficient_evidence(question="anything?")
    assert "not retrieved it yet" in answer["acknowledged"] \
        or "have not retrieved" in answer["acknowledged"]


# ---------------------------------------------------------------------------
# The agent's own rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule", [
    "RECONCILE BEFORE YOU QUOTE",
    "IF THE SUM CHECK FAILS, STOP",
    "NEVER compute a coverage metric from a class list",
    "A TRUNCATED RESULT IS NOT A READ RESULT",
    "reserved for evidence that DOES NOT EXIST",
])
def test_the_system_prompt_states_the_handoff_rules(rule):
    assert rule in debug_agent.SYSTEM_PROMPT


def test_the_agentic_prompt_points_at_the_handoff_gap_tool():
    assert "report_handoff_gap" in debug_agent.AGENTIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Session artefacts are namespaced and stamped
# ---------------------------------------------------------------------------
def test_a_session_directory_names_its_design_and_run(tmp_path, monkeypatch):
    monkeypatch.delenv(session.SESSION_DIR_ENV, raising=False)
    path = session.session_dir("my_design", "run-1", reuse_env=False)
    try:
        assert os.path.isdir(path)
        assert "my_design_run-1" == os.path.basename(path)
    finally:
        session.cleanup(path)
        assert not os.path.isdir(path)


def test_a_child_process_reuses_the_parent_session_directory(tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv(session.SESSION_DIR_ENV, str(tmp_path))
    assert session.session_dir("other") == str(tmp_path)


def test_evidence_is_stamped_with_its_design_and_inputs(demo_report):
    stamp = session.stamp(None, getattr(demo_report, "sources", None))
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints,
        demo_report.netlist, stamp=stamp)
    assert evidence["stamp"]["design"]
    assert evidence["stamp"]["netlist"]
    assert evidence["stamp"]["run_id"]


def test_cleanup_refuses_to_delete_outside_its_own_root(tmp_path):
    victim = tmp_path / "not_ours"
    victim.mkdir()
    session.cleanup(str(victim))
    assert victim.is_dir(), "cleanup must not touch paths outside its root"
