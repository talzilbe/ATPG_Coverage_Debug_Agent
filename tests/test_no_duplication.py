"""Guards against the same data being presented to the user twice.

A reader who meets the same number in two places, computed two different ways,
has to reconcile them before they can act. Each quantity therefore has exactly
one home, and each home is the version that carries the most evidence:

* fault counts and the coverage metric -- report section 2;
* mapped / unmapped / tied / actionable split, the scan-status breakdown and
  the hard-constant drivers -- section 3;
* per-category triage, clusters and *changeable* blocking sources -- section 4;
* the ranked, command-carrying fix plan -- section 5.

The agent is told all of that is already on screen, so its answer is confined
to judgement, corrections and narrative.
"""

from __future__ import annotations

import os
import re

import pytest

from atpg_coverage_debug_agent.agent.debug_agent import (
    SYSTEM_PROMPT, build_user_payload)
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


@pytest.fixture(scope="module")
def demo_html(demo_report):
    return build_html_report(demo_report, design_name="demo")


# ---------------------------------------------------------------------------
# Report: one home per quantity
# ---------------------------------------------------------------------------
def test_sections_are_numbered_without_gaps_including_subheadings(demo_html):
    sections = [int(m) for m in re.findall(r"<h2>(\d+)\.", demo_html)]
    assert sections == list(range(1, len(sections) + 1))

    # A sub-heading numbered 3.x inside section 4 is the classic leftover of a
    # renumbering, and it makes the reader think they are in another section.
    for parent, child in re.findall(r"<h3>(\d+)\.(\d+)", demo_html):
        assert int(parent) in sections


def test_detected_loss_headline_appears_once(demo_html):
    """The coverage metric lives in section 2 and is not repeated in the triage."""
    assert demo_html.count("Estimated structural coverage") == 1
    assert "<b>Detected:</b>" not in demo_html
    # The caveat about what the figure is not must survive the deduplication.
    assert "not</b> the ATPG tool's test-coverage figure" in demo_html


def test_hard_tie_cells_are_listed_in_one_place_only(demo_report, demo_html):
    """Section 3 owns the hard constants; the triage lists only changeable ones."""
    assert "Constant drivers holding fault sites" in demo_html

    tie_cells = [
        src.driver
        for cat in (demo_report.selected_categories or [])
        for src in getattr(getattr(cat, "attribution", None), "tie_sources", [])
        if src.kind == "tie_cell"
    ]
    assert tie_cells, "the demo must contain at least one hard tie cell"

    blocking = demo_html.split("4.4 What is blocking these faults")
    if len(blocking) > 1:
        table = blocking[1].split("<h3>")[0]
        for driver in tie_cells:
            assert driver not in table, (
                f"hard tie cell {driver} is listed twice; section 3 already "
                f"ranks it with a hierarchy-resolved trace")
        # What is left must be the drivers section 3 cannot describe.
        assert "Reprogrammable?" in table


def test_conclusions_do_not_repeat_the_fix_plan(demo_html):
    """Section 9 hands off to the fix plan instead of restating it."""
    assert "9. Conclusions" in demo_html
    assert "Expected Fault Reduction" not in demo_html
    assert "<th>Priority</th>" not in demo_html
    assert "&sect;5" in demo_html


def test_conclusions_make_no_unmeasured_coverage_prediction(demo_html):
    """The projected-coverage figure was a prediction, not a measurement."""
    assert "Expected coverage after the P1 fix" not in demo_html
    assert "would raise structural coverage" not in demo_html


def test_hotspots_rank_actionable_loss_only(demo_report, demo_html):
    assert "Actionable Faults" in demo_html
    assert "% of Actionable Loss" in demo_html

    tied = {r.instance_name for r in demo_report.fault_results
            if r.tie_driver and r.instance_name}
    ranked = {name for name, _ in demo_report.summary.top_instances}
    assert not (tied & ranked), (
        "a tie-driven instance is being ranked as a debug hotspot")


def test_markdown_mirrors_the_same_split(demo_report):
    md = render_markdown(demo_report)
    assert "## Evidence Quality" in md
    assert "Top affected instances (actionable loss only)" in md
    if "What is blocking these faults" in md:
        table = md.split("What is blocking these faults")[1]
        assert "Reprogrammable?" in table


# ---------------------------------------------------------------------------
# Agent: told not to restate what the report already computed
# ---------------------------------------------------------------------------
def test_prompt_forbids_reproducing_the_report(demo_report):
    assert "DO NOT REPRODUCE ANY OF THAT" in SYSTEM_PROMPT
    assert "Cite a section instead" in SYSTEM_PROMPT


@pytest.mark.parametrize("gone", [
    "total faults; counts by DS/DI/TI/AU/UO/UC",
    "C. Coverage-Loss Table",
    "D. Repeated Pattern Analysis",
    "top 3 next actions",
])
def test_duplicated_agent_sections_were_removed(gone):
    assert gone not in SYSTEM_PROMPT


@pytest.mark.parametrize("kept", [
    "A. Verdict",
    "B. Evidence Gaps That Change The Answer",
    "C. Corrections To The Computed Analysis",
    "D. Cross-Cutting Patterns",
    "E. Detailed Debug Notes",
    "F. Fix Plan Review",
])
def test_agent_output_is_confined_to_what_the_report_cannot_do(kept):
    assert kept in SYSTEM_PROMPT


def test_agent_ranks_the_actionable_population(demo_report):
    assert "ACTIONABLE coverage loss" in SYSTEM_PROMPT
    assert "Never" in SYSTEM_PROMPT and "raw loss total" in SYSTEM_PROMPT

    payload = build_user_payload(demo_report)
    assert "ACTIONABLE loss only" in payload
    # The per-fault table is input, and the payload says so.
    assert "do not reproduce it" in payload
