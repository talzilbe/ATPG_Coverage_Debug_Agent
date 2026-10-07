"""Ctrl+Shift+P: run any command by typing part of its name (as in VS Code)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QVBoxLayout, QWidget)

#: Typing this first searches the faults of the current report instead.
FAULT_PREFIX = "@"
MAX_RESULTS = 60


@dataclass
class PaletteEntry:
    label: str
    callback: Callable[[], None]
    detail: str = ""


def _score(query: str, label: str) -> Optional[float]:
    """Higher is better; None when *query* does not match *label*."""
    q, text = query.lower(), label.lower()
    if not q:
        return 0.0
    pos = text.find(q)
    if pos >= 0:
        word_start = pos == 0 or not text[pos - 1].isalnum()
        return 1000.0 - pos + (200.0 if word_start else 0.0) - len(text) * 0.1
    # Every query character in order, fewer gaps first.
    idx, gaps, last = 0, 0, -1
    for ch in q:
        if ch == " ":
            continue
        idx = text.find(ch, idx)
        if idx < 0:
            return None
        if last >= 0 and idx != last + 1:
            gaps += 1
        last = idx
        idx += 1
    return 500.0 - gaps * 20.0 - len(text) * 0.1


def rank_entries(query: str, entries: List[PaletteEntry],
                 limit: int = MAX_RESULTS) -> List[PaletteEntry]:
    """*entries* matching *query*, best first; all of them when it is empty."""
    query = query.strip()
    if not query:
        return list(entries)[:limit]
    scored = []
    for order, entry in enumerate(entries):
        score = _score(query, entry.label)
        if score is not None:
            scored.append((-score, order, entry))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [entry for _s, _o, entry in scored[:limit]]


class CommandPalette(QFrame):
    """A popup list filtered as you type. ``provider(query)`` builds the list."""

    def __init__(self, parent: QWidget,
                 provider: Callable[[str], List[PaletteEntry]]) -> None:
        super().__init__(parent, Qt.Popup)
        self._provider = provider
        self.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(
            "Type a command, a tab or a recent design \u2014 or @ and part of "
            "a fault path")
        self.edit.textChanged.connect(self.refresh)
        self.edit.returnPressed.connect(self.run_current)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit)
        self.list = QListWidget()
        self.list.itemActivated.connect(lambda _i: self.run_current())
        layout.addWidget(self.list, 1)
        self.hint = QLabel("\u2191\u2193 move \u00b7 Enter run \u00b7 Esc close")
        layout.addWidget(self.hint)
        self._entries: List[PaletteEntry] = []

    def open(self) -> None:
        parent = self.parentWidget()
        width = min(640, max(420, parent.width() // 2))
        self.resize(width, 380)
        top_left = parent.mapToGlobal(parent.rect().topLeft())
        self.move(top_left.x() + (parent.width() - width) // 2,
                  top_left.y() + 60)
        self.edit.clear()
        self.refresh()
        self.show()
        self.edit.setFocus()

    def refresh(self, *_args) -> None:
        query = self.edit.text()
        self._entries = self._provider(query)
        self.list.clear()
        for entry in self._entries:
            text = entry.label + (f"    {entry.detail}" if entry.detail else "")
            item = QListWidgetItem(text)
            item.setToolTip(entry.label)
            self.list.addItem(item)
        if self._entries:
            self.list.setCurrentRow(0)

    def entries(self) -> List[PaletteEntry]:
        return list(self._entries)

    def run_current(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self._entries)):
            return
        entry = self._entries[row]
        self.hide()
        entry.callback()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        if obj is self.edit and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up) and self.list.count():
                step = 1 if key == Qt.Key_Down else -1
                row = (self.list.currentRow() + step) % self.list.count()
                self.list.setCurrentRow(row)
                return True
            if key == Qt.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, event)
