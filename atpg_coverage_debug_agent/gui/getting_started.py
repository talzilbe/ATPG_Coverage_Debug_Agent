"""The Getting Started checklist on the Summary tab.

Each step ticks itself when the user does it; the main window decides when.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Set, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QWidget)

from . import theme

#: (id, what to do, button label)
STEPS: List[Tuple[str, str, str]] = [
    ("inputs", "Pick a netlist and a fault list (or drop the files onto "
               "the window)", "Pick files"),
    ("analyze", "Run \u25b6 Analyze (Ctrl+R) \u2014 or load a saved report",
     "Analyze"),
    ("triage", "Review the Triage & Fix Plan tab", "Open Triage"),
    ("agent", "Ask the AI Debug Agent for a diagnosis", "Open the agent"),
    ("visualizer", "Open the design in Tessent Visualizer to confirm a "
                   "finding", "Open Visualizer"),
]
STEP_IDS = [s[0] for s in STEPS]


class GettingStarted(QFrame):
    action_requested = Signal(str)
    hide_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("gettingStarted")
        self._done: Set[str] = set()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        head = QHBoxLayout()
        self.title = QLabel("")
        self.title.setTextFormat(Qt.RichText)
        head.addWidget(self.title, 1)
        self.toggle_btn = QPushButton("Show steps")
        self.toggle_btn.setFlat(True)
        self.toggle_btn.clicked.connect(self._toggle_steps)
        head.addWidget(self.toggle_btn)
        hide_btn = QPushButton("Hide checklist")
        hide_btn.setFlat(True)
        hide_btn.setToolTip("Hide it for good. Help \u2192 Getting Started "
                            "Checklist brings it back.")
        hide_btn.clicked.connect(self.hide_requested.emit)
        head.addWidget(hide_btn)
        outer.addLayout(head)
        self.steps_box = QWidget()
        grid = QGridLayout(self.steps_box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setColumnStretch(1, 1)
        self.marks = {}
        self.buttons = {}
        for row, (step_id, text, button) in enumerate(STEPS):
            mark = QLabel("")
            mark.setFixedWidth(22)
            grid.addWidget(mark, row, 0)
            grid.addWidget(QLabel(f"{row + 1}. {text}"), row, 1)
            btn = QPushButton(button)
            btn.clicked.connect(
                lambda _c=False, s=step_id: self.action_requested.emit(s))
            grid.addWidget(btn, row, 2)
            self.marks[step_id] = mark
            self.buttons[step_id] = btn
        outer.addWidget(self.steps_box)
        self._expanded = True
        self.restyle()
        self._render()

    def restyle(self) -> None:
        p = theme.palette()
        self.setStyleSheet(
            f"QFrame#gettingStarted {{ background: {p.alt_base};"
            f" border: 1px solid {p.border}; border-radius: 6px; }}")
        self._render()

    def done_steps(self) -> List[str]:
        return [s for s in STEP_IDS if s in self._done]

    def is_done(self, step_id: str) -> bool:
        return step_id in self._done

    def all_done(self) -> bool:
        return all(s in self._done for s in STEP_IDS)

    def set_done(self, step_ids: Iterable[str]) -> None:
        self._done = {s for s in step_ids if s in STEP_IDS}
        self._render()

    def mark(self, step_id: str) -> bool:
        """Tick *step_id*; True when that changed anything."""
        if step_id not in STEP_IDS or step_id in self._done:
            return False
        self._done.add(step_id)
        if self.all_done():
            self._expanded = False
        self._render()
        return True

    def _toggle_steps(self) -> None:
        self._expanded = not self._expanded
        self._render()

    def _render(self) -> None:
        n = len(self._done)
        ok = theme.color("ok")
        if self.all_done():
            self.title.setText(
                f"<b style='color:{ok};'>\u2713 You're all set</b> \u2014 every "
                "Getting Started step is done. Ctrl+Shift+P finds any command.")
        else:
            nxt = next(text for sid, text, _b in STEPS if sid not in self._done)
            self.title.setText(
                f"<b>Getting started</b> \u2014 {n} of {len(STEPS)} done. "
                f"Next: {nxt}")
        for step_id, mark in self.marks.items():
            done = step_id in self._done
            mark.setText(f"<b style='color:{ok};'>\u2713</b>" if done
                         else "\u25cb")
            self.buttons[step_id].setEnabled(not done or step_id != "analyze")
        self.steps_box.setVisible(self._expanded)
        self.toggle_btn.setText("Hide steps" if self._expanded else "Show steps")
