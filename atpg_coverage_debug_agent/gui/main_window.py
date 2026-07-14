"""Main application window for the PySide6 GUI — v2 with Skills system."""
from __future__ import annotations

import csv as csv_mod
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QStatusBar, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from ..app import AnalysisInputs, PartitionInputs, _design_name
from ..analysis import investigate, regression, report_edit
from ..config.settings import AppSettings
from ..models import AnalysisReport, FaultAnalysisResult
from ..reporting.csv_report import write_csv
from ..reporting.html_report import build_html_report
from ..reporting.markdown_report import write_markdown
from ..reporting.session_report import load_report, save_report
from ..skills.manager import SkillManager
from .details_panel import DetailsPanel
from .skills_panel import SkillsPanel
from .agent_panel import AgentPanel
from .custom_skills_panel import CustomSkillsPanel
from .workers import start_worker, start_multi_worker

logger = logging.getLogger(__name__)

_TABLE_HEADERS = [
    "Fault Object", "Class", "Mapped", "Confidence", "Instance", "Cell",
    "Fan-in", "Fan-out", "Ctrl", "Obsv", "Constraint", "Scan", "Root Cause",
]


_HELP_HTML = """
<html><head><style>
  body { font-family: 'Segoe UI', sans-serif; color: #1f2933; line-height: 1.5;
         padding: 8px 18px; }
  h1 { color: #0b5394; font-size: 22px; margin: 4px 0 2px; }
  h2 { color: #0b5394; font-size: 17px; margin: 20px 0 4px;
       border-bottom: 2px solid #d0e2f2; padding-bottom: 3px; }
  h3 { color: #1a5276; font-size: 14px; margin: 14px 0 3px; }
  p, li { font-size: 13px; }
  code { background: #eef2f7; padding: 1px 5px; border-radius: 3px;
         font-family: Consolas, monospace; font-size: 12px; }
  b.k { color: #0b5394; }
  .tip { background: #e8f5e9; border-left: 4px solid #4CAF50;
         padding: 6px 12px; margin: 8px 0; }
  .warn { background: #fff8e1; border-left: 4px solid #FFC107;
          padding: 6px 12px; margin: 8px 0; }
  .step { margin: 2px 0; }
  table { border-collapse: collapse; margin: 6px 0; }
  td, th { border: 1px solid #cfd8e3; padding: 4px 9px; font-size: 12px;
           text-align: left; vertical-align: top; }
  th { background: #eef2f7; }
</style></head><body>

<h1>ATPG Coverage-Loss Debug Agent &mdash; Help</h1>
<p>This tool analyses <b>structural</b> (non-simulation) ATPG coverage loss. It
maps untestable / unobservable / uncontrollable faults onto your gate-level
netlist, root-causes them, groups repeated patterns, and lets an AI agent
explain the results. Everything below is organised in the order you would
normally use it.</p>

<div class="tip"><b>Quick start:</b> 1) pick a <b>Netlist</b> and a
<b>Fault list</b> (constraints optional) &rarr; 2) click <b>Analyze</b> &rarr;
3) read the <b>Summary</b> and <b>Coverage Loss Table</b> &rarr; 4) optionally
run the <b>AI Debug Agent</b> for an explanation.</div>

<h2>1. Input files (top of the window)</h2>
<table>
<tr><th>Field</th><th>What to load</th></tr>
<tr><td><b class="k">Netlist (.v / .v.gz)</b></td>
    <td>Gate-level Verilog structural netlist. Used to trace fan-in/fan-out,
    map faults to instances, and find scan boundaries.</td></tr>
<tr><td><b class="k">Fault list (.mtfi / .mtfi.gz / flat)</b></td>
    <td>Tessent MTFI fault list or a flat <code>&lt;class&gt; &lt;value&gt;
    &lt;path&gt;</code> list. Dotted subtypes (e.g. <code>AU.NOFAULTS</code>,
    <code>AU.TC</code>) are preserved.</td></tr>
<tr><td><b class="k">Constraints (optional .do)</b></td>
    <td>Tessent constraint / dofile commands (force, disable, clock, reset,
    tie…). Used to attribute constraint-induced coverage loss.</td></tr>
<tr><td><b class="k">Output dir</b></td>
    <td>Default folder for exported Markdown / CSV / JSON reports.</td></tr>
</table>

<h2>2. Action buttons</h2>
<table>
<tr><th>Button</th><th>Use</th></tr>
<tr><td><b class="k">Analyze</b></td><td>Parse the inputs and build the report.
    Runs in the background; watch the progress bar and status bar.</td></tr>
<tr><td><b class="k">Cancel</b></td><td>Abort a running analysis.</td></tr>
<tr><td><b class="k">Export Markdown / Export CSV</b></td>
    <td>Save the report as Markdown, or the coverage-loss table as CSV.</td></tr>
<tr><td><b class="k">Save Report / Load Report</b></td>
    <td>Save the full analysis (including the AI investigation) to JSON and
    reload it later &mdash; no need to re-run Analyze.</td></tr>
<tr><td><b class="k">Compare Report</b></td>
    <td>Load a previous (baseline) JSON report and diff it against the current
    one: <b>regressed</b> (new loss), <b>fixed</b>, and <b>changed</b> faults.
    You can then ask the AI agent &ldquo;what changed vs the baseline?&rdquo;</td></tr>
<tr><td><b class="k">Edit Report</b></td>
    <td>Waive faults and recompute coverage &mdash; see section 4.</td></tr>
<tr><td><b class="k">Clear</b></td><td>Reset the views to start fresh.</td></tr>
</table>

<h2>3. Result tabs</h2>
<h3>Summary</h3>
<p>A full HTML report: coverage metric, fault-class / subtype breakdown, top
root causes, module and instance hotspots, and any analyst note. Click
<b>Open Report in Browser</b> for the full-fidelity version (and a shareable
local link).</p>

<h3>Coverage Loss Table</h3>
<p>One row per coverage-loss fault with its class, mapped instance, mapping
confidence, fan-in/out sizes, controllability / observability / constraint /
scan-boundary flags, and the diagnosed root cause.</p>
<ul>
  <li class="step"><b>Filter</b> by substring, fault class, or mapping
      confidence; <b>Export Filtered CSV</b> saves just the visible rows.</li>
  <li class="step"><b>Click a row</b> to see full per-fault evidence in the
      Details panel on the right.</li>
  <li class="step"><b>Right-click a row</b> to <i>Ask the AI agent about this
      fault</i> or <i>Exclude selected fault(s)</i> from the report.</li>
</ul>

<h3>Logs / Warnings</h3>
<p>Parser and skill warnings (unrecognised lines, unresolved mappings, etc.).
Check here first if a result looks incomplete.</p>

<h3>Skills</h3>
<p>Toggle and configure the deterministic analysis skills (coverage hotspots,
constraint impact, fault-cone summary, scan-boundary, DFT/ATPG debug…). Each
card has an enable checkbox and tunable parameters; changes persist.</p>

<h3>Custom Skills</h3>
<p>Load your own Python skills from a directory, or write one in the built-in
editor from the provided template, to add project-specific detectors. Loaded
custom skills appear on the Skills tab and become AI agent tools.</p>

<h2>4. Edit Report &mdash; waiving faults &amp; recomputing coverage</h2>
<p>Open with <b>Edit Report</b>. Excluded faults are removed from the totals so
the coverage metric rises, while the report <b>layout stays identical</b>.
Edits are reversible (they apply to a pristine base report) and are saved with
the JSON report. You can waive at four levels:</p>
<ul>
  <li class="step"><b>Whole classes</b> &mdash; all <code>AU</code> /
      <code>UO</code> / <code>UC</code> faults.</li>
  <li class="step"><b>Specific subtypes</b> &mdash; tick e.g.
      <code>AU.NOFAULTS</code> or <code>AU.TC</code> (each shown with its fault
      count).</li>
  <li class="step"><b>Individual faults by path</b> &mdash; type object paths,
      one per line.</li>
  <li class="step"><b>Table selection</b> &mdash; right-click selected rows in
      the Coverage Loss Table &rarr; <i>Exclude selected fault(s)</i>.</li>
</ul>
<p>Add an <b>analyst note</b> to record <i>why</i> the waiver is legitimate; it
appears on the report.</p>

<h2>5. AI Debug Agent &mdash; usage guide</h2>
<p>The agent explains coverage loss using an evidence-driven ATPG/DFT prompt.
It reads only the deterministic report, so it cannot invent faults. Run an
Analyze first, then open the <b>AI Debug Agent</b> tab.</p>

<h3>Step 1 &mdash; choose a backend (LLM Backend box)</h3>
<ul>
  <li class="step"><b>GitHub Copilot CLI (local subprocess)</b> &mdash; the
      default. Uses the bundled <code>copilot</code> CLI; data stays in the
      CLI's authenticated channel. Set the <b>CLI model</b>
      (<code>auto</code> lets Copilot choose).</li>
  <li class="step"><b>OpenAI-compatible HTTP endpoint</b> &mdash; point at an
      internal endpoint with a Base URL, Model id, and API key (kept in memory
      only, never written to disk).</li>
</ul>

<h3>Step 2 &mdash; authenticate (CLI backend only)</h3>
<p>On the agent's <b>Authentication</b> sub-tab, use <b>either</b>:</p>
<ul>
  <li class="step"><b>Option A &mdash; GitHub token:</b> paste a fine-grained
      PAT with the <i>Copilot Requests</i> permission (or an OAuth token).
      Classic <code>ghp_</code> tokens are not supported.</li>
  <li class="step"><b>Option B &mdash; device login:</b> click <i>Sign in with
      device code</i>, open the shown URL and enter the code.</li>
</ul>
<div class="warn">On a headless host with no keychain, the browser login can
succeed but fail to <b>save</b> the token &mdash; use Option A there. Use
<b>Check authentication</b> to confirm you are signed in.</div>

<h3>Step 3 &mdash; pick a mode</h3>
<ul>
  <li class="step"><b>Standard</b> (agentic off): the enabled skills run
      locally and their findings are folded into a single prompt.</li>
  <li class="step"><b>Agentic mode</b>: the model itself decides which
      investigative tools to call (<code>list_faults</code>,
      <code>get_fault_detail</code>, <code>why_blocked</code>,
      <code>list_constraints</code>, <code>trace_path</code>) and iterates. For
      the CLI backend this is driven through a local <b>MCP</b> server
      (&ldquo;Agentic tools (MCP)&rdquo; checkbox). The HTTP backend needs an
      endpoint that supports tool/function calling.</li>
</ul>

<h3>Step 4 &mdash; run &amp; review</h3>
<table>
<tr><th>Button</th><th>Use</th></tr>
<tr><td><b class="k">Run AI Debug Agent</b></td><td>Generate the A&ndash;F
    diagnosis. Output streams into the Agent Response pane; fault ids are
    clickable and focus the row in the table.</td></tr>
<tr><td><b class="k">Build Prompt Only</b></td><td>Preview exactly what would be
    sent to the LLM, without calling it.</td></tr>
<tr><td><b class="k">Verify</b></td><td>Cross-check the answer against the
    report &mdash; confirms every referenced fault exists and flags invented
    paths.</td></tr>
<tr><td><b class="k">Suggest Fixes</b></td><td>Deterministically rank faults by
    impact and propose concrete DFT fixes (observation/control points,
    constraint relaxation, scan insertion). No LLM used.</td></tr>
<tr><td>Copy / Save Prompt &amp; Response</td><td>Export the prompt or the
    agent's answer.</td></tr>
</table>

<h3>Step 5 &mdash; follow-up chat</h3>
<p>After a run, use <b>Follow-up Chat</b> to ask questions about the diagnosis;
the conversation keeps the full analysis context (e.g. &ldquo;which module
contributes the most loss?&rdquo;, &ldquo;how would a control point on X
help?&rdquo;). <b>Max tokens</b>, <b>Temperature</b>, and <b>Max faults in
prompt</b> tune size and determinism (temperature&nbsp;0 is most repeatable).</p>

<div class="tip"><b>Recommended flow:</b> Analyze &rarr; skim Summary &rarr;
run the agent in Agentic mode &rarr; <b>Verify</b> the answer &rarr; ask
follow-ups &rarr; <b>Suggest Fixes</b> &rarr; waive legitimate faults via
<b>Edit Report</b> &rarr; <b>Save Report</b>.</div>

</body></html>
"""


