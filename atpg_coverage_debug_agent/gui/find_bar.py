"""A slim find bar for the read-only text views (Summary, Triage, AI agent)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTextBrowser, QWidget)


class FindBar(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._target: Optional[QTextBrowser] = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.addWidget(QLabel("Find:"))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(
            "text to find (Enter = next, Shift+Enter = previous, Esc = close)")
        self.edit.returnPressed.connect(self._on_return)
        self.edit.textChanged.connect(lambda _t: self._find(restart=True))
        row.addWidget(self.edit, 1)
        prev_btn = QPushButton("\u2191 Previous")
        prev_btn.clicked.connect(self.find_previous)
        row.addWidget(prev_btn)
        next_btn = QPushButton("\u2193 Next")
        next_btn.clicked.connect(self.find_next)
        row.addWidget(next_btn)
        self.result_label = QLabel("")
        row.addWidget(self.result_label)
        close_btn = QPushButton("\u00d7")
        close_btn.setFlat(True)
        close_btn.setFixedWidth(24)
        close_btn.setToolTip("Close (Esc)")
        close_btn.clicked.connect(self.close_bar)
        row.addWidget(close_btn)
        self.hide()

    def open_for(self, target: QTextBrowser, where: str = "") -> None:
        self._target = target
        self.edit.setToolTip(f"Searching: {where}" if where else "")
        self.result_label.setText(where)
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()

    def close_bar(self) -> None:
        self.hide()
        if self._target is not None:
            self._target.setFocus()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(event)

    def _on_return(self) -> None:
        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            self.find_previous()
        else:
            self.find_next()

    def find_next(self) -> bool:
        return self._find()

    def find_previous(self) -> bool:
        return self._find(backward=True)

    def _find(self, backward: bool = False, restart: bool = False) -> bool:
        target, text = self._target, self.edit.text()
        if target is None or not text:
            self.result_label.setText("")
            return False
        if restart:
            target.moveCursor(QTextCursor.Start)
        flags = QTextDocument.FindBackward if backward else \
            QTextDocument.FindFlag(0)
        found = target.find(text, flags)
        if not found:
            # Wrap around once.
            target.moveCursor(QTextCursor.End if backward
                              else QTextCursor.Start)
            found = target.find(text, flags)
        self.result_label.setText("" if found else "not found")
        return found
