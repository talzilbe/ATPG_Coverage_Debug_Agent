"""The agent's tessent_collect_evidence tool: sample, approve, parse, fold in."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from atpg_coverage_debug_agent import mcp_server
from atpg_coverage_debug_agent.analysis import investigate
from atpg_coverage_debug_agent.analysis import tool_evidence as te
from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.launcher import agent_bridge as ab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = tuple(os.path.join(ROOT, "sample_data", n) for n in (
    "demo_netlist.v", "demo_faults.mtfi", "demo_constraints.do"))

TRANSCRIPT = """\
// Statistics Report
//   AU (atpg_untestable)                                 52
//     TC (tied_cells)                                    40
//   UO (unobserved)                                      32
//     AAB (atpg_abort)                                   32
//     test_coverage                                   61.20%
//  Fault analysis for /top/u_crypto/uc_m0 (4310693) output Y stuck at 0
//  Current fault classification = UO.AAB
//  Fault site sequential depth: Control_0 = 0, Control_1 = 0, Observe = 0.
//      The fault site was set to 1 successfully (data in parallel_pattern 0).
//    6 potential observation points were identified:
//      Fault was not observed successfully (ATPG status = abort).
"""


@pytest.fixture(scope="module")
def demo_state():
    if not all(os.path.isfile(p) for p in DEMO):
        pytest.skip("demo data not present")
    report = run_analysis(*DEMO)
    triage = investigate.serialize_triage(
        report.statistics, report.selected_categories, report.recommendations)
    return report, triage


def test_samples_are_spread_and_need_a_stuck_value(demo_state):
    report, triage = demo_state
    targets = te.collect_targets(report.fault_results, None, 3, triage)
    assert targets
    for subclass, picks in targets.items():
        assert 0 < len(picks) <= 3
        assert all(stuck in ("0", "1") for _, stuck in picks)
    only = te.collect_targets(report.fault_results, ["UO.AAB"], 50, triage)
    assert list(only) == ["UO.AAB"] and len(only["UO.AAB"]) <= te.MAX_SAMPLES


def test_the_script_is_read_only_and_survives_a_bad_path():
    script = te.build_collect_script(
        {"UO.AAB": [("/top/a/Y", "0"), ("/bad{path}", "1")]})
    assert "report_statistics -detailed_analysis" in script
    assert "catch {analyze_fault {/top/a/Y} -stuck_at 0}" in script
    assert "bad{path}" not in script
    assert "statistics" not in te.build_collect_script({}, statistics=False)


def test_the_transcript_becomes_evidence_and_a_summary(demo_state):
    _, triage = demo_state
    evidence = te.evidence_from_transcript(TRANSCRIPT)
    assert evidence.statistics[0].metrics["test_coverage"] == 61.2
    assert evidence.analyses[0].observation_points == 6
    summary = te.summarize_for_agent(evidence, triage, {})
    cat = summary["measured_categories"][0]
    assert cat["subclass"] == "UO.AAB"
    assert cat["measured_dominant"] == "reconvergent_complexity"
    assert cat["fixes_for_measured"]
    assert summary["evidence_source"] == "tool_report"


def test_an_empty_transcript_says_nothing_was_measured():
    evidence = te.evidence_from_transcript("SETUP> \n")
    assert not evidence.statistics and not evidence.analyses
    assert evidence.warnings


def test_merging_keeps_the_newest_sample_per_fault():
    a = te.evidence_from_transcript(TRANSCRIPT)
    b = te.evidence_from_transcript(TRANSCRIPT.replace("6 potential",
                                                       "0 potential"))
    merged = te.merge_evidence(a, b)
    assert len(merged.analyses) == 1
    assert merged.analyses[0].observation_points == 0


def _answer_when_asked(bridge, payload):
    def _run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reqs = ab.pending(bridge)
            if reqs:
                _answer_when_asked.request = reqs[0]
                ab.respond(bridge, reqs[0].id, payload)
                return
            time.sleep(0.02)
    threading.Thread(target=_run, daemon=True).start()


def test_the_mcp_tool_runs_one_approved_script_and_leaves_evidence(
        tmp_path, demo_state):
    report, triage = demo_state
    names = {t["name"] for t in mcp_server.build_tools_list()}
    assert "tessent_collect_evidence" in names
    bridge = ab.bridge_dir(str(tmp_path))
    _answer_when_asked(bridge, {"status": "completed", "ok": True,
                                "script_ran": "x", "transcript": TRANSCRIPT})
    state = {"work_dir": str(tmp_path), "tool_log_path": "",
             "faults": report.fault_results, "triage": triage, "context": {}}
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "tessent_collect_evidence",
                   "arguments": {"subclasses": "UO.AAB", "samples": 2}}},
        state)
    data = json.loads(resp["result"]["content"][0]["text"])
    assert data["status"] == "completed"
    assert data["requested_samples"] == {"UO.AAB": 2}
    assert data["measured_categories"][0]["measured_dominant"] == \
        "reconvergent_complexity"
    req = _answer_when_asked.request
    assert "analyze_fault" in req.script and "report_statistics" in req.script
    saved = json.load(open(tmp_path / ab.LIVE_EVIDENCE_FILE))
    assert saved["analyses"] and saved["statistics"]


def test_a_rejected_collection_is_passed_back_unchanged(tmp_path, demo_state):
    report, triage = demo_state
    bridge = ab.bridge_dir(str(tmp_path))
    _answer_when_asked(bridge, {"status": "rejected", "script_ran": "",
                                "message": "no"})
    state = {"work_dir": str(tmp_path), "faults": report.fault_results,
             "triage": triage}
    data = mcp_server.collect_live_evidence({}, state)
    assert data["status"] == "rejected"
    assert not (tmp_path / ab.LIVE_EVIDENCE_FILE).exists()


def test_the_main_window_folds_collected_evidence_in(qapp_window, demo_state):
    window = qapp_window
    report, _ = demo_state
    window._base_report = report
    window._apply_report(report)
    data = te.evidence_from_transcript(TRANSCRIPT).as_dict()
    window._on_agent_tool_evidence(data)
    assert window._report.tool_evidence.analyses
    assert "measured by Tessent" in window.dashboard.text()
    window._on_agent_tool_evidence(data)
    assert len(window._report.tool_evidence.analyses) == 1


@pytest.fixture
def qapp_window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from atpg_coverage_debug_agent.config import settings as settings_mod
    from atpg_coverage_debug_agent.gui import main_window as mw
    from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel
    monkeypatch.setattr(settings_mod, "_DEFAULT_CONFIG_FILE",
                        tmp_path / "settings.json")
    monkeypatch.setattr(AgentPanel, "refresh_models", lambda self: None)
    win = mw.MainWindow()
    yield win
    win.agent_panel.shutdown()
    win.close()
