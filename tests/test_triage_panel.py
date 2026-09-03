"""Offscreen tests for the triage and fix-plan panel."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from atpg_coverage_debug_agent.app import run_analysis
from atpg_coverage_debug_agent.gui.triage_panel import TriagePanel
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list

# Dotted subclasses and a clear hierarchy hotspot, so the panel has real
# triage to render rather than the sample data's bare classes.
FAULTS = "\n".join(
    [f"AU.TC 1 top/core/fscan/tdr_reg/u{i}/Y" for i in range(40)]
    + [f"UO.AAB 0 top/core/crypto/aes/u{i}/Y" for i in range(20)]
    + [f"DS 0 top/misc/blk/u{i}/Y" for i in range(60)]
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def report(tmp_path, sample_netlist_path):
    faults = tmp_path / "dotted.faults"
    faults.write_text(FAULTS, encoding="utf-8")
    return run_analysis(sample_netlist_path, str(faults), None)


@pytest.fixture
def panel(qapp, report):
    widget = TriagePanel()
    widget.set_report(report)
    return widget


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
def test_categories_table_lists_only_coverage_loss(panel, report):
    shown = {panel.category_table.item(r, 0).text()
             for r in range(panel.category_table.rowCount())}
    assert shown == {"AU.TC", "UO.AAB"}
    assert "DS" not in shown


def test_categories_show_counts_and_stuck_at_split(panel):
    rows = {panel.category_table.item(r, 0).text(): r
            for r in range(panel.category_table.rowCount())}
    row = rows["AU.TC"]
    assert panel.category_table.item(row, 1).text() == "40"
    # Every AU.TC fault here is stuck-at-1, so the split is maximally skewed.
    assert panel.category_table.item(row, 4).text() == "40"
    assert panel.category_table.item(row, 5).text() == "1.00"


def test_totals_line_states_these_are_fault_list_numbers(panel):
    text = panel.totals_label.text()
    assert "coverage loss" in text
    assert "not" in text and "test-coverage" in text


def test_selecting_a_category_shows_its_evidence(panel):
    panel.category_table.selectRow(0)
    text = panel.category_detail.toPlainText()
    assert "Scored verdict" in text
    assert "Worth acting on" in text


def test_first_category_is_selected_so_the_panel_is_never_blank(panel):
    assert panel.category_table.selectedItems()
    selected = panel.category_table.selectedItems()[0].text()
    assert selected in panel.category_detail.toPlainText()


def test_export_button_is_enabled_only_with_categories(panel, qapp):
    assert panel.export_categories_btn.isEnabled()
    panel.clear()
    assert not panel.export_categories_btn.isEnabled()


def test_export_button_asks_the_main_window_rather_than_writing_files(panel):
    """The panel owns no file IO; it only raises the request."""
    seen = []
    panel.export_categories_requested.connect(lambda: seen.append(True))
    panel.export_categories_btn.click()
    assert seen == [True]


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------
def test_cluster_tree_groups_samples_under_their_prefix(panel):
    assert panel.cluster_tree.topLevelItemCount() >= 1
    root = panel.cluster_tree.topLevelItem(0)
    assert root.childCount() >= 1
    cluster = root.child(0)
    assert cluster.text(0).startswith("top/core")
    assert cluster.childCount() >= 1


def test_cluster_samples_are_verbatim_fault_paths(panel, report):
    known = {f.fault_object for f in report.faults}
    root = panel.cluster_tree.topLevelItem(0)
    sample = root.child(0).child(0)
    assert sample.data(0, Qt.UserRole) in known


def test_double_clicking_a_sample_emits_the_fault(qapp, panel):
    seen = []
    panel.fault_referenced.connect(seen.append)

    root = panel.cluster_tree.topLevelItem(0)
    sample = root.child(0).child(0)
    panel._on_cluster_activated(sample, 0)
    assert seen == [sample.data(0, Qt.UserRole)]


def test_double_clicking_a_prefix_does_not_emit_a_fault(qapp, panel):
    seen = []
    panel.fault_referenced.connect(seen.append)

    root = panel.cluster_tree.topLevelItem(0)
    # A cluster prefix is not a fault, so it must not be treated as one.
    panel._on_cluster_activated(root.child(0), 0)
    assert seen == []


def test_copy_path_puts_the_selection_on_the_clipboard(qapp, panel):
    root = panel.cluster_tree.topLevelItem(0)
    cluster = root.child(0)
    panel.cluster_tree.setCurrentItem(cluster)
    panel._copy_selected_prefix()
    assert QApplication.clipboard().text() == cluster.data(0, Qt.UserRole)


# ---------------------------------------------------------------------------
# Fix plan
# ---------------------------------------------------------------------------
def test_fix_list_is_populated_and_ranked(panel, report):
    assert panel.fix_list.count() == len(report.recommendations)
    assert panel.fix_list.item(0).text().startswith("1. ")


def test_selected_fix_shows_rationale_evidence_and_commands(panel):
    panel.fix_list.setCurrentRow(0)
    text = panel.fix_detail.toPlainText()
    assert "Why:" in text
    assert "Evidence" in text
    assert "Commands" in text


def test_fix_detail_never_predicts_a_gain(panel):
    for row in range(panel.fix_list.count()):
        panel.fix_list.setCurrentRow(row)
        assert "will recover" not in panel.fix_detail.toPlainText().lower()


def test_copy_commands_is_enabled_only_when_there_are_commands(panel):
    for row in range(panel.fix_list.count()):
        panel.fix_list.setCurrentRow(row)
        rec = panel._current_recommendation()
        assert panel.copy_commands_btn.isEnabled() == bool(rec.fix.commands)


def test_copy_commands_copies_the_exact_command_block(qapp, panel):
    for row in range(panel.fix_list.count()):
        panel.fix_list.setCurrentRow(row)
        rec = panel._current_recommendation()
        if rec.fix.commands:
            panel._copy_commands()
            assert (QApplication.clipboard().text()
                    == "\n".join(rec.fix.commands))
            return
    pytest.fail("no recommendation carried commands")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_clear_resets_every_view(panel):
    panel.clear()
    assert panel.category_table.rowCount() == 0
    assert panel.cluster_tree.topLevelItemCount() == 0
    assert panel.fix_list.count() == 0
    assert not panel.copy_commands_btn.isEnabled()


def test_report_without_triage_is_handled(qapp):
    class _Bare:
        statistics = None
        selected_categories = None
        recommendations = None

    widget = TriagePanel()
    widget.set_report(_Bare())
    assert widget.category_table.rowCount() == 0
    assert "no coverage triage" in widget.totals_label.text()


def test_panel_survives_a_report_with_no_coverage_loss(qapp, tmp_path,
                                                       sample_netlist_path):
    faults = tmp_path / "clean.faults"
    faults.write_text("\n".join(f"DS 0 top/u/x{i}/Y" for i in range(10)),
                      encoding="utf-8")
    widget = TriagePanel()
    widget.set_report(run_analysis(sample_netlist_path, str(faults), None))

    assert widget.category_table.rowCount() == 0
    assert widget.fix_list.count() == 0
    assert "No fix proposals" in widget.fix_detail.toPlainText()


# ---------------------------------------------------------------------------
# Help text stays in step with the features
# ---------------------------------------------------------------------------
def test_help_documents_the_triage_tab_and_how_it_reaches_conclusions():
    from atpg_coverage_debug_agent.gui.main_window import _HELP_HTML

    for topic in ("Triage &amp; Fix Plan",
                  "How the triage reaches its conclusions",
                  "Where the loss is",
                  "Fix Plan",
                  "Hierarchy clustering",
                  "Scored verdicts",
                  "What is blocking the faults",
                  "Why aborted faults were hard to test",
                  "Honesty guardrails",
                  "What this analysis cannot do"):
        assert topic in _HELP_HTML, f"help does not cover {topic!r}"


def test_help_lists_every_agent_tool():
    from atpg_coverage_debug_agent.analysis.investigate import TOOL_SPECS
    from atpg_coverage_debug_agent.gui.main_window import _HELP_HTML

    # Regression tools are documented under Compare Report rather than in the
    # agentic tool table, so they are exempt.
    exempt = {"regression_summary", "list_regressed", "list_fixed",
              "list_changed"}
    for name in set(TOOL_SPECS) - exempt:
        assert name in _HELP_HTML, f"help does not mention tool {name!r}"


def test_help_states_the_key_honesty_caveats():
    from atpg_coverage_debug_agent.gui.main_window import _HELP_HTML

    # Claims the tool must never let a reader assume otherwise.
    assert "not</b> the ATPG tool's test-coverage number" in _HELP_HTML
    assert "never predicts a coverage gain" in _HELP_HTML
    assert "never a root\n      cause" in _HELP_HTML
    assert "estimates" in _HELP_HTML
