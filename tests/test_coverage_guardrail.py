"""No coverage percentage may appear without a derivable denominator.

The defect this guards against was a headline "Estimated structural coverage
~NN.N%" whose denominator appeared nowhere in the report and which matched none
of the six figures the ATPG tool produced for the same run. Deleting that one
line is not enough -- the next renderer would reintroduce the same shape -- so
the rule is enforced over the rendered output of every report path.
"""

from __future__ import annotations

import os

import pytest

from atpg_coverage_debug_agent.analysis.guardrails import (
    scan_coverage_percentages,
)
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.reporting.html_report import build_html_report
from atpg_coverage_debug_agent.reporting.markdown_report import render_markdown

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_HERE, "sample_data")


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(
        os.path.join(_SAMPLE, "demo_netlist.v"),
        os.path.join(_SAMPLE, "demo_faults.mtfi"),
        os.path.join(_SAMPLE, "demo_constraints.do"),
    )


# ---------------------------------------------------------------------------
# The scanner itself has teeth
# ---------------------------------------------------------------------------
def test_the_scanner_catches_a_bare_percentage_under_a_coverage_heading():
    issues = scan_coverage_percentages(
        "## Coverage Metric\nEstimated structural coverage ~87.4%.")
    assert len(issues) == 1
    assert issues[0].kind == "underivable_percentage"


def test_the_scanner_catches_the_html_form_too():
    issues = scan_coverage_percentages(
        "<h3>2.1 Coverage metrics</h3>\n<td>Test coverage</td><td>87.4%</td>")
    assert len(issues) == 1


def test_a_percentage_with_its_substitution_passes():
    assert not scan_coverage_percentages(
        "## Coverage metrics\n"
        "| Test coverage | 89.8746% | `(767594 + 0*862) / (860480 - 6408) "
        "= 767594 / 854072 = 89.8746%` |")


def test_a_percentage_outside_a_coverage_heading_is_not_this_check_s_business():
    assert not scan_coverage_percentages(
        "## Where the loss concentrates\ntop/blk_a holds 41.2%")


def test_a_category_share_under_a_triage_heading_is_not_a_coverage_claim():
    """A share's denominator is the stated population total, not a mystery."""
    assert not scan_coverage_percentages(
        "## Coverage Triage\n"
        "- **Coverage loss:** 84 (71.19%)\n"
        "| UO.AAB | 32 | 27.12% | 16 | 16 | 0.00 |")


def test_a_share_of_a_stated_whole_is_not_a_coverage_claim():
    assert not scan_coverage_percentages(
        "## Coverage triage\nAU.TC is 19.48% of the fault population")


def test_a_later_heading_ends_the_coverage_region():
    assert not scan_coverage_percentages(
        "## Coverage metrics\n"
        "| Test coverage | 50.0% | `14 / 32 = 43.75%` |\n"
        "## Hotspots\ntop/blk_a 41.2%")


# ---------------------------------------------------------------------------
# Acceptance criterion 5: applied to the real rendered reports
# ---------------------------------------------------------------------------
def test_the_markdown_report_has_no_underivable_coverage_percentage(
        demo_report):
    issues = scan_coverage_percentages(render_markdown(demo_report),
                                       "markdown report")
    assert not issues, [i.text for i in issues]


def test_the_html_report_has_no_underivable_coverage_percentage(demo_report):
    issues = scan_coverage_percentages(build_html_report(demo_report),
                                       "html report")
    assert not issues, [i.text for i in issues]


def test_the_deleted_estimate_stays_deleted(demo_report):
    """It was `detected / (detected + loss)`, which matches no tool figure."""
    for rendered in (render_markdown(demo_report),
                     build_html_report(demo_report)):
        assert "Estimated structural coverage" not in rendered


def test_the_skill_no_longer_emits_an_estimated_coverage_figure():
    source = os.path.join(_HERE, "atpg_coverage_debug_agent", "skills",
                          "dft_atpg_debug.py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "Estimated structural coverage" not in text


# ---------------------------------------------------------------------------
# Acceptance criterion 3, at the rendering layer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("metric", ["test_coverage", "fault_coverage",
                                    "atpg_effectiveness"])
def test_every_rendered_metric_carries_its_substitution(demo_report, metric):
    spec = demo_report.statistics.metrics()["formulas"][metric]
    rendered = render_markdown(demo_report)
    assert spec["substitution"] in rendered
