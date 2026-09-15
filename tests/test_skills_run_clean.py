"""Every built-in skill must survive a real analysis run.

``SkillManager.run_all`` catches a crashing skill and turns it into a failed
``SkillResult`` so one bad skill cannot take down an analysis. That is the right
runtime behaviour and the wrong test behaviour: a skill that raises on every
run looks exactly like a skill that ran, unless something asserts otherwise.

This file is that assertion. It exists because deleting a variable from
``dft_atpg_debug`` left a second reference behind, and the crash only surfaced
when the application was run by hand.
"""

from __future__ import annotations

import os

import pytest

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.skills.base import AnalysisContext
from atpg_coverage_debug_agent.skills.manager import SkillManager

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
def skill_results(demo_report):
    manager = SkillManager()
    ctx = AnalysisContext(
        netlist=demo_report.netlist,
        faults=demo_report.faults,
        constraints=demo_report.constraints,
        fault_results=demo_report.fault_results,
        pattern_groups=demo_report.pattern_groups,
        summary=demo_report.summary,
    )
    return manager.run_all(ctx)


def test_the_bulk_pass_actually_runs_some_skills(skill_results):
    """A vacuous pass would make every assertion below meaningless."""
    assert skill_results


def test_no_builtin_skill_crashes_on_a_real_report(skill_results):
    crashed = [(r.skill_id, r.summary) for r in skill_results if not r.success]
    assert not crashed, f"skill(s) raised during a normal analysis: {crashed}"


def test_no_builtin_skill_reports_an_error(skill_results):
    errored = [(r.skill_id, [m.text for m in r.messages if m.level == "error"])
               for r in skill_results
               if any(m.level == "error" for m in r.messages)]
    assert not errored, f"skill(s) reported errors: {errored}"


def test_every_skill_produces_a_summary(skill_results):
    missing = [r.skill_id for r in skill_results if not (r.summary or "").strip()]
    assert not missing, f"skill(s) produced no summary: {missing}"
