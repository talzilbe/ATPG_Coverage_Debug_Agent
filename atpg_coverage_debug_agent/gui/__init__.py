"""PySide6 desktop GUI for the ATPG coverage-loss debug agent."""

from __future__ import annotations

__all__ = ["launch"]


def launch() -> int:
    """Launch the GUI. Imported lazily so the CLI works without PySide6."""
    from .main_window import run

    return run()
