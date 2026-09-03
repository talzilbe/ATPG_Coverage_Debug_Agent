"""Per-category fault dumps: one file per selected coverage-loss category.

The triage picks a handful of categories worth debugging (``AU.TC``,
``UO.AAB``, ...) but a :class:`~..analysis.statistics.SelectedCategory` carries
only aggregate counts. To debug a category you need the faults themselves, so
this module writes a sidecar folder next to the report holding, for each
selected category:

* ``<CATEGORY>.csv`` -- every fault in the category, using the same columns as
  the full per-fault CSV, for humans and spreadsheet/pandas triage;
* ``<CATEGORY>.json`` -- the same faults with their full structural evidence
  plus the category's verdict, clusters, blocking sources and structural
  profile, for the AI agent;
* ``index.json`` -- a manifest naming every file written.

Nothing here predicts anything or rewrites a path: fault objects are copied
verbatim from the fault list so they can be pasted straight into an ATPG tool.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import AnalysisReport, FaultAnalysisResult
from .csv_report import COLUMNS, render_rows, write_rows_csv

logger = logging.getLogger(__name__)

#: Manifest format marker and version, mirroring ``session_report``.
INDEX_FORMAT = "atpg_category_faults_index"
CATEGORY_FORMAT = "atpg_category_faults"
FORMAT_VERSION = 1

#: Cap on the number of faults embedded in a category's JSON file. A single
#: constant driver can hold tens of thousands of faults, and the JSON is meant
#: to be read by an agent in one piece. The CSV is never truncated.
MAX_JSON_FAULTS = 5000

#: Characters kept in a file name derived from a subclass id.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

#: Suffix of the sidecar directory, appended to the report's base name.
DIR_SUFFIX = "_categories"


@dataclass
class CategoryDump:
    """Bookkeeping for one category's dumped files.

    File names are stored *relative* to the sidecar directory so a report can
    link to them without embedding an absolute path from the machine that
    generated it.
    """

    subclass_id: str
    rank: int
    count: int
    dumped_count: int
    dir_name: str
    csv_name: Optional[str] = None
    json_name: Optional[str] = None
    truncated: bool = False

    @property
    def csv_href(self) -> Optional[str]:
        """Report-relative link to the CSV, or ``None`` when not written."""
        return f"{self.dir_name}/{self.csv_name}" if self.csv_name else None

    @property
    def json_href(self) -> Optional[str]:
        """Report-relative link to the JSON, or ``None`` when not written."""
        return f"{self.dir_name}/{self.json_name}" if self.json_name else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subclass": self.subclass_id,
            "rank": self.rank,
            "count": self.count,
            "dumped_count": self.dumped_count,
            "dir": self.dir_name,
            "csv": self.csv_name,
            "json": self.json_name,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CategoryDump":
        """Rebuild a dump record from :meth:`as_dict` (session reload)."""
        return cls(
            subclass_id=str(data.get("subclass", "")),
            rank=int(data.get("rank", 0) or 0),
            count=int(data.get("count", 0) or 0),
            dumped_count=int(data.get("dumped_count", 0) or 0),
            dir_name=str(data.get("dir", "")),
            csv_name=data.get("csv") or None,
            json_name=data.get("json") or None,
            truncated=bool(data.get("truncated", False)),
        )


def safe_slug(subclass_id: str) -> str:
    """Return a file-name-safe form of *subclass_id*.

    Subclass ids are short tokens such as ``AU.TC``, but the value ultimately
    comes from an input file, so anything outside ``[A-Za-z0-9._-]`` (a path
    separator above all) is replaced rather than trusted.
    """
    slug = _SAFE_CHARS.sub("_", (subclass_id or "").strip())
    slug = slug.strip("._-")
    return slug or "unclassified"


def dump_dir_for(base_path: str) -> str:
    """Return the sidecar directory path for a report written to *base_path*.

    ``/tmp/atpg_report.md`` -> ``/tmp/atpg_report_categories``.
    """
    directory = os.path.dirname(os.path.abspath(base_path))
    stem = os.path.basename(base_path)
    stem = os.path.splitext(stem)[0] or "atpg_report"
    return os.path.join(directory, f"{stem}{DIR_SUFFIX}")


def faults_for_category(report: AnalysisReport,
                        subclass_id: str) -> List[FaultAnalysisResult]:
    """Return every analysed fault belonging to *subclass_id*.

    This is the single definition of "the faults in this category", shared by
    the dump writers and the ``list_category_faults`` agent tool so the files
    and the tool can never disagree.

    Args:
        report: The analysed report.
        subclass_id: A dotted category id such as ``AU.TC``, or a bare family
            such as ``AU``.

    Returns:
        The matching results, in report order. Empty when nothing matches.
    """
    wanted = (subclass_id or "").strip().upper()
    if not wanted:
        return []
    return [r for r in (report.fault_results or [])
            if _dotted_class_of(r).upper() == wanted]


def _dotted_class_of(result: Any) -> str:
    """Read a result's dotted category id, tolerating rehydrated objects."""
    from ..analysis.investigate import dotted_class_of

    return dotted_class_of(result)


def _category_payload(report: AnalysisReport, category: Any,
                      results: Sequence[FaultAnalysisResult],
                      truncated: bool,
                      csv_name: Optional[str]) -> Dict[str, Any]:
    """Build the JSON document for one category."""
    from ..analysis.investigate import serialize_fault_result

    stat = category.stat
    info = stat.info
    payload: Dict[str, Any] = {
        "format": CATEGORY_FORMAT,
        "version": FORMAT_VERSION,
        "subclass": category.subclass_id,
        "rank": category.rank,
        "title": info.title if info else None,
        "meaning": info.meaning if info else None,
        "selection_reason": category.reason,
        "counts": {
            "in_category": stat.count,
            "analysed": len(results),
            "dumped": min(len(results), MAX_JSON_FAULTS),
            "sa0": stat.sa0,
            "sa1": stat.sa1,
            "imbalance": round(stat.sa_asymmetry, 4),
        },
        "verdict": _as_dict(getattr(category, "verdict", None)),
        "clusters": _as_dict(getattr(category, "clusters", None)),
        "attribution": _as_dict(getattr(category, "attribution", None)),
        "reachability": _as_dict(getattr(category, "reachability", None)),
        "faults": [serialize_fault_result(r, full=True)
                   for r in results[:MAX_JSON_FAULTS]],
        "note": (
            "Fault objects are copied verbatim from the fault list. "
            "'in_category' counts every fault of this class in the fault "
            "list; 'analysed' counts those carried through the structural "
            "analysis. Any difference is reported, never silently dropped."
        ),
    }
    if truncated:
        payload["truncated"] = True
        payload["complete_in"] = csv_name
    return payload


def _as_dict(obj: Any) -> Optional[Dict[str, Any]]:
    """Call ``as_dict()`` when available, else return ``None``."""
    if obj is None:
        return None
    try:
        return obj.as_dict()
    except Exception:  # noqa: BLE001 - a missing serialiser must not stop a dump
        logger.debug("Object %r has no usable as_dict()", type(obj))
        return None


def write_category_dumps(report: AnalysisReport, base_path: str,
                         formats: Iterable[str] = ("csv", "json")
                         ) -> List[CategoryDump]:
    """Write one file per selected coverage-loss category beside *base_path*.

    Args:
        report: An analysed report. A report with no triage (an older session
            file, or one whose fault list held no coverage loss) writes
            nothing.
        base_path: The path of the main report; the sidecar directory is
            derived from it via :func:`dump_dir_for`.
        formats: Which formats to emit -- any of ``"csv"`` and ``"json"``.

    Returns:
        One :class:`CategoryDump` per category written, in rank order. Empty
        when there was nothing to write. The list is also recorded on
        ``report.category_dumps`` as the last known location of the files.
    """
    selected = getattr(report, "selected_categories", None) or []
    wanted = {f.strip().lower() for f in formats}
    if not selected or not wanted:
        logger.info("No selected categories to dump for %s", base_path)
        return []

    directory = dump_dir_for(base_path)
    dir_name = os.path.basename(directory)
    os.makedirs(directory, exist_ok=True)

    dumps: List[CategoryDump] = []
    used: Dict[str, int] = {}
    for category in selected:
        slug = safe_slug(category.subclass_id)
        # Two distinct ids could sanitise to the same slug; keep them apart
        # rather than letting one overwrite the other.
        seen = used.get(slug, 0)
        used[slug] = seen + 1
        if seen:
            slug = f"{slug}-{seen + 1}"

        results = faults_for_category(report, category.subclass_id)
        truncated = len(results) > MAX_JSON_FAULTS

        csv_name = f"{slug}.csv" if "csv" in wanted else None
        json_name = f"{slug}.json" if "json" in wanted else None

        if csv_name:
            write_rows_csv(render_rows(report, results),
                           os.path.join(directory, csv_name))
        if json_name:
            payload = _category_payload(report, category, results,
                                        truncated and bool(csv_name), csv_name)
            with open(os.path.join(directory, json_name), "w",
                      encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=False)

        dumps.append(CategoryDump(
            subclass_id=category.subclass_id,
            rank=category.rank,
            count=category.stat.count,
            dumped_count=len(results),
            dir_name=dir_name,
            csv_name=csv_name,
            json_name=json_name,
            truncated=truncated and bool(json_name),
        ))

    _write_index(directory, report, dumps)
    try:
        report.category_dumps = dumps
    except AttributeError:  # pragma: no cover - read-only report stand-ins
        pass
    logger.info("Wrote %d category dump(s) to %s", len(dumps), directory)
    return dumps


def _write_index(directory: str, report: AnalysisReport,
                 dumps: Sequence[CategoryDump]) -> None:
    """Write the manifest that lists every category file in *directory*."""
    stats = getattr(report, "statistics", None)
    index = {
        "format": INDEX_FORMAT,
        "version": FORMAT_VERSION,
        "columns": list(COLUMNS),
        "totals": {
            "total_faults": getattr(stats, "total_faults", None),
            "detected": getattr(stats, "detected_count", None),
            "coverage_loss": getattr(stats, "loss_count", None),
        },
        "categories": [d.as_dict() for d in dumps],
        "note": (
            "One CSV and one JSON per coverage-loss category selected for "
            "investigation. The CSV always holds every fault in the category; "
            "a JSON marked 'truncated' points at its CSV for the remainder."
        ),
    }
    with open(os.path.join(directory, "index.json"), "w",
              encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=False)
