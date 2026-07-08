"""Skills panel — GUI component for managing and displaying skill settings."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..skills.base import SkillBase, SkillResult
from ..skills.manager import SkillManager

logger = logging.getLogger(__name__)


class _SkillCard(QGroupBox):
    """A single card UI for one skill: checkbox + description + parameters."""

    changed = Signal()  # emitted when enabled state or params change
    selected = Signal(object)  # emitted (with the skill) when the card is picked

    def __init__(self, skill: SkillBase, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._skill = skill
        self._param_widgets: Dict[str, QWidget] = {}
        self._build()

    @property
    def skill(self) -> SkillBase:
        return self._skill

    def _build(self) -> None:
        self.setTitle("")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        # --- Header row ---
        header = QHBoxLayout()
        self._check = QCheckBox(self._skill.display_name)
        self._check.setChecked(self._skill.enabled)
        self._check.setStyleSheet("font-weight: bold;")
        self._check.stateChanged.connect(self._on_enabled_changed)
        header.addWidget(self._check, 1)
        view_btn = QPushButton("View content")
        view_btn.setToolTip("Show this skill's content / guidance")
        view_btn.clicked.connect(lambda: self.selected.emit(self._skill))
        header.addWidget(view_btn, 0)
        outer.addLayout(header)

        # --- Description ---
        desc = QLabel(self._skill.description)
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #555; font-size: 11px;")
        outer.addWidget(desc)

        # --- Parameters (if any) ---
        schema = self._skill.parameters_schema()
        if schema:
            params_box = QFrame()
            params_box.setFrameShape(QFrame.StyledPanel)
            params_layout = QFormLayout(params_box)
            params_layout.setContentsMargins(4, 4, 4, 4)
            for param_name, param_info in schema.items():
                widget = self._make_param_widget(param_name, param_info)
                label = QLabel(param_info.get("description", param_name) + ":")
                label.setStyleSheet("font-size: 11px;")
                params_layout.addRow(label, widget)
                self._param_widgets[param_name] = widget
            outer.addWidget(params_box)

    def _make_param_widget(self, name: str, info: Dict[str, Any]) -> QWidget:
        """Create an appropriate input widget for the parameter type."""
        ptype = info.get("type", "str")
        current = self._skill.get_param(name)

        if ptype == "int":
            w = QSpinBox()
            w.setMinimum(-999999)
            w.setMaximum(999999)
            w.setValue(int(current))
            w.valueChanged.connect(
                lambda v, n=name: self._on_param_changed(n, v))
        else:
            w = QLineEdit(str(current))
            w.textChanged.connect(
                lambda v, n=name: self._on_param_changed(n, v))
        return w

    def _on_enabled_changed(self, state: int) -> None:
        self._skill.enabled = (state == Qt.Checked)
        self.changed.emit()
        if self._skill.enabled:
            # Marking (checking) a skill shows its content.
            self.selected.emit(self._skill)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.selected.emit(self._skill)
        super().mousePressEvent(event)

    def _on_param_changed(self, name: str, value: Any) -> None:
        try:
            self._skill.set_param(name, value)
        except Exception as exc:
            logger.warning("Skill param error: %s", exc)
        self.changed.emit()

    def refresh(self) -> None:
        """Sync widget state from the skill (e.g. after reset defaults)."""
        self._check.blockSignals(True)
        self._check.setChecked(self._skill.enabled)
        self._check.blockSignals(False)
        for name, widget in self._param_widgets.items():
            val = self._skill.get_param(name)
            if isinstance(widget, QSpinBox):
                widget.blockSignals(True)
                widget.setValue(int(val))
                widget.blockSignals(False)
            elif isinstance(widget, QLineEdit):
                widget.blockSignals(True)
                widget.setText(str(val))
                widget.blockSignals(False)


class SkillSettingsPane(QWidget):
    """Left-side panel showing all skill cards for enabling/configuring skills."""

    settings_changed = Signal()  # emitted whenever any skill setting changes
    skill_selected = Signal(object)  # emitted (with the skill) when one is picked

    def __init__(self, manager: SkillManager,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._cards: List[_SkillCard] = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Toolbar ---
        toolbar = QHBoxLayout()
        btn_enable_all = QPushButton("Enable All Skills")
        btn_enable_all.clicked.connect(self._enable_all)
        btn_disable_all = QPushButton("Disable All Skills")
        btn_disable_all.clicked.connect(self._disable_all)
        btn_reset = QPushButton("Reset Skill Defaults")
        btn_reset.clicked.connect(self._reset_defaults)
        for btn in (btn_enable_all, btn_disable_all, btn_reset):
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            toolbar.addWidget(btn)
        layout.addLayout(toolbar)

        # --- Scrollable skill cards ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        cards_layout = QVBoxLayout(container)
        cards_layout.setContentsMargins(4, 4, 4, 4)
        cards_layout.setSpacing(8)
        self._cards_layout = cards_layout

        self._populate_cards()

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def _populate_cards(self) -> None:
        """Create one card per skill currently known to the manager."""
        for skill in self._manager.skills:
            card = _SkillCard(skill)
            card.changed.connect(self.settings_changed.emit)
            card.selected.connect(self.skill_selected.emit)
            self._cards.append(card)
            self._cards_layout.addWidget(card)
        self._cards_layout.addStretch(1)

    def rebuild(self) -> None:
        """Rebuild all cards from the manager (e.g. after adding custom skills)."""
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards = []
        self._populate_cards()


    def _enable_all(self) -> None:
        self._manager.enable_all()
        self._refresh_all()
        self.settings_changed.emit()

    def _disable_all(self) -> None:
        self._manager.disable_all()
        self._refresh_all()
        self.settings_changed.emit()

    def _reset_defaults(self) -> None:
        self._manager.reset_defaults()
        self._refresh_all()
        self.settings_changed.emit()

    def _refresh_all(self) -> None:
        for card in self._cards:
            card.refresh()


class SkillResultsPane(QWidget):
    """Right-side panel displaying structured results from executed skills."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        layout.addWidget(self._browser)

        self._browser.setHtml(
            "<i>Run an analysis to see Skill Results here.</i>"
        )

    def clear(self) -> None:
        self._browser.setHtml(
            "<i>Run an analysis to see Skill Results here.</i>"
        )

    def show_skill_content(self, skill) -> None:
        """Render the selected *skill*'s content / guidance."""
        import html as _html

        name = _html.escape(skill.display_name or skill.skill_id)
        desc = _html.escape(skill.description or "")
        parts = [f"<h2>{name}</h2>",
                 f"<p><b>ID:</b> <code>{_html.escape(skill.skill_id)}</code>"
                 f" &nbsp; <b>Enabled:</b> {'yes' if skill.enabled else 'no'}</p>"]
        if desc:
            parts.append(f"<p>{desc}</p>")

        source = getattr(skill, "_source_path", None)
        if source:
            parts.append(f"<p><b>Source file:</b> <code>{_html.escape(str(source))}</code></p>")

        content = getattr(skill, "_content", None)
        if content:
            parts.append("<hr/><h3>Content</h3>")
            parts.append(
                "<pre style='white-space: pre-wrap; font-family: monospace;'>"
                + _html.escape(content) + "</pre>")
        else:
            schema = skill.parameters_schema()
            if schema:
                parts.append("<hr/><h3>Parameters</h3><ul>")
                for pname, pinfo in schema.items():
                    pdesc = _html.escape(pinfo.get("description", pname))
                    pval = _html.escape(str(skill.get_param(pname)))
                    parts.append(f"<li><b>{_html.escape(pname)}</b> = {pval}"
                                 f" — {pdesc}</li>")
                parts.append("</ul>")
            else:
                parts.append("<p><i>This skill has no extra content; it runs "
                             "structural analysis logic.</i></p>")
        self._browser.setHtml("".join(parts))

    def show_results(self, results: List[SkillResult]) -> None:
        """Render *results* into the browser widget."""
        if not results:
            self._browser.setHtml("<p><i>No skills were executed.</i></p>")
            return

        html = ["<h2>Skill Results</h2>"]
        for res in results:
            status = "✓" if res.success else "✗"
            color = "#2d6a2d" if res.success else "#8b0000"
            html.append(
                f"<h3 style='color:{color};'>{status} {_esc(res.skill_id)}</h3>"
            )
            if res.summary:
                html.append(f"<p><b>Summary:</b> {_esc(res.summary)}</p>")

            # Messages
            if res.messages:
                html.append("<h4>Messages</h4><ul>")
                for msg in res.messages:
                    lvl_color = {
                        "info": "#333",
                        "warning": "#b05000",
                        "error": "#8b0000",
                    }.get(msg.level, "#333")
                    html.append(
                        f"<li style='color:{lvl_color};'>"
                        f"<b>[{msg.level.upper()}]</b> {_esc(msg.text)}</li>"
                    )
                html.append("</ul>")

            # Findings
            if res.findings:
                html.append("<h4>Findings</h4>")
                for f in res.findings:
                    conf_color = {"high": "#2d6a2d",
                                  "medium": "#b05000",
                                  "low": "#555"}.get(f.confidence, "#333")
                    html.append(
                        f"<div style='border:1px solid #ccc; padding:6px; "
                        f"margin-bottom:6px; border-radius:4px;'>"
                    )
                    html.append(
                        f"<p><b>{_esc(f.title)}</b> "
                        f"<span style='color:{conf_color};font-size:11px;'>"
                        f"[{f.confidence}]</span></p>"
                    )
                    html.append(f"<p>{_esc(f.description)}</p>")
                    if f.evidence:
                        html.append(
                            "<p><b>Evidence:</b> " +
                            "; ".join(_esc(e) for e in f.evidence) + "</p>"
                        )
                    if f.affected_objects:
                        html.append(
                            "<p><b>Affected objects:</b><br><code>" +
                            "<br>".join(_esc(o) for o in f.affected_objects[:5]) +
                            ("…" if len(f.affected_objects) > 5 else "") +
                            "</code></p>"
                        )
                    if f.recommendation:
                        html.append(
                            f"<p><b>Recommendation:</b> {_esc(f.recommendation)}</p>"
                        )
                    html.append("</div>")
            else:
                html.append("<p><i>No findings.</i></p>")

            html.append("<hr/>")

        self._browser.setHtml("".join(html))


class SkillsPanel(QWidget):
    """Full Skills tab: left pane = settings, right pane = results."""

    settings_changed = Signal()

    def __init__(self, manager: SkillManager,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)

        # Left: Skill Settings
        left_box = QGroupBox("Skill Settings")
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(4, 4, 4, 4)
        self.settings_pane = SkillSettingsPane(manager)
        self.settings_pane.settings_changed.connect(self.settings_changed.emit)
        self.settings_pane.skill_selected.connect(self._on_skill_selected)
        left_layout.addWidget(self.settings_pane)
        splitter.addWidget(left_box)

        # Right: Skill Results
        right_box = QGroupBox("Skill Content / Results")
        right_layout = QVBoxLayout(right_box)
        right_layout.setContentsMargins(4, 4, 4, 4)
        self.results_pane = SkillResultsPane()
        right_layout.addWidget(self.results_pane)
        splitter.addWidget(right_box)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

    def _on_skill_selected(self, skill) -> None:
        self.results_pane.show_skill_content(skill)

    def show_results(self, results: List[SkillResult]) -> None:
        self.results_pane.show_results(results)

    def clear_results(self) -> None:
        self.results_pane.clear()


def _esc(text: str) -> str:
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
