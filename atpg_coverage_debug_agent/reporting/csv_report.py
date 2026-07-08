"""Render an :class:`AnalysisReport` per-fault table as CSV.

``pandas`` is used when present for convenience, but a pure-``csv`` fallback
keeps the tool dependency-light.
"""

from __future__ import annotations

import csv
import logging
from typing import Dict, List

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
    return {
        "fault_object": r.fault.fault_object,
        "fault_class": r.fault.fault_class.value,
        "mapped_object": r.mapping.instance_name or "",
        "mapping_confidence": r.mapping.confidence.value,
        "instance_name": r.instance_name or "",
        "cell_type": r.cell_type or "",
        "fan_in_count": str(len(r.fan_in)),
        "fan_out_count": str(len(r.fan_out)),
        "controllability_issue": "yes" if r.controllability_issue else "no",
        "observability_issue": "yes" if r.observability_issue else "no",
        "constraint_related": "yes" if r.constraint_related else "no",
        "scan_boundary_involved": "yes" if r.scan_boundary_involved else "no",
        "root_cause": r.root_cause.value,
        "evidence": " | ".join(r.evidence),
        "recommended_step": r.recommended_step,
    }


def render_rows(report: AnalysisReport) -> List[Dict[str, str]]:
    """Return a list of dict rows ready for CSV/DataFrame consumption."""
    return [_row(r) for r in report.fault_results]


def write_csv(report: AnalysisReport, path: str) -> None:
    """Write the per-fault table to *path* as CSV."""
    rows = render_rows(report)
    try:
        import pandas as pd  # type: ignore

        pd.DataFrame(rows, columns=COLUMNS).to_csv(path, index=False)
        logger.info("CSV report written via pandas to %s", path)
        return
    except Exception:
        # Fallback to the standard library.
        pass

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logger.info("CSV report written via csv module to %s", path)
