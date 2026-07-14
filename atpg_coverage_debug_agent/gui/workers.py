"""Background worker so analysis never blocks the Qt event loop."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from ..app import AnalysisInputs, analyze_paths, analyze_partitions
from ..models import AnalysisReport

logger = logging.getLogger(__name__)


class AnalysisWorker(QObject):
    """Runs :func:`analyze_paths` on a worker thread.

    Signals:
        progress: ``(done, total, message)`` progress updates.
        finished: ``(AnalysisReport)`` on success.
        failed: ``(str)`` human-readable error message on failure.
    """

    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, inputs: AnalysisInputs, skill_manager=None) -> None:
        super().__init__()
        self._inputs = inputs
        self._skill_manager = skill_manager

    def run(self) -> None:
        """Execute the analysis; emit results via signals."""
        try:
            report: AnalysisReport = analyze_paths(
                self._inputs,
                progress=lambda d, t, m: self.progress.emit(d, t, m),
                skill_manager=self._skill_manager,
            )
        except Exception as exc:  # report all errors to the UI gracefully
            logger.exception("Analysis failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(report)


def start_worker(inputs: AnalysisInputs,
                 skill_manager=None) -> tuple[QThread, AnalysisWorker]:
    """Create and start a thread running an :class:`AnalysisWorker`.

    Returns the ``(thread, worker)`` pair so the caller can keep references
    alive and connect to the worker's signals.
    """
    thread = QThread()
    worker = AnalysisWorker(inputs, skill_manager=skill_manager)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker


class MultiAnalysisWorker(QObject):
    """Runs :func:`analyze_partitions` on a worker thread.

    Signals:
        progress: ``(done, total, message)`` progress updates.
        finished: ``(list[tuple[str, AnalysisReport]])`` on success.
        failed: ``(str)`` human-readable error message on failure.
    """

    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, partitions, skill_manager=None) -> None:
        super().__init__()
        self._partitions = partitions
        self._skill_manager = skill_manager

    def run(self) -> None:
        """Execute all partition analyses; emit results via signals."""
        try:
            results = analyze_partitions(
                self._partitions,
                progress=lambda d, t, m: self.progress.emit(d, t, m),
                skill_manager=self._skill_manager,
            )
        except Exception as exc:  # report all errors to the UI gracefully
            logger.exception("Multi-partition analysis failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(results)


def start_multi_worker(partitions,
                       skill_manager=None) -> tuple[QThread, MultiAnalysisWorker]:
    """Create and start a thread running a :class:`MultiAnalysisWorker`."""
    thread = QThread()
    worker = MultiAnalysisWorker(partitions, skill_manager=skill_manager)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker
