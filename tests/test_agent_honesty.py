"""Tests for the agent's honesty machinery: correction, context and gaps.

These cover the parts of the loop that decide what reaches the user, rather
than the transport plumbing: whether an unsupported claim is corrected instead
of merely annotated, whether the agent can see how far the evidence goes, and
whether "not determined" is an action it can actually take.
"""

from __future__ import annotations

import os

import pytest

from atpg_coverage_debug_agent.agent.debug_agent import (
    AGENTIC_SYSTEM_PROMPT,
    CORRECTION_SYSTEM_PROMPT,
    MAX_TRIAGE_CATEGORIES,
    SYSTEM_PROMPT,
    AgentConfig,
    DebugAgent,
    build_user_payload,
)
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.analysis.report_edit import apply_exclusions
from atpg_coverage_debug_agent.app import run_analysis

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_HERE, "sample_data")


@pytest.fixture(scope="module")
def demo_report():
    return run_analysis(
        os.path.join(_SAMPLE, "demo_netlist.v"),
        os.path.join(_SAMPLE, "demo_faults.mtfi"),
        os.path.join(_SAMPLE, "demo_constraints.do"),
    )


class _ScriptedAgent(DebugAgent):
    """A DebugAgent whose model calls are replaced by a canned script."""

    def __init__(self, replies, **cfg):
        super().__init__(AgentConfig(backend="http", base_url="http://x",
                                     model="m", **cfg))
        self._replies = list(replies)
        self.calls = []

    def run_with_prompt(self, system_prompt: str, user_payload: str) -> str:
        self.calls.append((system_prompt, user_payload))
        return self._replies.pop(0) if self._replies else ""


# ---------------------------------------------------------------------------
# Corrective round-trip
# ---------------------------------------------------------------------------
def test_a_clean_answer_is_not_sent_back_to_the_model(demo_report):
    agent = _ScriptedAgent(["should never be used"])
    answer = "The loss concentrates in the fifo block. No paths quoted."
    assert agent.correct_guardrail_issues(answer, demo_report) == answer
    assert agent.calls == [], "a clean answer must not cost an extra call"


def test_an_unmeasured_claim_triggers_one_correction(demo_report):
    corrected = "Fixing the tie cell should help; only a re-run can measure it."
    agent = _ScriptedAgent([corrected])
    bad = "Fixing the tie cell will recover 12% coverage."

    result = agent.correct_guardrail_issues(bad, demo_report)

    assert result == corrected
    assert len(agent.calls) == 1, "exactly one corrective round-trip"
    system, payload = agent.calls[0]
    assert system == CORRECTION_SYSTEM_PROMPT
    # The model must be told what was wrong and be given the text to fix.
    assert bad in payload
    assert "recover" in payload.lower()


def test_a_fabricated_path_triggers_a_correction(demo_report):
    agent = _ScriptedAgent(["Rewritten without the invented path."])
    bad = "The blocker sits at /top/u_invented/never_existed_reg/Q."
    assert agent.correct_guardrail_issues(bad, demo_report) != bad
    assert len(agent.calls) == 1


def test_the_original_answer_survives_a_failed_correction(demo_report):
    """A broken correction call must not cost the user their analysis."""

    class _Failing(_ScriptedAgent):
        def run_with_prompt(self, system_prompt, user_payload):
            raise RuntimeError("endpoint down")

    agent = _Failing([])
    bad = "This will recover 12% coverage."
    assert agent.correct_guardrail_issues(bad, demo_report) == bad


def test_an_empty_correction_is_ignored(demo_report):
    agent = _ScriptedAgent(["   "])
    bad = "This will recover 12% coverage."
    assert agent.correct_guardrail_issues(bad, demo_report) == bad


def test_correction_can_be_switched_off(demo_report):
    agent = _ScriptedAgent(["corrected"], guardrail_retry=False)
    bad = "This will recover 12% coverage."
    assert agent.correct_guardrail_issues(bad, demo_report) == bad
    assert agent.calls == []


def test_correction_is_skipped_without_a_report():
    agent = _ScriptedAgent(["corrected"])
    bad = "This will recover 12% coverage."
    assert agent.correct_guardrail_issues(bad, None) == bad
    assert agent.calls == []


# ---------------------------------------------------------------------------
# report_context
# ---------------------------------------------------------------------------
def _context(report):
    return investigate.serialize_context(report)


