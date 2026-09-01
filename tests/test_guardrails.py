"""Tests for the honesty guardrails on generated output."""

from __future__ import annotations

import pytest

from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.analysis.guardrails import (
    PathRegistry,
    audit_report,
    check_text,
    issues_as_warnings,
    scan_claims,
    scan_paths,
)


@pytest.fixture
def registry():
    return PathRegistry([
        "top/core/fifo/u_reg_0/Q",
        "top/core/alu/u_add/Y",
        "top/io/pad_ctl/u_buf/A",
    ])


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def test_exact_source_paths_are_accepted(registry):
    assert registry.is_known("top/core/fifo/u_reg_0/Q")


def test_component_aligned_prefixes_are_accepted(registry):
    # A cluster prefix is derived from real paths, not invented.
    assert registry.is_known("top/core/fifo")
    assert registry.is_known("top/core")
    assert registry.is_known("top")


def test_partial_components_are_rejected(registry):
    # 'top/core/fi' is not a hierarchy level, it is half a name.
    assert not registry.is_known("top/core/fi")


def test_paths_that_were_never_in_the_inputs_are_rejected(registry):
    assert not registry.is_known("top/core/crypto/u_aes/Y")


def test_separator_style_does_not_change_the_verdict(registry):
    # Tessent writes dotted paths; the fault list may use either form.
    assert registry.is_known("top.core.fifo.u_reg_0.Q")
    assert registry.is_known("/top/core/fifo/u_reg_0/Q")


def test_elided_paths_are_reported_even_when_the_prefix_is_real(registry):
    issue = registry.validate("top/core/.../u_reg_0/Q")
    assert issue is not None
    assert issue.kind == "elided_path"


def test_unknown_path_is_reported_with_its_context(registry):
    issue = registry.validate("top/made/up/path", context="answer")
    assert issue.kind == "unknown_path"
    assert issue.context == "answer"


# ---------------------------------------------------------------------------
# Scanning prose
# ---------------------------------------------------------------------------
def test_scan_finds_a_fabricated_path_in_prose(registry):
    text = ("The loss concentrates under top/core/fifo, but the blocker is "
            "top/core/crypto/u_aes/Y.")
    issues = scan_paths(text, registry)
    assert [i.text for i in issues] == ["top/core/crypto/u_aes/Y"]


def test_scan_accepts_prose_that_only_quotes_real_paths(registry):
    text = ("Faults concentrate under top/core/fifo; a sample is "
            "top/core/fifo/u_reg_0/Q.")
    assert scan_paths(text, registry) == []


def test_scan_flags_an_abbreviated_path(registry):
    issues = scan_paths("see top/.../u_reg_0/Q for details", registry)
    assert any(i.kind == "elided_path" for i in issues)


def test_command_placeholders_are_not_treated_as_paths(registry):
    # Fix commands legitimately contain <output>/file style placeholders.
    text = "write_faults <output_dir>/au_pc.faults -class AU.PC"
    assert scan_paths(text, registry) == []


def test_each_bad_path_is_reported_once(registry):
    text = "top/bad/one and again top/bad/one"
    assert len(scan_paths(text, registry)) == 1


@pytest.mark.parametrize("text", [
    "User-applied C0/C1 constraints on specific named pins.",
    "the sa0/sa1 split is even",
    "tied to T0/T1 depending on the register",
    "a 0/1 decision",
])
def test_value_code_pairs_are_not_treated_as_paths(registry, text):
    # 'C0/C1' is a pair of constraint values, not a two-level hierarchy path.
    assert scan_paths(text, registry) == []


def test_a_real_two_level_path_is_still_checked(registry):
    assert scan_paths("see top/ghost for details", registry)


# ---------------------------------------------------------------------------
# Self-audit of the tool's own output
# ---------------------------------------------------------------------------
_DOTTED_FAULTS = "\n".join(
    [f"AU.PC 1 top/io/pad_ctl/u{i}/Y" for i in range(30)]
    + [f"AU.TC 0 top/core/fscan/tdr_reg/u{i}/Y" for i in range(30)]
    + [f"UO.AAB 1 top/core/aes/u{i}/Y" for i in range(20)]
    + [f"DS 0 top/misc/u{i}/Y" for i in range(40)]
)


