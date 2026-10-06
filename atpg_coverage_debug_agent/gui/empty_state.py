"""A friendly placeholder for a tab that has nothing to show yet."""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

#: Action ids every empty state may offer; the main window routes them.
DEMO, LOAD, INPUTS, ANALYZE = "demo", "load", "inputs", "analyze"

DEFAULT_ACTIONS: List[Tuple[str, str]] = [
    ("Try the demo data", DEMO),
    ("Load a saved report…", LOAD),
    ("Pick my own files", INPUTS),
]


class EmptyState(QWidget):
    """Title, one sentence and a row of buttons that say what to do next."""

    action_requested = Signal(str)

    def __init__(self, title: str, text: str,
                 actions: Optional[List[Tuple[str, str]]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        heading = QLabel(f"<h3 style='color:#0b5394;'>{title}</h3>")
        heading.setAlignment(Qt.AlignCenter)
        outer.addWidget(heading)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        body.setStyleSheet("color: #555; font-size: 13px;")
        outer.addWidget(body)
        row = QHBoxLayout()
        row.addStretch(1)
        self.buttons = {}
        for label, action in (actions if actions is not None else DEFAULT_ACTIONS):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _c=False, a=action: self.action_requested.emit(a))
            row.addWidget(btn)
            self.buttons[action] = btn
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