class _QuietHTTPRequestHandler(SimpleHTTPRequestHandler):
    """A ``SimpleHTTPRequestHandler`` that logs to the logger, not stderr."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("report-server: " + format, *args)


class _FilePicker(QWidget):
    def __init__(self, label: str, *, directory: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._directory = directory
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label), 0)
        self.edit = QLineEdit(self)
        layout.addWidget(self.edit, 1)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse, 0)

    def _browse(self) -> None:
        if self._directory:
            path = QFileDialog.getExistingDirectory(self, "Select directory")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select file")
        if path:
            self.edit.setText(path)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, value: str) -> None:
        self.edit.setText(value)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ATPG Coverage-Loss Debug Agent  v2")
        self.resize(1400, 900)

        self._report: Optional[AnalysisReport] = None
        self._base_report: Optional[AnalysisReport] = None
        self._results: List[FaultAnalysisResult] = []
        self._report_html: Optional[str] = None
        self._thread: Optional[QThread] = None
        self._worker = None

        # Multi-partition state: a queue of partitions to analyze, and the
        # analyzed partitions (each keeps its own pristine base + current
        # report) plus the index of the one currently shown in every tab.
        self._queued: List[PartitionInputs] = []
        self._partitions: List[dict] = []
        self._active_idx: int = -1

        # Background HTTP server used to publish a shareable report link.
        self._report_server: Optional[ThreadingHTTPServer] = None
        self._report_server_thread: Optional[threading.Thread] = None
        self._report_server_dir: Optional[str] = None

        self._settings = AppSettings.load()
        self._skill_manager = SkillManager()
        if self._settings.skills:
            self._skill_manager.from_config(self._settings.skills)

        self._build_ui()
        self._build_menu()
        self._restore_paths()

    def _restore_paths(self) -> None:
        s = self._settings
        if s.last_netlist:
            self.netlist_picker.set_path(s.last_netlist)
        if s.last_faults:
            self.faults_picker.set_path(s.last_faults)
        if s.last_constraints:
            self.constraints_picker.set_path(s.last_constraints)
        if s.last_output_dir:
            self.outdir_picker.set_path(s.last_output_dir)
        if s.filter_text:
            self.filter_text.setText(s.filter_text)
        if s.class_filter:
            idx = self.class_filter.findText(s.class_filter)
            if idx >= 0:
                self.class_filter.setCurrentIndex(idx)
        if s.conf_filter:
            idx = self.conf_filter.findText(s.conf_filter)
            if idx >= 0:
                self.conf_filter.setCurrentIndex(idx)
        if s.agent:
            self.agent_panel.import_settings(s.agent)
        if s.custom_skills_dir:
            self.custom_skills_panel.set_custom_dir(s.custom_skills_dir)

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        self.netlist_picker = _FilePicker("Netlist (.v / .v.gz):")
        self.faults_picker = _FilePicker("Fault list (.mtfi / .mtfi.gz / flat):")
        self.constraints_picker = _FilePicker("Constraints (optional .do):")
        self.outdir_picker = _FilePicker("Output dir:", directory=True)
        for picker in (self.netlist_picker, self.faults_picker,
                       self.constraints_picker, self.outdir_picker):
            outer.addWidget(picker)

        # --- Partition queue (analyze several partitions together) ---
        part_bar = QHBoxLayout()
        part_bar.addWidget(QLabel("Partitions:"))
        self.partition_list = QListWidget()
        self.partition_list.setMaximumHeight(78)
        self.partition_list.setToolTip(
            "Partitions queued for analysis. Set the netlist / fault list / "
            "constraints above, click 'Add Partition', then repeat for each "
            "partition. 'Analyze' runs them all; leave empty to analyze the "
            "single file set above.")
        part_bar.addWidget(self.partition_list, 1)
        part_btns = QVBoxLayout()
        self.add_partition_btn = QPushButton("Add Partition")
        self.add_partition_btn.setToolTip(
            "Queue the current netlist / fault list / constraints as a named "
            "partition.")
        self.add_partition_btn.clicked.connect(self.on_add_partition)
        self.remove_partition_btn = QPushButton("Remove")
        self.remove_partition_btn.setToolTip(
            "Remove the selected partition from the queue.")
        self.remove_partition_btn.clicked.connect(self.on_remove_partition)
        self.clear_partitions_btn = QPushButton("Clear Queue")
        self.clear_partitions_btn.clicked.connect(self.on_clear_partitions)
        for b in (self.add_partition_btn, self.remove_partition_btn,
                  self.clear_partitions_btn):
            part_btns.addWidget(b)
        part_btns.addStretch(1)
        part_bar.addLayout(part_btns)
        outer.addLayout(part_bar)

        btn_row = QHBoxLayout()
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.clicked.connect(self.on_analyze)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.cancel_btn.setEnabled(False)
        self.md_btn = QPushButton("Export Markdown")
        self.md_btn.clicked.connect(self.on_export_md)
        self.csv_btn = QPushButton("Export CSV")
        self.csv_btn.clicked.connect(self.on_export_csv)
        self.save_report_btn = QPushButton("Save Report")
        self.save_report_btn.setToolTip(
            "Save the full analysis to a JSON file you can reload later "
            "without re-running Analyze.")
        self.save_report_btn.clicked.connect(self.on_save_report)
        self.load_report_btn = QPushButton("Load Report")
        self.load_report_btn.setToolTip(
            "Load a previously saved report and work on it (tables + AI agent) "
            "without re-analyzing.")
        self.load_report_btn.clicked.connect(self.on_load_report)
        self.compare_btn = QPushButton("Compare Report")
        self.compare_btn.setToolTip(
            "Load a baseline report (a previous run) and diff it against the "
            "current one — regressed / fixed / changed faults — then ask the AI "
            "agent what changed.")
        self.compare_btn.clicked.connect(self.on_compare_report)
        self.edit_btn = QPushButton("Edit Report")
        self.edit_btn.setToolTip(
            "Waive whole classes (AU/UO/UC), specific subtypes (e.g. "
            "AU.NOFAULTS), or individual faults; coverage recomputes and the "
            "layout is unchanged. Reversible and saved with the report.")
        self.edit_btn.clicked.connect(self.on_edit_report)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.on_clear)
        for b in (self.analyze_btn, self.cancel_btn, self.md_btn,
                  self.csv_btn, self.save_report_btn, self.load_report_btn,
                  self.compare_btn, self.edit_btn, self.clear_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)
        self._set_export_enabled(False)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        # --- Active-partition selector (shown after a multi-partition run) ---
        sel_row = QHBoxLayout()
        self.partition_selector_label = QLabel("Active partition:")
        self.partition_selector = QComboBox()
        self.partition_selector.setToolTip(
            "Switch the partition shown in every tab below (Summary, table, "
            "AI agent, Edit Report). Each partition keeps its own report and "
            "edits.")
        self.partition_selector.currentIndexChanged.connect(
            self._on_partition_selected)
        sel_row.addWidget(self.partition_selector_label)
        sel_row.addWidget(self.partition_selector, 1)
        sel_row.addStretch(1)
        self.partition_selector_label.setVisible(False)
        self.partition_selector.setVisible(False)
        outer.addLayout(sel_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_summary_tab(), "Summary")
        self.tabs.addTab(self._build_table_tab(), "Coverage Loss Table")
        # The "Repeated Patterns" tab is intentionally not shown; the backing
        # widget is still built so the populate/reset logic keeps working.
        self._build_patterns_tab()
        self.tabs.addTab(self._build_logs_tab(), "Logs / Warnings")

        self.skills_panel = SkillsPanel(self._skill_manager)
        self.skills_panel.settings_changed.connect(self._save_settings)
        self.tabs.addTab(self.skills_panel, "Skills")

        self.custom_skills_panel = CustomSkillsPanel(self._skill_manager)
        self.custom_skills_panel.skills_loaded.connect(self._on_custom_skills_loaded)
        self.tabs.addTab(self.custom_skills_panel, "Custom Skills")

        self.agent_panel = AgentPanel()
        self.agent_panel.config_changed.connect(self._save_settings)
        self.agent_panel.fault_referenced.connect(self._focus_fault_in_table)
        self.tabs.addTab(self.agent_panel, "AI Debug Agent")

        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready — load files and click Analyze.")

    def _build_summary_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        bar.addStretch(1)
        self.open_browser_btn = QPushButton("Open Report in Browser")
        self.open_browser_btn.setToolTip(
            "Render the full-fidelity HTML report in your web browser")
        self.open_browser_btn.clicked.connect(self.on_open_report_in_browser)
        self.open_browser_btn.setEnabled(False)
        bar.addWidget(self.open_browser_btn)
        layout.addLayout(bar)

        self.summary_view = QTextBrowser()
        self.summary_view.setOpenExternalLinks(True)
        layout.addWidget(self.summary_view, 1)
        self._set_summary_html(
            "<body style='font-family: Segoe UI, sans-serif; padding: 40px; "
            "color: #6c757d;'><h2>ATPG Coverage Debug Report</h2>"
            "<p>Load a netlist, fault list and (optionally) constraints, then "
            "click <b>Analyze</b> to generate the report.</p></body>")
        return widget

    def _set_summary_html(self, html: str) -> None:
        self.summary_view.setHtml(html)

    def _clear_summary(self) -> None:
        self._set_summary_html(
            "<body style='font-family: Segoe UI, sans-serif; padding: 40px; "
            "color: #6c757d;'><p>Cleared. Run an analysis to generate a new "
            "report.</p></body>")


    def _build_table_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("Filter:"))
        self.filter_text = QLineEdit()
        self.filter_text.setPlaceholderText("substring on object/instance/root-cause…")
        self.filter_text.textChanged.connect(self._apply_filter)
        filt.addWidget(self.filter_text, 1)
        filt.addWidget(QLabel("Class:"))
        self.class_filter = QComboBox()
        self.class_filter.addItems(["all", "AU", "UO", "UC"])
        self.class_filter.currentTextChanged.connect(self._apply_filter)
        filt.addWidget(self.class_filter)
        filt.addWidget(QLabel("Confidence:"))
        self.conf_filter = QComboBox()
        self.conf_filter.addItems(["all", "high", "medium", "low", "unresolved"])
        self.conf_filter.currentTextChanged.connect(self._apply_filter)
        filt.addWidget(self.conf_filter)
        export_filtered_btn = QPushButton("Export Filtered CSV")
        export_filtered_btn.clicked.connect(self.on_export_filtered_csv)
        filt.addWidget(export_filtered_btn)
        layout.addLayout(filt)

        splitter = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, len(_TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        splitter.addWidget(self.table)
        self.details = DetailsPanel()
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        return widget

    def _build_patterns_tab(self) -> QWidget:
        self.patterns_table = QTableWidget(0, 4)
        self.patterns_table.setHorizontalHeaderLabels(
            ["Kind", "Key", "Count", "Sample faults"])
        self.patterns_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.patterns_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return self.patterns_table

    def _build_logs_tab(self) -> QWidget:
        self.logs_view = QPlainTextEdit()
        self.logs_view.setReadOnly(True)
        return self.logs_view

    def _show_help(self) -> None:
        """Open the user guide in a scrollable dialog."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Help — User Guide")
        dlg.resize(820, 720)
        v = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setHtml(_HELP_HTML)
        view.moveCursor(QTextCursor.Start)
        v.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        v.addWidget(buttons)
        dlg.exec()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        export_md = QAction("Export Markdown Report…", self)
        export_md.triggered.connect(self.on_export_md)
        file_menu.addAction(export_md)
        export_csv = QAction("Export CSV…", self)
        export_csv.triggered.connect(self.on_export_csv)
        file_menu.addAction(export_csv)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        skills_menu = self.menuBar().addMenu("&Skills")
        enable_all = QAction("Enable All Skills", self)
        enable_all.triggered.connect(self._on_enable_all_skills)
        skills_menu.addAction(enable_all)
        disable_all = QAction("Disable All Skills", self)
        disable_all.triggered.connect(self._on_disable_all_skills)
        skills_menu.addAction(disable_all)
        skills_menu.addSeparator()
        reset_defaults = QAction("Reset Skill Defaults", self)
        reset_defaults.triggered.connect(self._on_reset_skill_defaults)
        skills_menu.addAction(reset_defaults)

        help_menu = self.menuBar().addMenu("&Help")
        user_guide = QAction("User Guide…", self)
        user_guide.triggered.connect(self._show_help)
        help_menu.addAction(user_guide)

    def _on_enable_all_skills(self) -> None:
        self._skill_manager.enable_all()
        self.skills_panel.settings_pane._refresh_all()
        self._save_settings()
        self.statusBar().showMessage("All skills enabled.")

    def _on_disable_all_skills(self) -> None:
        self._skill_manager.disable_all()
        self.skills_panel.settings_pane._refresh_all()
        self._save_settings()
        self.statusBar().showMessage("All skills disabled.")

    def _on_reset_skill_defaults(self) -> None:
        self._skill_manager.reset_defaults()
        self.skills_panel.settings_pane._refresh_all()
        self._save_settings()
        self.statusBar().showMessage("Skill defaults restored.")

    def _on_custom_skills_loaded(self) -> None:
        """Rebuild the Skills tab cards when custom skills are added."""
        self.skills_panel.settings_pane.rebuild()
        self._settings.custom_skills_dir = self.custom_skills_panel.custom_dir()
        self._save_settings()
        self.statusBar().showMessage("Custom skills loaded — see the Skills tab.")

    def on_analyze(self) -> None:
        if self._queued:
            self._save_settings()
            self._start_multi_analysis(list(self._queued))
            return

        netlist = self.netlist_picker.path()
        faults = self.faults_picker.path()
        constraints = self.constraints_picker.path() or None

        if not netlist or not os.path.isfile(netlist):
            self._error("Please select a valid netlist file.")
            return
        if not faults or not os.path.isfile(faults):
            self._error("Please select a valid fault-list file.")
            return
        if constraints and not os.path.isfile(constraints):
            self._error("The constraint path is set but the file does not exist.")
            return
        if not constraints:
            QMessageBox.warning(self, "No constraints",
                "No constraint file selected. Constraint-related diagnoses will be disabled.")

        self._save_settings()
        inputs = AnalysisInputs(netlist, faults, constraints)
        self._start_analysis(inputs)

    def _unique_partition_name(self, base: str) -> str:
        """Return *base* made unique against already-queued partition names."""
        existing = {p.name for p in self._queued}
        if base not in existing:
            return base
        i = 2
        while f"{base}_{i}" in existing:
            i += 1
        return f"{base}_{i}"

    def on_add_partition(self) -> None:
        netlist = self.netlist_picker.path()
        faults = self.faults_picker.path()
        constraints = self.constraints_picker.path() or None
        if not netlist or not os.path.isfile(netlist):
            self._error("Select a valid netlist file before adding a partition.")
            return
        if not faults or not os.path.isfile(faults):
            self._error("Select a valid fault-list file before adding a partition.")
            return
        if constraints and not os.path.isfile(constraints):
            self._error("The constraint path is set but the file does not exist.")
            return
        base = _design_name(netlist) or f"partition_{len(self._queued) + 1}"
        name = self._unique_partition_name(base)
        self._queued.append(
            PartitionInputs(name, AnalysisInputs(netlist, faults, constraints)))
        self.partition_list.addItem(
            f"{name}  —  {os.path.basename(netlist)} / {os.path.basename(faults)}")
        self.statusBar().showMessage(
            f"Queued partition '{name}'. Set the next partition's files and "
            f"click 'Add Partition' again, or click 'Analyze' to run all "
            f"{len(self._queued)}.")

    def on_remove_partition(self) -> None:
        row = self.partition_list.currentRow()
        if row < 0:
            return
        self.partition_list.takeItem(row)
        del self._queued[row]

    def on_clear_partitions(self) -> None:
        self._queued = []
        self.partition_list.clear()
        self.statusBar().showMessage("Partition queue cleared.")

    def _start_analysis(self, inputs: AnalysisInputs) -> None:
        self.analyze_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._set_export_enabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage("Starting analysis…")

        self._thread, self._worker = start_worker(inputs, self._skill_manager)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _start_multi_analysis(self, partitions) -> None:
        self.analyze_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._set_export_enabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage(
            f"Analyzing {len(partitions)} partition(s)…")

        self._thread, self._worker = start_multi_worker(
            partitions, self._skill_manager)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_multi_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def on_cancel(self) -> None:
        if self._thread and self._thread.isRunning():
            self._thread.requestInterruption()
            self.statusBar().showMessage("Cancellation requested…")
            self.cancel_btn.setEnabled(False)

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.statusBar().showMessage(msg)

    def _on_finished(self, report: AnalysisReport) -> None:
        self.progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._reset_partitions()
        self._base_report = report
        self._apply_report(report)
        n_skills = len(report.skill_results) if report.skill_results else 0
        self.statusBar().showMessage(
            f"Done. {report.summary.coverage_loss_count} coverage-loss faults. "
            f"{n_skills} skill(s) ran.")

    def _on_multi_finished(self, results) -> None:
        self.progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._partitions = [
            {"name": name, "report": rep, "base_report": rep}
            for name, rep in results
        ]
        self._active_idx = -1
        self._populate_partition_selector()
        if self._partitions:
            self._set_active_partition(0)
        total_loss = sum(
            rep.summary.coverage_loss_count for _, rep in results)
        self.statusBar().showMessage(
            f"Done. {len(results)} partition(s), {total_loss} total "
            f"coverage-loss faults. Use 'Active partition' to switch views.")

    def _reset_partitions(self) -> None:
        """Clear analyzed-partition state and hide the selector (single run)."""
        self._partitions = []
        self._active_idx = -1
        self.partition_selector.blockSignals(True)
        self.partition_selector.clear()
        self.partition_selector.blockSignals(False)
        self.partition_selector.setVisible(False)
        self.partition_selector_label.setVisible(False)

    def _populate_partition_selector(self) -> None:
        self.partition_selector.blockSignals(True)
        self.partition_selector.clear()
        for p in self._partitions:
            self.partition_selector.addItem(
                f"{p['name']}  "
                f"({p['report'].summary.coverage_loss_count} loss)")
        self.partition_selector.blockSignals(False)
        show = len(self._partitions) >= 1
        self.partition_selector.setVisible(show)
        self.partition_selector_label.setVisible(show)

    def _set_active_partition(self, idx: int) -> None:
        if not (0 <= idx < len(self._partitions)):
            return
        self._active_idx = idx
        self.partition_selector.blockSignals(True)
        self.partition_selector.setCurrentIndex(idx)
        self.partition_selector.blockSignals(False)
        p = self._partitions[idx]
        self._base_report = p["base_report"]
        self._apply_report(p["report"])

    def _on_partition_selected(self, idx: int) -> None:
        self._set_active_partition(idx)

    def _apply_report(self, report: AnalysisReport) -> None:
        """Populate all views from *report* (shared by Analyze and Load)."""
        self._report = report
        self._results = report.fault_results
        # Keep the active partition's stored report in sync so edits/waivers
        # survive switching between partitions.
        if 0 <= self._active_idx < len(self._partitions):
            p = self._partitions[self._active_idx]
            p["report"] = report
            self.partition_selector.blockSignals(True)
            self.partition_selector.setItemText(
                self._active_idx,
                f"{p['name']}  ({report.summary.coverage_loss_count} loss)")
            self.partition_selector.blockSignals(False)
        self._set_export_enabled(True)
        self._populate(report)
        if report.skill_results:
            self.skills_panel.show_results(report.skill_results)
        self.agent_panel.set_report(report, self._skill_manager)
        # Restore a saved agent investigation (or clear stale agent output).
        self.agent_panel.import_investigation(getattr(report, "investigation",
                                                      None))

    def _on_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._error(f"Analysis failed:\n{message}")
        self.statusBar().showMessage("Analysis failed.")

    def _cleanup_thread(self) -> None:
        self._thread = None
        self._worker = None

    def _populate(self, report: AnalysisReport) -> None:
        self._populate_summary(report)
        self._populate_table(report)
        self._populate_patterns(report)
        self._populate_logs(report)

    def _populate_summary(self, report: AnalysisReport) -> None:
        # Prefer the current file pickers; fall back to source metadata stored
        # in the report (so a loaded report still shows its cover header).
        sources = getattr(report, "sources", None) or {}
        netlist = self.netlist_picker.path() or sources.get("netlist") or ""
        faults = self.faults_picker.path() or sources.get("faults") or ""
        constraints = (self.constraints_picker.path()
                       or sources.get("constraints") or "")
        design = None
        if netlist:
            base = os.path.basename(netlist)
            for ext in (".v.gz", ".gz", ".v"):
                if base.endswith(ext):
                    base = base[: -len(ext)]
                    break
            design = base or None
        if not design:
            design = sources.get("design")
        edits = getattr(report, "edits", None) or {}
        note_parts = []
        banner = report_edit.edit_banner(edits)
        if banner:
            note_parts.append(f"Edits applied — {banner}.")
        if edits.get("note"):
            note_parts.append(edits["note"])
        analyst_note = "\n".join(note_parts) if note_parts else None
        try:
            html = build_html_report(
                report,
                design_name=design,
                netlist_path=netlist or None,
                faults_path=faults or None,
                constraints_path=constraints or None,
                analyst_note=analyst_note,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to build HTML report: %s", exc)
            self._set_summary_html(
                f"<body><h2>Report generation failed</h2><pre>{exc}</pre></body>")
            return
        self._report_html = html
        self._set_summary_html(html)
        self.open_browser_btn.setEnabled(True)

    def _populate_table(self, report: AnalysisReport) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for r in report.fault_results:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                r.fault.fault_object, r.fault.fault_class.value,
                r.mapping.instance_name or "—", r.mapping.confidence.value,
                r.instance_name or "—", r.cell_type or "—",
                str(len(r.fan_in)), str(len(r.fan_out)),
                "yes" if r.controllability_issue else "no",
                "yes" if r.observability_issue else "no",
                "yes" if r.constraint_related else "no",
                "yes" if r.scan_boundary_involved else "no",
                r.root_cause.value,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col == 0:
                    item.setData(Qt.UserRole, row)
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self._apply_filter()

    def _populate_patterns(self, report: AnalysisReport) -> None:
        self.patterns_table.setRowCount(0)
        for g in report.pattern_groups:
            row = self.patterns_table.rowCount()
            self.patterns_table.insertRow(row)
            for col, val in enumerate([g.kind, g.key, str(g.count),
                                        ", ".join(g.sample_faults)]):
                self.patterns_table.setItem(row, col, QTableWidgetItem(val))

    def _populate_logs(self, report: AnalysisReport) -> None:
        lines = []
        if report.warnings:
            lines.extend(f"- {w}" for w in report.warnings)
        if report.skill_results:
            skill_warnings = []
            for sr in report.skill_results:
                for msg in sr.warnings:
                    skill_warnings.append(f"[{sr.skill_id}] {msg}")
            if skill_warnings:
                lines.append("")
                lines.append("=== Skill Warnings ===")
                lines.extend(skill_warnings)
        self.logs_view.setPlainText("\n".join(lines) if lines else "No warnings.")

    def _apply_filter(self) -> None:
        text = self.filter_text.text().strip().lower()
        cls = self.class_filter.currentText()
        conf = self.conf_filter.currentText()
        for row in range(self.table.rowCount()):
            visible = True
            row_class = self.table.item(row, 1).text()
            row_conf = self.table.item(row, 3).text()
            if cls != "all" and row_class != cls:
                visible = False
            if conf != "all" and row_conf != conf:
                visible = False
            if visible and text:
                joined = " ".join(
                    self.table.item(row, c).text().lower() for c in (0, 4, 12))
                if text not in joined:
                    visible = False
            self.table.setRowHidden(row, not visible)

    def _on_row_selected(self) -> None:
        items = self.table.selectedItems()
        if not items:
            return
        row = items[0].row()
        idx_item = self.table.item(row, 0)
        idx = idx_item.data(Qt.UserRole)
        if idx is None or idx >= len(self._results):
            return
        self.details.show_result(self._results[idx])

    def _on_table_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        id_item = self.table.item(item.row(), 0)
        if id_item is None:
            return
        fault_object = id_item.text()
        menu = QMenu(self)
        ask_act = menu.addAction("Ask AI agent about this fault")
        rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        rows.add(item.row())
        exclude_act = None
        if self._base_report:
            label = ("Exclude selected fault(s) from report" if len(rows) > 1
                     else "Exclude this fault from report")
            exclude_act = menu.addAction(label)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == ask_act:
            self._switch_to_tab("AI Debug Agent")
            self.agent_panel.ask_about_fault(fault_object)
        elif exclude_act is not None and chosen == exclude_act:
            if not self.table.selectionModel().isRowSelected(
                    item.row(), self.table.rootIndex()):
                self.table.selectRow(item.row())
            self._exclude_selected_faults()

    def _switch_to_tab(self, title: str) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == title:
                self.tabs.setCurrentIndex(i)
                return

    def _focus_fault_in_table(self, fault_object: str) -> None:
        """Select and reveal the table row for *fault_object* (from an agent link)."""
        self._switch_to_tab("Coverage Loss Table")
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == fault_object:
                self.table.clearSelection()
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                self.table.setFocus()
                return
        self.statusBar().showMessage(
            f"'{fault_object}' is not a row in the current table.")

    def _default_path(self, name: str) -> str:
        outdir = self.outdir_picker.path()
        if outdir and os.path.isdir(outdir):
            return os.path.join(outdir, name)
        return name

    def on_open_report_in_browser(self) -> None:
        if not self._report_html:
            return
        outdir = self.outdir_picker.path()
        try:
            if outdir and os.path.isdir(outdir):
                serve_dir = outdir
            else:
                serve_dir = tempfile.mkdtemp(prefix="atpg_report_")
            path = os.path.join(serve_dir, "atpg_coverage_report.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._report_html)
        except OSError as exc:
            self._error(f"Could not write the HTML report:\n{exc}")
            return

        # Publish the report over HTTP so the link can be shared with others.
        try:
            port = self._ensure_report_server(serve_dir)
        except OSError as exc:
            logger.warning("Could not start report server: %s", exc)
            port = None

        if port is not None:
            host = socket.getfqdn()
            share_url = f"http://{host}:{port}/atpg_coverage_report.html"
        else:
            # Fall back to a local-only file URL if the server failed to start.
            share_url = QUrl.fromLocalFile(path).toString()

        firefox = shutil.which("firefox")
        opened = False
        if firefox:
            try:
                subprocess.Popen(
                    [firefox, share_url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                opened = True
            except OSError:
                pass  # fall back to the default browser below

        if not opened:
            opened = QDesktopServices.openUrl(QUrl(share_url))

        if not opened:
            self._error(
                "Could not open Firefox or any web browser automatically. The "
                f"report is available at:\n{share_url}")
            return

        self.statusBar().showMessage(f"Report opened in browser: {share_url}")
        if port is not None:
            self._show_share_dialog(share_url)
        else:
            QMessageBox.information(
                self, "Report opened",
                "The report was opened locally, but a shareable network link "
                "could not be created (the HTTP server failed to start).\n\n"
                f"Local file:\n{path}")

    def _ensure_report_server(self, directory: str) -> int:
        """Start (or reuse) a background HTTP server serving ``directory``.

        Returns the TCP port the server is listening on. If a server is
        already running for a different directory it is restarted so the
        shared link always points at the freshest report.
        """
        directory = os.path.abspath(directory)
        if self._report_server is not None:
            if self._report_server_dir == directory:
                return self._report_server.server_address[1]
            self._shutdown_report_server()

        handler = partial(_QuietHTTPRequestHandler, directory=directory)
        # Bind to all interfaces on an ephemeral port so peers can reach it.
        server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        thread = threading.Thread(
            target=server.serve_forever, name="atpg-report-server", daemon=True)
        thread.start()

        self._report_server = server
        self._report_server_thread = thread
        self._report_server_dir = directory
        return server.server_address[1]

    def _shutdown_report_server(self) -> None:
        if self._report_server is not None:
            try:
                self._report_server.shutdown()
                self._report_server.server_close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        self._report_server = None
        self._report_server_thread = None
        self._report_server_dir = None

    def _show_share_dialog(self, url: str) -> None:
        """Show the shareable report link with a one-click copy button."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Share Report Link")
        layout = QVBoxLayout(dialog)

        label = QLabel(
            "The report is now published on this machine. Share the link "
            "below so other users on the network can view it:")
        label.setWordWrap(True)
        layout.addWidget(label)

        row = QHBoxLayout()
        link_edit = QLineEdit(url)
        link_edit.setReadOnly(True)
        link_edit.selectAll()
        row.addWidget(link_edit, 1)

        copy_btn = QPushButton("Copy Link")

        def _copy() -> None:
            QApplication.clipboard().setText(url)
            copy_btn.setText("Copied!")
            self.statusBar().showMessage(f"Report link copied: {url}")

        copy_btn.clicked.connect(_copy)
        row.addWidget(copy_btn)
        layout.addLayout(row)

        note = QLabel(
            "<i>The link stays active while this application is running.</i>")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.resize(520, dialog.sizeHint().height())
        dialog.exec()

    def on_export_md(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Markdown report", self._default_path("atpg_report.md"), "Markdown (*.md)")
        if not path:
            return
        try:
            write_markdown(self._report, path)
        except OSError as exc:
            self._error(f"Could not write Markdown report:\n{exc}")
            return
        self.statusBar().showMessage(f"Markdown report saved: {path}")

    def on_export_csv(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV report", self._default_path("atpg_report.csv"), "CSV (*.csv)")
        if not path:
            return
        try:
            write_csv(self._report, path)
        except OSError as exc:
            self._error(f"Could not write CSV report:\n{exc}")
            return
        self.statusBar().showMessage(f"CSV report saved: {path}")

    def on_export_filtered_csv(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save filtered CSV", self._default_path("atpg_filtered.csv"), "CSV (*.csv)")
        if not path:
            return
        visible_results = []
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                idx_item = self.table.item(row, 0)
                idx = idx_item.data(Qt.UserRole)
                if idx is not None and idx < len(self._results):
                    visible_results.append(self._results[idx])
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv_mod.writer(f)
                writer.writerow(_TABLE_HEADERS)
                for r in visible_results:
                    writer.writerow([
                        r.fault.fault_object, r.fault.fault_class.value,
                        r.mapping.instance_name or "", r.mapping.confidence.value,
                        r.instance_name or "", r.cell_type or "",
                        len(r.fan_in), len(r.fan_out),
                        r.controllability_issue, r.observability_issue,
                        r.constraint_related, r.scan_boundary_involved,
                        r.root_cause.value,
                    ])
        except OSError as exc:
            self._error(f"Could not write filtered CSV:\n{exc}")
            return
        self.statusBar().showMessage(
            f"Filtered CSV saved: {path} ({len(visible_results)} rows)")

    def on_clear(self) -> None:
        self._report = None
        self._base_report = None
        self._results = []
        self._report_html = None
        self._reset_partitions()
        self.table.setRowCount(0)
        self.patterns_table.setRowCount(0)
        self._clear_summary()
        self.open_browser_btn.setEnabled(False)
        self.logs_view.clear()
        self.details.clear_details()
        self.skills_panel.clear_results()
        self.agent_panel.clear()
        self._set_export_enabled(False)
        self.statusBar().showMessage("Cleared.")

    def _save_settings(self) -> None:
        s = self._settings
        s.last_netlist = self.netlist_picker.path()
        s.last_faults = self.faults_picker.path()
        s.last_constraints = self.constraints_picker.path()
        s.last_output_dir = self.outdir_picker.path()
        s.filter_text = self.filter_text.text()
        s.class_filter = self.class_filter.currentText()
        s.conf_filter = self.conf_filter.currentText()
        s.update_skills(self._skill_manager.to_config())
        s.agent = self.agent_panel.export_settings()
        s.custom_skills_dir = self.custom_skills_panel.custom_dir()
        s.save()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        self._shutdown_report_server()
        super().closeEvent(event)

    def _set_export_enabled(self, enabled: bool) -> None:
        self.md_btn.setEnabled(enabled)
        self.csv_btn.setEnabled(enabled)
        self.save_report_btn.setEnabled(enabled)
        self.compare_btn.setEnabled(enabled)
        self.edit_btn.setEnabled(enabled)

    def on_edit_report(self) -> None:
        if not self._base_report:
            return
        current_edits = getattr(self._report, "edits", None) or {}
        ex_classes = set(current_edits.get("excluded_classes", []))
        ex_subtypes = {s.upper() for s in current_edits.get("excluded_subtypes", [])}
        ex_ids = list(current_edits.get("excluded_ids", []))
        note = current_edits.get("note", "")

        # Loss subtypes present in the *base* report, with their fault counts.
        subtype_counts = self._loss_subtype_counts(self._base_report)

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Report")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "Waive coverage-loss faults and/or record an analyst note. Excluded "
            "faults are removed from the totals so the coverage metric recomputes; "
            "the report layout is unchanged. Edits are reversible and saved with "
            "the report."))

        # --- Whole coarse classes -------------------------------------------
        class_box = QGroupBox("Exclude whole fault classes")
        class_layout = QVBoxLayout(class_box)
        class_checks = {}
        for cls in ("AU", "UO", "UC"):
            cb = QCheckBox(f"Exclude all {cls} faults")
            cb.setChecked(cls in ex_classes)
            class_layout.addWidget(cb)
            class_checks[cls] = cb
        v.addWidget(class_box)

        # --- Specific subtypes (e.g. AU.NOFAULTS) ---------------------------
        subtype_checks = {}
        if subtype_counts:
            sub_box = QGroupBox("Exclude specific fault subtypes")
            sub_outer = QVBoxLayout(sub_box)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            for token, count in subtype_counts:
                cb = QCheckBox(f"{token}  ({count} fault{'s' if count != 1 else ''})")
                cb.setChecked(token.upper() in ex_subtypes)
                inner_layout.addWidget(cb)
                subtype_checks[token] = cb
            inner_layout.addStretch(1)
            scroll.setWidget(inner)
            scroll.setMinimumHeight(120)
            sub_outer.addWidget(scroll)
            v.addWidget(sub_box, 1)

        # --- Specific faults by id / path -----------------------------------
        v.addWidget(QLabel("Exclude specific faults by object path (one per line):"))
        ids_edit = QPlainTextEdit()
        ids_edit.setPlainText("\n".join(ex_ids))
        ids_edit.setPlaceholderText("/top/u_seq/optlc_900/o")
        ids_edit.setMaximumHeight(80)
        v.addWidget(ids_edit)

        v.addWidget(QLabel("Analyst note / annotation:"))
        note_edit = QPlainTextEdit()
        note_edit.setPlainText(note)
        note_edit.setPlaceholderText(
            "e.g. AU.NOFAULTS reviewed and waived as legitimately untestable "
            "(black-box RAM boundary).")
        note_edit.setMaximumHeight(80)
        v.addWidget(note_edit, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)
        dlg.resize(560, 560)
        if dlg.exec() != QDialog.Accepted:
            return

        excluded = [cls for cls, cb in class_checks.items() if cb.isChecked()]
        excluded_subtypes = [tok for tok, cb in subtype_checks.items()
                             if cb.isChecked()]
        excluded_ids = [ln.strip() for ln in ids_edit.toPlainText().splitlines()
                        if ln.strip()]
        new_note = note_edit.toPlainText().strip()
        edited = report_edit.apply_exclusions(
            self._base_report, excluded_classes=excluded,
            excluded_subtypes=excluded_subtypes, excluded_ids=excluded_ids,
            note=new_note)
        self._apply_report(edited)
        banner = report_edit.edit_banner(edited.edits)
        self.statusBar().showMessage(
            "Report edited" + (f": {banner}" if banner else " (note updated).")
            + f"  {edited.summary.coverage_loss_count} coverage-loss faults remain.")

    @staticmethod
    def _loss_subtype_counts(report: AnalysisReport) -> List[tuple]:
        """Return ``[(subtype_token, count), ...]`` for coverage-loss subtypes.

        Only dotted subtypes whose coarse class is a coverage-loss class
        (``AU`` / ``UO`` / ``UC``) are offered for waiving; they are sorted by
        descending count so the biggest contributors surface first.
        """
        from collections import Counter
        counts: Counter = Counter()
        for r in report.fault_results:
            cls = r.fault.fault_class.value
            if cls not in ("AU", "UO", "UC"):
                continue
            token = r.fault.raw_class_token or cls
            if "." in token:
                counts[token] += 1
        return counts.most_common()

    def _exclude_selected_faults(self) -> None:
        """Waive the faults selected in the Coverage Loss Table."""
        if not self._base_report:
            return
        objects = set()
        for item in self.table.selectedItems():
            if item.column() == 0:
                objects.add(item.text())
        if not objects:
            return
        current_edits = getattr(self._report, "edits", None) or {}
        ex_ids = set(current_edits.get("excluded_ids", [])) | objects
        edited = report_edit.apply_exclusions(
            self._base_report,
            excluded_classes=current_edits.get("excluded_classes", []),
            excluded_subtypes=current_edits.get("excluded_subtypes", []),
            excluded_ids=sorted(ex_ids),
            note=current_edits.get("note", ""))
        self._apply_report(edited)
        self.statusBar().showMessage(
            f"Excluded {len(objects)} fault(s).  "
            f"{edited.summary.coverage_loss_count} coverage-loss faults remain.")

    def on_compare_report(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load baseline report to compare (JSON)",
            self._default_path("atpg_report.json"),
            "ATPG report (*.json);;All files (*)")
        if not path:
            return
        try:
            baseline = load_report(path)
        except (OSError, ValueError) as exc:
            self._error(f"Could not load baseline report:\n{exc}")
            return
        label = os.path.basename(path)
        compare = investigate.serialize_report_for_compare(
            baseline.fault_results, baseline.summary, baseline.constraints,
            label=label)
        current = [investigate.serialize_fault_result(fr)
                   for fr in self._report.fault_results]
        summ = regression.summary(
            compare["faults"], current, compare.get("summary"),
            {"class_counts": dict(self._report.summary.class_counts)},
            label=label)
        c = summ["counts"]
        self.agent_panel.set_compare(compare)
        self._switch_to_tab("AI Debug Agent")
        QMessageBox.information(
            self, "Regression vs baseline",
            f"Baseline: {label}\n\n"
            f"Baseline coverage-loss: {c['baseline_loss']}\n"
            f"Current coverage-loss:  {c['current_loss']}\n"
            f"Net delta: {c['net_delta']:+d}\n\n"
            f"Regressed (new loss): {c['regressed']}\n"
            f"Fixed (improved):     {c['fixed']}\n"
            f"Changed (class/root): {c['changed']}\n\n"
            "The AI agent can now use the regression tools — ask it "
            "'what changed vs the baseline?'")
        self.statusBar().showMessage(
            f"Compared against {label}: +{c['regressed']} regressed, "
            f"-{c['fixed']} fixed, {c['changed']} changed.")

    def on_save_report(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report (JSON)",
            self._default_path("atpg_report.json"), "ATPG report (*.json)")
        if not path:
            return
        # Capture the current agent investigation (diagnosis, chat, trace).
        self._report.investigation = self.agent_panel.export_investigation()
        try:
            save_report(self._report, path)
        except OSError as exc:
            self._error(f"Could not save report:\n{exc}")
            return
        self.statusBar().showMessage(f"Report saved: {path}")

    def on_load_report(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load report (JSON)",
            self._default_path("atpg_report.json"),
            "ATPG report (*.json);;All files (*)")
        if not path:
            return
        try:
            report = load_report(path)
        except (OSError, ValueError) as exc:
            self._error(f"Could not load report:\n{exc}")
            return
        self._reset_partitions()
        self._base_report = report
        self._apply_report(report)
        self.statusBar().showMessage(
            f"Report loaded: {path} — "
            f"{report.summary.coverage_loss_count} coverage-loss faults. "
            "Work on it without re-analyzing.")

    def _error(self, message: str) -> None:
        QMessageBox.critical(self, "Error", message)


def run() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
