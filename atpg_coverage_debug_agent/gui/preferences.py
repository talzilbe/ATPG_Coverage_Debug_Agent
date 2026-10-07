"""Preferences: one place for appearance and everyday behaviour."""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QLabel, QSpinBox, QVBoxLayout,
                               QWidget)

from . import theme

_THEME_LABELS = [("Light", theme.LIGHT), ("Dark", theme.DARK)]


class PreferencesDialog(QDialog):
    def __init__(self, values: Dict[str, Any],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.theme_combo = QComboBox()
        for label, name in _THEME_LABELS:
            self.theme_combo.addItem(label, name)
        idx = self.theme_combo.findData(values.get("theme", theme.LIGHT))
        self.theme_combo.setCurrentIndex(max(0, idx))
        form.addRow("Theme:", self.theme_combo)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(theme.MIN_FONT_DELTA, theme.MAX_FONT_DELTA)
        self.font_spin.setSuffix(" pt")
        self.font_spin.setValue(theme.clamp_font_delta(values.get("font_delta", 0)))
        self.font_spin.setToolTip("Added to the normal text size "
                                  "(Ctrl+= / Ctrl+- / Ctrl+0 do the same).")
        form.addRow("Text size:", self.font_spin)
        self.autosave_check = QCheckBox("Save the JSON report after every Analyze")
        self.autosave_check.setChecked(bool(values.get("auto_save_report")))
        form.addRow("Auto-save:", self.autosave_check)
        self.advanced_check = QCheckBox("Show the Logs, Skills and Custom "
                                        "Skills tabs")
        self.advanced_check.setChecked(bool(values.get("show_advanced_tabs")))
        form.addRow("Advanced tabs:", self.advanced_check)
        self.checklist_check = QCheckBox("Show the Getting Started checklist "
                                         "on the Summary tab")
        self.checklist_check.setChecked(bool(values.get("show_checklist", True)))
        form.addRow("Checklist:", self.checklist_check)
        self.tour_check = QCheckBox("Show the guided tour again after the next "
                                    "analysis")
        self.tour_check.setChecked(not values.get("tour_done", False))
        form.addRow("Guided tour:", self.tour_check)
        v.addLayout(form)
        note = QLabel("<i>Everything here is saved per user in "
                      "~/.atpg_debug_agent/settings.json.</i>")
        note.setWordWrap(True)
        v.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def values(self) -> Dict[str, Any]:
        return {
            "theme": self.theme_combo.currentData(),
            "font_delta": self.font_spin.value(),
            "auto_save_report": self.autosave_check.isChecked(),
            "show_advanced_tabs": self.advanced_check.isChecked(),
            "show_checklist": self.checklist_check.isChecked(),
            "tour_done": not self.tour_check.isChecked(),
        }