def test_generated_report_for_every_category_stays_within_the_guardrails(
        tmp_path, sample_netlist_path, sample_constraints_path):
    """The categories with the richest generated prose must stay clean.

    AU.PC and AU.TC produce the most narrative text — named contributors,
    caveats and command templates — so they are where a fabricated path or an
    unmeasured claim would most likely slip in.
    """
    from atpg_coverage_debug_agent.app import run_analysis

    faults = tmp_path / "dotted.faults"
    faults.write_text(_DOTTED_FAULTS, encoding="utf-8")
    report = run_analysis(sample_netlist_path, str(faults),
                          sample_constraints_path)

    assert {c.subclass_id for c in report.selected_categories} >= {
        "AU.PC", "AU.TC", "UO.AAB"}
    issues = audit_report(report)
    assert issues == [], f"tool violated its own guardrails: {issues}"


# ---------------------------------------------------------------------------
# Unmeasured claims
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "This will recover 3.2% coverage.",
    "Expected gain of 1.5%",
    "a 2% coverage improvement",
    "This will recover the aborted faults.",
    "estimated coverage recovery",
    "projected improvement",
])
def test_predicted_gains_are_flagged(text):
    issues = scan_claims(text)
    assert issues and issues[0].kind == "unmeasured_claim"


@pytest.mark.parametrize("text", [
    "AU.TC accounts for 19.48% of the fault population.",
    "100.0% of these faults sit under top/core/fifo.",
    "Only 40% of the faults examined could be traced to any source.",
    "The benefit can only be established by re-running ATPG.",
])
def test_factual_percentages_are_not_flagged(text):
    assert scan_claims(text) == []


def test_check_text_runs_both_guardrails(registry):
    text = "Adding a point at top/fake/path will recover 5% coverage."
    kinds = {i.kind for i in check_text(text, registry)}
    assert kinds == {"unknown_path", "unmeasured_claim"}


def test_claims_are_still_checked_without_a_registry():
    issues = check_text("will recover 5% coverage", None)
    assert [i.kind for i in issues] == ["unmeasured_claim"]


def test_warnings_explain_why_each_issue_matters(registry):
    warnings = issues_as_warnings(
        check_text("top/fake/x will recover 5%", registry, "answer"))
    assert any("does not appear in any source artefact" in w
               for w in warnings)
    assert any("has not been measured" in w for w in warnings)


# ---------------------------------------------------------------------------
# Self-audit of the tool's own output
# ---------------------------------------------------------------------------
def test_generated_report_quotes_only_real_paths_and_predicts_no_gain(
        sample_netlist_path, sample_faults_path, sample_constraints_path):
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path,
                          sample_constraints_path)
    issues = audit_report(report)
    assert issues == [], f"tool violated its own guardrails: {issues}"


def test_self_audit_violations_surface_as_report_warnings(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    # A clean run must not manufacture guardrail warnings either.
    assert not any(w.startswith("Guardrail:") for w in report.warnings)


def test_audit_catches_a_fabricated_path_injected_into_a_recommendation(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    report.recommendations[0].evidence.append(
        "[netlist] Constant driver 'top/invented/u_ghost/Y' reaches 40 faults.")

    issues = audit_report(report)
    assert any(i.kind == "unknown_path"
               and "u_ghost" in i.text for i in issues)


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------
def _verify(paths="", text="", results=(), constraints=()):
    return investigate.run_tool(
        "verify_paths", {"paths": paths, "text": text},
        fault_results=results, constraints=constraints, netlist=None)


def test_verify_paths_tool_separates_real_from_invented(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    real = report.fault_results[0].fault.fault_object

    data = _verify(paths=f"{real} top/invented/path",
                   results=report.fault_results,
                   constraints=report.constraints)
    verdicts = {row["path"]: row["ok"] for row in data["checked"]}
    assert verdicts[real] is True
    assert verdicts["top/invented/path"] is False


def test_verify_paths_tool_scans_free_text(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.app import run_analysis

    report = run_analysis(sample_netlist_path, sample_faults_path, None)
    data = _verify(text="Adding a point at top/ghost/x will recover 4%.",
                   results=report.fault_results,
                   constraints=report.constraints)
    kinds = {i["kind"] for i in data["text_issues"]}
    assert "unknown_path" in kinds
    assert "unmeasured_claim" in kinds


def test_verify_paths_tool_accepts_a_list_argument():
    data = _verify(paths=["top/a/b"])
    assert data["checked"][0]["path"] == "top/a/b"
    assert data["checked"][0]["ok"] is False
