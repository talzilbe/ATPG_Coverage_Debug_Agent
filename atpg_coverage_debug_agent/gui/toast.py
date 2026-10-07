"""A non-blocking notice in the bottom-right corner of the main window."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from . import theme

INFO, OK, WARN, ERROR = "info", "ok", "warn", "error"
DEFAULT_MSEC = 5000


class Toast(QFrame):
    """One message at a time; a newer message replaces the current one."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setFrameShape(QFrame.NoFrame)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setMaximumWidth(420)
        row.addWidget(self.label, 1)
        self.action_btn = QPushButton("")
        self.action_btn.setVisible(False)
        self.action_btn.clicked.connect(self._on_action)
        row.addWidget(self.action_btn)
        close = QPushButton("\u00d7")
        close.setFlat(True)
        close.setFixedWidth(24)
        close.setToolTip("Dismiss")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        self._callback: Optional[Callable[[], None]] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.kind = INFO
        self.hide()

    def show_message(self, text: str, kind: str = INFO,
                     action: Optional[str] = None,
                     callback: Optional[Callable[[], None]] = None,
                     msec: int = DEFAULT_MSEC) -> None:
        self.kind = kind
        self.label.setText(text)
        self._callback = callback
        self.action_btn.setText(action or "")
        self.action_btn.setVisible(bool(action and callback))
        accent = {OK: "ok", WARN: "warn", ERROR: "err"}.get(kind, "info")
        p = theme.palette()
        self.setStyleSheet(
            f"QFrame#toast {{ background: {p.alt_base}; color: {p.text};"
            f" border: 1px solid {p.border};"
            f" border-left: 5px solid {theme.color(accent)};"
            " border-radius: 4px; }")
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(max(1000, msec))

    def _on_action(self) -> None:
        callback, self._callback = self._callback, None
        self.hide()
        if callback is not None:
            callback()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        bottom = parent.height() - 12
        status = getattr(parent, "statusBar", None)
        if callable(status) and status() is not None and status().isVisible():
            bottom -= status().height()
        self.move(max(0, parent.width() - self.width() - 16),
                  max(0, bottom - self.height()))
