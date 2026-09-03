"""Render an :class:`AnalysisReport` per-fault table as CSV.

``pandas`` is used when present for convenience, but a pure-``csv`` fallback
keeps the tool dependency-light.
"""

from __future__ import annotations

import csv
import logging
from typing import Any, Dict, Iterable, List, Optional

from ..models import AnalysisReport, FaultAnalysisResult

logger = logging.getLogger(__name__)

COLUMNS = [
    "fault_object",
    "fault_class",
    "mapped_object",
    "mapping_confidence",
    "instance_name",
    "cell_type",
    "fan_in_count",
    "fan_out_count",
    "controllability_issue",
    "observability_issue",
    "constraint_related",
    "scan_boundary_involved",
    "root_cause",
    "evidence",
    "recommended_step",
]


def _row(r: FaultAnalysisResult) -> Dict[str, str]:
    # Unmapped objects carry no connectivity information; their fan-in/out
    # cells are left EMPTY (NULL) rather than 0, and the scan column reads
    # 'unknown' rather than 'no'.
    fan_in = r.fan_in_count
    fan_out = r.fan_out_count
    return {
        "fault_object": r.fault.fault_object,
        "fault_class": r.fault.fault_class.value,
        "mapped_object": r.mapping.instance_name or "",
        "mapping_confidence": r.mapping.confidence.value,
        "instance_name": r.instance_name or "",
        "cell_type": r.cell_type or "",
        "fan_in_count": "" if fan_in is None else str(fan_in),
        "fan_out_count": "" if fan_out is None else str(fan_out),
        "controllability_issue": "yes" if r.controllability_issue else "no",
        "observability_issue": "yes" if r.observability_issue else "no",
        "constraint_related": "yes" if r.constraint_related else "no",
        "scan_boundary_involved": r.scan_boundary_state,
        "root_cause": r.root_cause.value,
        "evidence": " | ".join(r.evidence),
        "recommended_step": r.recommended_step,
    }


def render_rows(report: AnalysisReport,
                results: Optional[Iterable[Any]] = None) -> List[Dict[str, str]]:
    """Return a list of dict rows ready for CSV/DataFrame consumption.

    Args:
        report: The analysed report.
        results: An explicit subset of ``FaultAnalysisResult`` objects, for
            example the faults of a single coverage-loss category. Defaults to
            every result in *report*.

    Returns:
        One dict per fault, keyed by :data:`COLUMNS`.
    """
    chosen = report.fault_results if results is None else results
    return [_row(r) for r in chosen]


def write_rows_csv(rows: Iterable[Dict[str, str]], path: str) -> None:
    """Write pre-rendered *rows* to *path* using :data:`COLUMNS` as the header.

    ``pandas`` is used when importable, with a standard-library fallback so the
    column set is defined in exactly one place either way.
    """
    rows = list(rows)
    try:
        import pandas as pd  # type: ignore

        pd.DataFrame(rows, columns=COLUMNS).to_csv(path, index=False)
        logger.info("CSV written via pandas to %s", path)
        return
    except Exception:
        # Fallback to the standard library.
        pass

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info("CSV written via csv module to %s", path)


def write_csv(report: AnalysisReport, path: str) -> None:
    """Write the per-fault table to *path* as CSV."""
    write_rows_csv(render_rows(report), path)
