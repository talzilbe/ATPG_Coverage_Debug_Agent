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
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStatusBar,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
    QWidget,
)

from ..app import AnalysisInputs
from ..config.settings import AppSettings
from ..models import AnalysisReport, FaultAnalysisResult
from ..reporting.csv_report import write_csv
from ..reporting.html_report import build_html_report
from ..reporting.markdown_report import write_markdown
from ..skills.manager import SkillManager
from .details_panel import DetailsPanel
from .skills_panel import SkillsPanel
from .agent_panel import AgentPanel
from .custom_skills_panel import CustomSkillsPanel
from .workers import start_worker

logger = logging.getLogger(__name__)

_TABLE_HEADERS = [
    "Fault Object", "Class", "Mapped", "Confidence", "Instance", "Cell",
    "Fan-in", "Fan-out", "Ctrl", "Obsv", "Constraint", "Scan", "Root Cause",
]


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
        self._results: List[FaultAnalysisResult] = []
        self._report_html: Optional[str] = None
        self._thread: Optional[QThread] = None
        self._worker = None

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
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.on_clear)
        for b in (self.analyze_btn, self.cancel_btn, self.md_btn,
                  self.csv_btn, self.clear_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)
        self._set_export_enabled(False)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

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
        self._report = report
        self._results = report.fault_results
        self.progress.setVisible(False)
        self.analyze_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._set_export_enabled(True)
        self._populate(report)
        if report.skill_results:
            self.skills_panel.show_results(report.skill_results)
        self.agent_panel.set_report(report, self._skill_manager)
        n_skills = len(report.skill_results) if report.skill_results else 0
        self.statusBar().showMessage(
            f"Done. {report.summary.coverage_loss_count} coverage-loss faults. "
            f"{n_skills} skill(s) ran.")

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
        design = None
        netlist = self.netlist_picker.path()
        if netlist:
            base = os.path.basename(netlist)
            for ext in (".v.gz", ".gz", ".v"):
                if base.endswith(ext):
                    base = base[: -len(ext)]
                    break
            design = base or None
        try:
            html = build_html_report(
                report,
                design_name=design,
                netlist_path=netlist or None,
                faults_path=self.faults_picker.path() or None,
                constraints_path=self.constraints_picker.path() or None,
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
        self._results = []
        self._report_html = None
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

    def _error(self, message: str) -> None:
        QMessageBox.critical(self, "Error", message)


def run() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
