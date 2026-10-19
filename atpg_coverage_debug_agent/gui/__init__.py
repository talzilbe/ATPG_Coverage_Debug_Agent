"""PySide6 desktop GUI for the ATPG coverage-loss debug agent."""

from __future__ import annotations

__all__ = ["launch"]


def launch(restore: str | None = None) -> int:
    """Launch the GUI. Imported lazily so the CLI works without PySide6.

    *restore* is a snapshot manifest handed over by a window that reopened
    itself inside Cth.ai.
    """
    from .main_window import run

    return run(restore)
