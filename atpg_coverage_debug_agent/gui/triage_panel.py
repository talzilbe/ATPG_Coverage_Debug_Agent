"""Triage and fix-plan panel.

Presents the coverage-loss triage the way an engineer works through it:
*which* categories are losing coverage, *where* in the hierarchy they
concentrate, *what* is blocking them, and *what to do next*.

The three views are deliberately separate. Hierarchy clustering says where to
look and is never a root cause; the blocking-source attribution and structural
profile are estimates from the netlist; only the fix plan makes a
recommendation. Collapsing them into one list would blur exactly the
distinctions that keep the output trustworthy.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_CATEGORY_HEADERS = ["Category", "Faults", "% of all", "sa0", "sa1",
                     "Imbalance", "Worth acting on", "Confidence"]

_EMPTY_HTML = (
    "<body style='font-family: Segoe UI, sans-serif; padding: 30px; "
    "color: #6c757d;'><p>Run an analysis to see the coverage triage.</p>"
    "</body>")

#: Colours for the actionability verdict, kept subtle enough to read as a hint
#: rather than a judgement.
_ACTIONABLE_COLOURS = {
    "true": "#1b7f3b",
    "partial": "#8a6d00",
    "false": "#6c757d",
}


def _esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _html_page(body: str) -> str:
    return (
        "<body style=\"font-family: Segoe UI, sans-serif; font-size: 13px; "
        "color: #212529;\">" + body + "</body>")


class TriagePanel(QWidget):
    """Coverage triage, hierarchy hotspots and the ranked fix plan.

    Signals:
        fault_referenced: Emitted with a fault object path when the user
            activates a sample, so the main window can focus that fault in the
            coverage-loss table.
    """

    fault_referenced = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._report: Any = None
        self._categories: List[Any] = []
        self._recommendations: List[Any] = []
        self._build_ui()

    # -- construction -----------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_categories_tab(), "Categories")
        self.tabs.addTab(self._build_clusters_tab(), "Where the loss is")
        self.tabs.addTab(self._build_fixes_tab(), "Fix Plan")
        layout.addWidget(self.tabs, 1)

    def _build_categories_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self.totals_label = QLabel(
            "Run an analysis to see the coverage breakdown.")
        self.totals_label.setWordWrap(True)
        layout.addWidget(self.totals_label)

        splitter = QSplitter(Qt.Horizontal)
        self.category_table = QTableWidget(0, len(_CATEGORY_HEADERS))
        self.category_table.setHorizontalHeaderLabels(_CATEGORY_HEADERS)
        self.category_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.category_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.category_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.category_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        self.category_table.itemSelectionChanged.connect(
            self._on_category_selected)
        splitter.addWidget(self.category_table)

        self.category_detail = QTextBrowser()
        self.category_detail.setOpenExternalLinks(False)
        self.category_detail.setHtml(_EMPTY_HTML)
        splitter.addWidget(self.category_detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)
        return widget

    def _build_clusters_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        note = QLabel(
            "A dominant prefix shows <b>where</b> the faults are — it is not a "
            "root cause. Double-click a sample to focus it in the coverage-loss "
            "table; sample paths are verbatim and safe to paste into a tool.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        layout.addWidget(note)

        self.cluster_tree = QTreeWidget()
        self.cluster_tree.setHeaderLabels(
            ["Hierarchy prefix", "Faults", "%", "sa0", "sa1"])
        self.cluster_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.cluster_tree.itemDoubleClicked.connect(self._on_cluster_activated)
        self.cluster_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        layout.addWidget(self.cluster_tree, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_prefix_btn = QPushButton("Copy selected path")
        self.copy_prefix_btn.setToolTip(
            "Copy the selected prefix or sample path to the clipboard, "
            "verbatim")
        self.copy_prefix_btn.clicked.connect(self._copy_selected_prefix)
        row.addWidget(self.copy_prefix_btn)
        layout.addLayout(row)
        return widget

    def _build_fixes_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        note = QLabel(
            "Commands are for you to run in your own ATPG session — this tool "
            "never executes them. Where an action needs measurement, no "
            "coverage gain is predicted: the re-run is what establishes it.")
        note.setWordWrap(True)
        layout.addWidget(note)

        splitter = QSplitter(Qt.Horizontal)
        self.fix_list = QListWidget()
        self.fix_list.currentRowChanged.connect(self._on_fix_selected)
        splitter.addWidget(self.fix_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.fix_detail = QTextBrowser()
        self.fix_detail.setHtml(_EMPTY_HTML)
        right_layout.addWidget(self.fix_detail, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.copy_commands_btn = QPushButton("Copy commands")
        self.copy_commands_btn.setToolTip(
            "Copy this fix's commands to the clipboard")
        self.copy_commands_btn.clicked.connect(self._copy_commands)
        self.copy_commands_btn.setEnabled(False)
        row.addWidget(self.copy_commands_btn)
        right_layout.addLayout(row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        layout.addWidget(splitter, 1)
        return widget

    # -- population -------------------------------------------------------
    def set_report(self, report: Any) -> None:
        """Populate every view from *report*, or clear when it has no triage."""
        self._report = report
        self._categories = list(getattr(report, "selected_categories", None)
                                or [])
        self._recommendations = list(getattr(report, "recommendations", None)
                                     or [])
        self._populate_totals(report)
        self._populate_categories()
        self._populate_clusters()
        self._populate_fixes()

    def clear(self) -> None:
        """Reset every view to its empty state."""
        self._report = None
        self._categories = []
        self._recommendations = []
        self.totals_label.setText(
            "Run an analysis to see the coverage breakdown.")
        self.category_table.setRowCount(0)
        self.category_detail.setHtml(_EMPTY_HTML)
        self.cluster_tree.clear()
        self.fix_list.clear()
        self.fix_detail.setHtml(_EMPTY_HTML)
        self.copy_commands_btn.setEnabled(False)

    def _populate_totals(self, report: Any) -> None:
        stats = getattr(report, "statistics", None)
        if stats is None:
            self.totals_label.setText("This report has no coverage triage.")
            return
        self.totals_label.setText(
            f"Detected {stats.detected_count} ({stats.detected_pct:.2f}%) · "
            f"coverage loss {stats.loss_count} ({stats.loss_pct:.2f}%) of "
            f"{stats.total_faults} fault(s). These are fault-list totals, not "
            f"the tool's test-coverage figure.")

    def _populate_categories(self) -> None:
        stats = getattr(self._report, "statistics", None)
        rows = list(stats.loss_stats) if stats is not None else []
        by_id = {c.subclass_id: c for c in self._categories}

        self.category_table.setRowCount(len(rows))
        for row, stat in enumerate(rows):
            category = by_id.get(stat.subclass_id)
            verdict = getattr(category, "verdict", None)
            values = [
                stat.subclass_id,
                str(stat.count),
                f"{stat.pct:.2f}%",
                str(stat.sa0),
                str(stat.sa1),
                f"{stat.sa_asymmetry:.2f}",
                getattr(verdict, "actionable", "—"),
                getattr(getattr(verdict, "confidence", None), "value", "—"),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 6 and verdict is not None:
                    colour = _ACTIONABLE_COLOURS.get(verdict.actionable)
                    if colour:
                        item.setForeground(QColor(colour))
                item.setData(Qt.UserRole, stat.subclass_id)
                self.category_table.setItem(row, col, item)

        if rows:
            self.category_table.selectRow(0)
        else:
            self.category_detail.setHtml(_html_page(
                "<p>No coverage-loss categories were found.</p>"))

    def _selected_category_id(self) -> Optional[str]:
        items = self.category_table.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.UserRole)

    def _category_by_id(self, subclass_id: Optional[str]) -> Optional[Any]:
        for category in self._categories:
            if category.subclass_id == subclass_id:
                return category
        return None

    def _on_category_selected(self) -> None:
        subclass_id = self._selected_category_id()
        self.category_detail.setHtml(
            _html_page(self._category_html(subclass_id)))

    def _category_html(self, subclass_id: Optional[str]) -> str:
        stats = getattr(self._report, "statistics", None)
        stat = stats.get(subclass_id) if stats is not None else None
        if stat is None:
            return "<p>Select a category to see the evidence behind it.</p>"

        parts = [f"<h3>{_esc(stat.subclass_id)}</h3>"]
        info = stat.info
        if info is not None:
            parts.append(f"<p><b>{_esc(info.title)}</b> — "
                         f"{_esc(info.meaning)}</p>")
            if info.primary_causes:
                parts.append("<p><b>Usual causes</b></p><ul>")
                parts.extend(f"<li>{_esc(c)}</li>"
                             for c in info.primary_causes)
                parts.append("</ul>")
            if info.caveat:
                parts.append(f"<p><b>Caveat:</b> {_esc(info.caveat)}</p>")

        category = self._category_by_id(subclass_id)
        if category is None:
            parts.append("<p><i>This category was not selected for "
                         "investigation, so no deeper evidence was "
                         "gathered.</i></p>")
            return "".join(parts)

        verdict = getattr(category, "verdict", None)
        if verdict is not None:
            parts.append(
                f"<h4>Scored verdict</h4><p>Worth acting on: "
                f"<b>{_esc(verdict.actionable)}</b> "
                f"({_esc(verdict.confidence.value)} confidence)<br>"
                f"{_esc(verdict.reason)}</p>")
            if verdict.patterns:
                parts.append(f"<p><b>Pattern:</b> "
                             f"{_esc(', '.join(verdict.patterns))}</p>")

        attribution = getattr(category, "attribution", None)
        if attribution is not None and attribution.note:
            parts.append(f"<h4>What is blocking them</h4>"
                         f"<p>{_esc(attribution.note)}</p>")
            if attribution.tie_sources:
                parts.append("<ul>")
                for src in attribution.tie_sources[:5]:
                    value = (f", tied {src.tie_value}" if src.tie_value else "")
                    parts.append(
                        f"<li><code>{_esc(src.driver)}</code> "
                        f"[{_esc(src.cell_type)}{_esc(value)}] — "
                        f"{src.count} fault(s), {_esc(src.kind)}</li>")
                parts.append("</ul>")
            if attribution.constraint_sources:
                parts.append("<ul>")
                for src in attribution.constraint_sources[:5]:
                    parts.append(
                        f"<li><code>{_esc(src.signal)}</code> = "
                        f"{_esc(src.value or '?')} — {src.count} fault(s)</li>")
                parts.append("</ul>")

        reachability = getattr(category, "reachability", None)
        if reachability is not None and reachability.note:
            parts.append(f"<h4>Why they were hard to test</h4>"
                         f"<p>{_esc(reachability.note)}</p>")

        return "".join(parts)

    def _populate_clusters(self) -> None:
        self.cluster_tree.clear()
        for category in self._categories:
            clusters = getattr(category, "clusters", None)
            if clusters is None or not clusters.clusters:
                continue
            root = QTreeWidgetItem([
                f"{category.subclass_id} ({clusters.depth_note})",
                str(clusters.total_faults), "", "", "",
            ])
            self.cluster_tree.addTopLevelItem(root)
            for cluster in clusters.clusters[:20]:
                node = QTreeWidgetItem([
                    cluster.prefix,
                    str(cluster.count),
                    f"{cluster.pct:.1f}%",
                    str(cluster.sa0),
                    str(cluster.sa1),
                ])
                node.setData(0, Qt.UserRole, cluster.prefix)
                root.addChild(node)
                for sample in cluster.samples:
                    leaf = QTreeWidgetItem([sample, "", "", "", ""])
                    leaf.setData(0, Qt.UserRole, sample)
                    # Marks the row as a real fault, so activating it can
                    # focus that fault rather than a derived prefix.
                    leaf.setData(1, Qt.UserRole, "fault")
                    node.addChild(leaf)
            root.setExpanded(True)

    def _on_cluster_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(1, Qt.UserRole) != "fault":
            return
        path = item.data(0, Qt.UserRole)
        if path:
            self.fault_referenced.emit(str(path))

    def _copy_selected_prefix(self) -> None:
        item = self.cluster_tree.currentItem()
        if item is None:
            return
        path = item.data(0, Qt.UserRole) or item.text(0)
        QApplication.clipboard().setText(str(path))

    def _populate_fixes(self) -> None:
        self.fix_list.clear()
        for rec in self._recommendations:
            entry = QListWidgetItem(
                f"{rec.rank}. [{rec.subclass_id}] {rec.title}")
            entry.setToolTip(f"{rec.fault_count} fault(s) · "
                             f"{rec.confidence.value} confidence")
            self.fix_list.addItem(entry)
        if self._recommendations:
            self.fix_list.setCurrentRow(0)
        else:
            self.fix_detail.setHtml(_html_page(
                "<p>No fix proposals — there is no coverage loss to act on.</p>"))
            self.copy_commands_btn.setEnabled(False)

    def _current_recommendation(self) -> Optional[Any]:
        row = self.fix_list.currentRow()
        if 0 <= row < len(self._recommendations):
            return self._recommendations[row]
        return None

    def _on_fix_selected(self, _row: int) -> None:
        rec = self._current_recommendation()
        if rec is None:
            self.fix_detail.setHtml(_EMPTY_HTML)
            self.copy_commands_btn.setEnabled(False)
            return
        self.copy_commands_btn.setEnabled(bool(rec.fix.commands))
        self.fix_detail.setHtml(_html_page(self._fix_html(rec)))

    def _fix_html(self, rec: Any) -> str:
        parts = [f"<h3>{_esc(rec.title)}</h3>"]
        parts.append(
            f"<p><b>Category:</b> {_esc(rec.subclass_id)} "
            f"({rec.fault_count} faults, {rec.pct:.2f}%)<br>"
            f"<b>Worth acting on:</b> {_esc(rec.actionable)} · "
            f"<b>confidence:</b> {_esc(rec.confidence.value)}<br>"
            f"<b>Effort / risk:</b> {_esc(rec.fix.effort)} / "
            f"{_esc(rec.fix.risk)}</p>")
        if rec.hotspot:
            parts.append(f"<p><b>Concentrated under:</b> "
                         f"<code>{_esc(rec.hotspot)}</code></p>")
        parts.append(f"<p><b>Why:</b> {_esc(rec.fix.rationale)}</p>")

        if rec.fix.preconditions:
            parts.append("<p><b>Confirm first</b></p><ul>")
            parts.extend(f"<li>{_esc(p)}</li>" for p in rec.fix.preconditions)
            parts.append("</ul>")
        if rec.evidence:
            parts.append("<p><b>Evidence</b></p><ul>")
            parts.extend(f"<li>{_esc(e)}</li>" for e in rec.evidence)
            parts.append("</ul>")
        if rec.fix.expected_effect:
            parts.append(f"<p><b>Expected outcome:</b> "
                         f"{_esc(rec.fix.expected_effect)}</p>")
        if rec.caveats:
            parts.append("<p><b>Caveats</b></p><ul>")
            parts.extend(f"<li>{_esc(c)}</li>" for c in rec.caveats)
            parts.append("</ul>")
        if rec.fix.commands:
            body = "\n".join(rec.fix.commands)
            parts.append(
                "<p><b>Commands</b> (run these yourself)</p>"
                "<pre style='background:#f5f5f5; padding:8px; "
                "border:1px solid #ddd;'>" + _esc(body) + "</pre>")
        return "".join(parts)

    def _copy_commands(self) -> None:
        rec = self._current_recommendation()
        if rec is None or not rec.fix.commands:
            return
        QApplication.clipboard().setText("\n".join(rec.fix.commands))
