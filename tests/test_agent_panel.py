"""Offscreen tests for the agent panel's grounded-evidence and verify features."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from urllib.parse import quote

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.gui.agent_panel import AgentPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel_with_report(qapp, sample_netlist_path, sample_faults_path,
                      sample_constraints_path):
    rep = run_analysis(sample_netlist_path, sample_faults_path,
                       sample_constraints_path)
    panel = AgentPanel()
    panel.set_report(rep, None)
    return panel, rep


def test_linkify_creates_fault_anchor(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    html = panel._to_html(f"Coverage lost at {fo} here.")
    assert 'href="fault:' in html
    assert quote(fo, safe="") in html


def test_anchor_click_emits_fault(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    captured = []
    panel.fault_referenced.connect(captured.append)
    panel._on_anchor_clicked(QUrl("fault:" + quote(fo, safe="")))
    assert captured == [fo]


def test_verify_flags_grounded_and_hallucinated(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    panel._last_response = f"Faults {fo} and top/made/up/XX are affected."
    panel.on_verify()
    trace = panel.trace_view.toPlainText()
    assert "VERIFICATION" in trace
    assert fo in trace                     # grounded, with ground-truth attrs
    assert "top/made/up/XX" in trace       # flagged as not-in-report


def test_ask_about_fault_prefills_chat(panel_with_report):
    panel, rep = panel_with_report
    fo = rep.fault_results[0].fault.fault_object
    panel.ask_about_fault(fo)
    assert fo in panel.chat_input.text()


def test_investigation_export_import(panel_with_report, qapp,
                                     sample_netlist_path, sample_faults_path,
                                     sample_constraints_path):
    panel, rep = panel_with_report
    panel._set_response("Diagnosis text about a fault.")
    panel._append_chat("You", "why is it lost?")
    panel._append_chat("Agent", "because of a constraint")
    panel.trace_view.setPlainText("=== VERIFICATION ===")

    data = panel.export_investigation()
    assert data["diagnosis"].startswith("Diagnosis text")
    assert [t["role"] for t in data["chat"]] == ["You", "Agent"]
    assert "VERIFICATION" in data["trace"]

    # Import into a fresh panel restores the transcript.
    fresh = AgentPanel()
    fresh.set_report(rep, None)
    fresh.import_investigation(data)
    assert "Diagnosis text" in fresh.response_view.toPlainText()
    chat_text = fresh.chat_view.toPlainText()
    assert "why is it lost?" in chat_text
    assert "because of a constraint" in chat_text
    assert "VERIFICATION" in fresh.trace_view.toPlainText()

    # Importing None clears everything.
    fresh.import_investigation(None)
    assert fresh.response_view.toPlainText().strip() == ""
    assert fresh.chat_view.toPlainText().strip() == ""

