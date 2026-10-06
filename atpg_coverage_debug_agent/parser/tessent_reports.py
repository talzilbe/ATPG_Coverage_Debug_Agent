"""Read the ATPG tool's own reports: ``report_statistics`` and ``analyze_fault``.

These are optional inputs. When present they are *measurements* by the tool
that owns the answer, so they outrank every structural estimate this analyzer
makes. Parsing is deliberately tolerant: Tessent's column layout varies between
releases and options, so rows are recognised by their shape, never by column
position, and anything unrecognised is skipped rather than guessed at.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .fault_parser import open_fault_list

#: ``AU (atpg_untestable)   12345 ( 1.23%) ...`` -- a class or subclass row.
_CLASS_ROW = re.compile(
    r"^(?P<indent>\s*)(?P<code>[A-Z]{2,4})\s+\((?P<name>[a-z_][a-z0-9_ ]*)\)"
    r"\s+(?P<count>\d[\d,]*)")

#: ``  /top/.../tdr_out_inter_reg_0 (1650303) T1      39486 ( 1.39%)``
_TIE_SOURCE_ROW = re.compile(
    r"^\s+(?P<path>/\S+)\s+\((?P<id>\d+)\)\s+(?P<value>T[01X])\s+"
    r"(?P<count>\d[\d,]*)")

#: ``  (Individual tied cells below threshold)    719 ( 0.03%)``
_TIE_BUCKET_ROW = re.compile(
    r"^\s+\((?P<label>[^)]*tied[^)]*)\)\s+(?P<count>\d[\d,]*)", re.IGNORECASE)

_METRIC_ROW = re.compile(
    r"^\s*(?P<name>test_coverage|fault_coverage|atpg_effectiveness)\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*%", re.IGNORECASE)

_AF_HEADER = re.compile(
    r"Fault analysis for (?P<inst>\S+)\s+\((?P<id>\d+)\)\s+(?P<kind>\w+)\s+"
    r"(?P<pin>\S+)\s+stuck at (?P<stuck>[01])", re.IGNORECASE)
_AF_CLASS = re.compile(r"Current fault classification\s*=\s*(?P<cls>\S+)")
_AF_DEPTH = re.compile(
    r"sequential depth:\s*Control_0\s*=\s*(?P<c0>\d+),\s*Control_1\s*=\s*"
    r"(?P<c1>\d+),\s*Observe\s*=\s*(?P<obs>\d+)", re.IGNORECASE)
_AF_OBS = re.compile(r"(?P<n>\d+)\s+potential observation points? (?:was|were) "
                     r"identified", re.IGNORECASE)
_AF_NO_OBS = re.compile(r"no potential observ", re.IGNORECASE)
_AF_ACT_OK = re.compile(r"was set to [01] successfully", re.IGNORECASE)
_AF_ACT_FAIL = re.compile(r"cannot be activated|could not be (?:set|activated)",
                          re.IGNORECASE)
_AF_STATUS = re.compile(r"ATPG status\s*=\s*(?P<status>\w+)", re.IGNORECASE)


@dataclass
class TieSourceRow:
    path: str
    value: str
    count: int


@dataclass
class StatisticsReport:
    """The parts of ``report_statistics`` this analyzer can use."""

    source_file: str = ""
    #: Top-level class code -> fault count (``AU`` -> 12345).
    classes: Dict[str, int] = field(default_factory=dict)
    #: Dotted subclass -> fault count (``AU.TC`` -> 42769).
    subclasses: Dict[str, int] = field(default_factory=dict)
    #: ``test_coverage`` / ``fault_coverage`` / ``atpg_effectiveness`` in %.
    metrics: Dict[str, float] = field(default_factory=dict)
    tie_sources: List[TieSourceRow] = field(default_factory=list)
    tie_buckets: Dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.classes or self.metrics)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_file": self.source_file,
            "classes": dict(self.classes),
            "subclasses": dict(self.subclasses),
            "metrics": dict(self.metrics),
            "tie_sources": [vars(t) for t in self.tie_sources],
            "tie_buckets": dict(self.tie_buckets),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StatisticsReport":
        return cls(
            source_file=str(data.get("source_file", "")),
            classes={str(k): int(v) for k, v in (data.get("classes") or {}).items()},
            subclasses={str(k): int(v)
                        for k, v in (data.get("subclasses") or {}).items()},
            metrics={str(k): float(v) for k, v in (data.get("metrics") or {}).items()},
            tie_sources=[TieSourceRow(**t) for t in data.get("tie_sources") or []],
            tie_buckets={str(k): int(v)
                         for k, v in (data.get("tie_buckets") or {}).items()},
        )


@dataclass
class FaultAnalysis:
    """One ``analyze_fault`` entry, reduced to the five decision fields."""

    instance: str
    pin: str
    stuck: str
    fault_class: str = ""
    control_depth: Optional[int] = None
    observe_depth: Optional[int] = None
    activatable: Optional[bool] = None
    observation_points: Optional[int] = None
    status: str = ""
    summary: List[str] = field(default_factory=list)

    @property
    def fault_object(self) -> str:
        return f"{self.instance}/{self.pin}"

    def as_dict(self) -> Dict[str, Any]:
        data = dict(vars(self))
        data["summary"] = list(self.summary)
        data["fault_object"] = self.fault_object
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FaultAnalysis":
        keys = {"instance", "pin", "stuck", "fault_class", "control_depth",
                "observe_depth", "activatable", "observation_points", "status",
                "summary"}
        return cls(**{k: v for k, v in data.items() if k in keys})


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def parse_report_statistics(text: str, source_file: str = "") -> StatisticsReport:
    """Pull class counts, coverage metrics and tie sources out of the text."""
    report = StatisticsReport(source_file=source_file)
    parent: Optional[Tuple[str, int]] = None
    in_tc = False
    for line in text.splitlines():
        body = line.lstrip("/ ") if line.lstrip().startswith("//") else line
        metric = _METRIC_ROW.match(body)
        if metric:
            report.metrics[metric.group("name").lower()] = float(
                metric.group("value"))
            continue
        row = _CLASS_ROW.match(body)
        if row:
            indent = len(row.group("indent").expandtabs())
            code = row.group("code")
            count = _int(row.group("count"))
            if parent is not None and indent > parent[1]:
                report.subclasses[f"{parent[0]}.{code}"] = count
                in_tc = code == "TC"
            else:
                parent = (code, indent)
                report.classes.setdefault(code, count)
                in_tc = False
            continue
        if in_tc:
            tie = _TIE_SOURCE_ROW.match(body)
            if tie:
                report.tie_sources.append(TieSourceRow(
                    tie.group("path"), tie.group("value"),
                    _int(tie.group("count"))))
                continue
            bucket = _TIE_BUCKET_ROW.match(body)
            if bucket:
                report.tie_buckets[bucket.group("label")] = _int(
                    bucket.group("count"))
    return report


def parse_analyze_fault(text: str) -> List[FaultAnalysis]:
    """Split an ``analyze_fault`` log into one record per analysed fault."""
    entries: List[FaultAnalysis] = []
    current: Optional[FaultAnalysis] = None
    in_summary = False
    for raw in text.splitlines():
        line = raw.lstrip("/").strip()
        head = _AF_HEADER.search(line)
        if head:
            current = FaultAnalysis(instance=head.group("inst"),
                                    pin=head.group("pin"),
                                    stuck=head.group("stuck"))
            entries.append(current)
            in_summary = False
            continue
        if current is None or not line or set(line) <= {"-"}:
            continue
        if line.lower().startswith("analysis summary"):
            in_summary = True
            continue
        if in_summary:
            current.summary.append(line)
            continue
        match = _AF_CLASS.search(line)
        if match:
            current.fault_class = match.group("cls")
            continue
        match = _AF_DEPTH.search(line)
        if match:
            current.control_depth = max(int(match.group("c0")),
                                        int(match.group("c1")))
            current.observe_depth = int(match.group("obs"))
            continue
        if _AF_ACT_OK.search(line):
            current.activatable = True
        elif _AF_ACT_FAIL.search(line):
            current.activatable = False
        match = _AF_OBS.search(line)
        if match:
            current.observation_points = int(match.group("n"))
        elif _AF_NO_OBS.search(line):
            current.observation_points = 0
        match = _AF_STATUS.search(line)
        if match:
            current.status = match.group("status").lower()
    return entries


def read_tool_reports(path: str) -> Tuple[List[StatisticsReport],
                                          List[FaultAnalysis], List[str]]:
    """Read a report file, or every ``.log``/``.txt``/``.rpt`` in a directory.

    Returns (statistics reports, analyze_fault entries, warnings). A file that
    is neither kind is reported in the warnings, never silently ignored.
    """
    files: List[str] = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isfile(full) and re.search(
                    r"\.(log|txt|rpt|out)(\.gz)?$", name, re.IGNORECASE):
                files.append(full)
    elif os.path.isfile(path):
        files.append(path)
    else:
        return [], [], [f"Tool report path not found: {path}"]

    stats: List[StatisticsReport] = []
    analyses: List[FaultAnalysis] = []
    warnings: List[str] = []
    for file_path in files:
        try:
            with open_fault_list(file_path) as fh:
                text = fh.read()
        except (OSError, EOFError, ValueError) as exc:
            warnings.append(f"Could not read tool report {file_path}: {exc}")
            continue
        found = False
        entries = parse_analyze_fault(text)
        if entries:
            analyses.extend(entries)
            found = True
        report = parse_report_statistics(text, file_path)
        if not report.empty:
            stats.append(report)
            found = True
        if not found:
            warnings.append(
                f"{os.path.basename(file_path)}: no report_statistics table or "
                f"analyze_fault entry recognised; file ignored.")
    if not files:
        warnings.append(f"No report files (.log/.txt/.rpt/.out) in {path}.")
    return stats, analyses, warnings