def test_context_reports_how_much_of_the_loss_actually_mapped(demo_report):
    data = investigate.run_tool(
        "report_context", {}, fault_results=demo_report.fault_results,
        constraints=[], netlist=None, context=_context(demo_report))
    evidence = data["evidence"]
    assert evidence["mapped_onto_netlist"] + evidence["never_mapped"] > 0
    assert 0.0 <= evidence["mapped_share"] <= 1.0
    # The caveat that stops an unmapped fault being read as an unconnected one.
    assert "UNKNOWN connectivity, not zero" in evidence["note"]


def test_context_sections_can_be_requested_individually(demo_report):
    ctx = _context(demo_report)
    data = investigate.run_tool(
        "report_context", {"section": "patterns"},
        fault_results=[], constraints=[], netlist=None, context=ctx)
    assert "patterns" in data
    assert "evidence" not in data


def test_context_rejects_an_unknown_section(demo_report):
    data = investigate.run_tool(
        "report_context", {"section": "nonsense"},
        fault_results=[], constraints=[], netlist=None,
        context=_context(demo_report))
    assert "error" in data


def test_context_says_when_a_list_was_truncated(demo_report):
    ctx = _context(demo_report)
    data = investigate.run_tool(
        "report_context", {"section": "patterns", "limit": 2},
        fault_results=[], constraints=[], netlist=None, context=ctx)
    if len(ctx.get("patterns", [])) > 2:
        assert data["patterns_truncated"] is True
        assert data["patterns_total"] == len(ctx["patterns"])


def test_context_surfaces_analyst_waivers(demo_report):
    """A waiver changes every count, so the agent must be able to see it."""
    edited = apply_exclusions(demo_report, excluded_subtypes=["AU.TC"],
                              note="known TDR topology")
    data = investigate.run_tool(
        "report_context", {"section": "waivers"},
        fault_results=[], constraints=[], netlist=None,
        context=_context(edited))
    waivers = data["waivers"]
    assert "AU.TC" in waivers["excluded_subtypes"]
    assert waivers["removed_count"] > 0
    assert "AFTER that removal" in waivers["caveat"]


def test_context_without_an_analysis_is_an_error():
    data = investigate.run_tool(
        "report_context", {}, fault_results=[], constraints=[], netlist=None)
    assert "error" in data


def test_context_survives_the_out_of_process_round_trip(demo_report):
    evidence = investigate.export_evidence(
        demo_report.fault_results, demo_report.constraints,
        demo_report.netlist, context=_context(demo_report))
    assert "context" in evidence
    data = investigate.run_tool(
        "report_context", {}, fault_results=[], constraints=[], netlist=None,
        context=evidence["context"])
    assert "evidence" in data


# ---------------------------------------------------------------------------
# report_insufficient_evidence
# ---------------------------------------------------------------------------
def test_the_agent_can_declare_the_evidence_insufficient():
    data = investigate.run_tool(
        "report_insufficient_evidence",
        {"question": "Is u_foo on the scan chain?",
         "missing": "no netlist was loaded",
         "would_settle_it": "the gate-level netlist for this block"},
        fault_results=[], constraints=[], netlist=None)
    assert data["verdict"] == "insufficient_evidence"
    assert data["confidence"] == "insufficient"
    assert data["would_settle_it"]
    # It must read as a valid destination, not as a failure to try harder.
    assert "correct result" in data["acknowledged"]
    assert "Do not follow it with a speculative root cause" in \
        data["acknowledged"]


def test_declaring_insufficiency_needs_a_question():
    data = investigate.run_tool(
        "report_insufficient_evidence", {"question": "  "},
        fault_results=[], constraints=[], netlist=None)
    assert "error" in data


def test_both_new_tools_are_registered():
    for name in ("report_context", "report_insufficient_evidence"):
        assert name in investigate.TOOL_SPECS


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------
def test_the_prompt_states_that_not_knowing_is_an_acceptable_answer():
    assert "does not settle this" in SYSTEM_PROMPT
    assert "COMPLETE and ACCEPTABLE answer" in SYSTEM_PROMPT


def test_the_agentic_prompt_points_at_the_honesty_tools():
    assert "report_insufficient_evidence" in AGENTIC_SYSTEM_PROMPT
    assert "report_context" in AGENTIC_SYSTEM_PROMPT
    assert "verify_paths" in AGENTIC_SYSTEM_PROMPT


def test_the_correction_prompt_forbids_collateral_edits():
    assert "Preserve everything else" in CORRECTION_SYSTEM_PROMPT
    assert "do not soften unrelated conclusions" in CORRECTION_SYSTEM_PROMPT


def test_the_category_census_in_the_prompt_is_bounded(demo_report):
    payload = build_user_payload(demo_report, max_faults=10)
    listed = [line for line in payload.splitlines()
              if line.startswith("    ") and " | " in line]
    assert len(listed) <= MAX_TRIAGE_CATEGORIES
