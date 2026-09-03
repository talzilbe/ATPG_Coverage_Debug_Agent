"""Render an :class:`AnalysisReport` as a print-style DFT/ATPG debug document.

The layout mirrors the ``gpio_north_l_coverage_debug_report.html`` reference: a
cover block, then eight numbered sections (constraint analysis, fault
statistics, coverage triage, the fix plan, module hotspots, per-root-cause
boxes, a coverage-loss summary table, and conclusions / recommended actions).

The markup is intentionally simple — headings, plain ``<table>``s, inline-styled
callout boxes and ``<span>`` badges — so it renders faithfully in **both** a real
web browser (where the ``<style>`` block adds rounded corners / left borders) and
Qt's built-in rich-text engine (``QTextBrowser``), which honours background
colours and table attributes but ignores the fancier CSS.
"""

from __future__ import annotations

import html
import logging
import os
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import (
    AnalysisReport,
    FaultAnalysisResult,
    RootCause,
)

logger = logging.getLogger(__name__)

#: Placeholder for an empty cell. Kept as a constant because Python 3.11
#: forbids backslash escapes inside f-string expressions.
_DASH = "\u2014"


# ---------------------------------------------------------------------------
# Stylesheet (browser polish; Qt ignores the parts it does not understand)
# ---------------------------------------------------------------------------
_STYLE = """
  * { box-sizing: border-box; }
  body { font-family: Arial, Helvetica, sans-serif; font-size: 11pt; color: #1a1a1a; background: #fff; }
  h1 { font-size: 20pt; color: #003366; border-bottom: 3px solid #003366; padding-bottom: 6px; margin-bottom: 12px; }
  h2 { font-size: 14pt; color: #003366; border-left: 5px solid #0055aa; padding-left: 8px; margin: 24px 0 8px; }
  h3 { font-size: 11pt; color: #444; margin: 14px 0 4px; }
  p  { margin: 4px 0 8px; line-height: 1.5; }
  code { font-family: "Courier New", monospace; font-size: 9.5pt; background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }
  pre  { font-family: "Courier New", monospace; font-size: 8.5pt; background: #f6f6f6; border: 1px solid #ddd; border-radius: 4px; padding: 10px 14px; margin: 6px 0 12px; white-space: pre-wrap; word-break: break-all; line-height: 1.4; }
  .page { max-width: 980px; margin: 0 auto; padding: 28px 36px; }
  .cover { text-align: center; padding: 40px 40px 24px; }
  .cover h1 { border: none; font-size: 24pt; margin-bottom: 8px; }
  .cover .sub { font-size: 13pt; color: #555; margin-bottom: 4px; }
  .divider { border: none; border-top: 1px solid #ccc; margin: 18px 0; }
  table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 9.5pt; }
  th { background: #003366; color: #fff; padding: 6px 10px; text-align: left; font-weight: bold; }
  td { padding: 5px 10px; border-bottom: 1px solid #ddd; vertical-align: top; }
  .num { text-align: right; font-family: monospace; font-weight: bold; }
  .info  { background:#e8f4fd; border-left:4px solid #2196F3; padding:8px 14px; margin:8px 0; border-radius:3px; }
  .warn  { background:#fff8e1; border-left:4px solid #FFC107; padding:8px 14px; margin:8px 0; border-radius:3px; }
  .error { background:#fdecea; border-left:4px solid #f44336; padding:8px 14px; margin:8px 0; border-radius:3px; }
  .ok    { background:#e8f5e9; border-left:4px solid #4CAF50; padding:8px 14px; margin:8px 0; border-radius:3px; }
  .ds  { color: #1a7f37; font-weight: bold; }
  .au  { color: #c0392b; font-weight: bold; }
  .uo  { color: #e67e22; font-weight: bold; }
  .di  { color: #2980b9; }
  .bb  { color: #7f8c8d; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:8.5pt; font-weight:bold; }
  .badge-red  { background:#fdecea; color:#c0392b; border:1px solid #f44336; }
  .badge-warn { background:#fff8e1; color:#7f6000; border:1px solid #FFC107; }
  .badge-ok   { background:#e8f5e9; color:#1a7f37; border:1px solid #4CAF50; }
  .badge-gray { background:#f5f5f5; color:#555; border:1px solid #ccc; }
  .rc-box { background:#fff; border:2px solid #003366; border-radius:6px; padding:14px 18px; margin:12px 0; }
  .rc-box h3 { color:#003366; margin-bottom:6px; }
"""

# Human-readable labels for each root-cause category.
_RC_LABELS: Dict[RootCause, str] = {
    RootCause.CONSTRAINT_CONTROLLABILITY: "Constraint-Induced Controllability Loss",
    RootCause.CONSTRAINT_OBSERVABILITY: "Constraint-Induced Observability Loss",
    RootCause.SCAN_TO_NON_SCAN: "Scan-to-Non-Scan Boundary",
    RootCause.NON_SCAN_PROPAGATION: "Non-Scan Block Propagation Loss",
    RootCause.TIED_OR_CONSTANT: "Tied / Constant Hardware",
    RootCause.CLOCK_RESET_TE_BLOCKING: "Clock / Reset / Test-Enable Blocking",
    RootCause.STRUCTURAL_MASKING: "Structural Masking / Reconvergence",
    RootCause.UNRESOLVED_CONNECTIVITY: "Unresolved Connectivity",
    RootCause.OTHER_STRUCTURAL: "Other Structural Cause",
}

# Per-class metadata: meaning + (badge text, badge css class).
_CLASS_META: Dict[str, Tuple[str, str, str]] = {
    "DS": ("Detected by scan pattern (simulation)", "\u2713 Detected", "badge-ok"),
    "DI": ("Detected by implication", "\u2713 Detected", "badge-ok"),
    "TI": ("Tied / logically constant", "Excluded", "badge-gray"),
    "AU": ("ATPG untestable \u2014 no test possible", "\u2717 Coverage loss", "badge-red"),
    "UO": ("Unobservable \u2014 cannot observe at a PO", "\u2717 Coverage loss", "badge-red"),
    "UC": ("Uncontrollable \u2014 cannot drive fault site", "\u2717 Coverage loss", "badge-red"),
    "UNKNOWN": ("Unrecognised fault-class token", "Excluded", "badge-gray"),
}

# Map a fault-class value to the coloured-text CSS class used in tables.
_CLASS_CSS: Dict[str, str] = {
    "DS": "ds", "DI": "di", "TI": "bb",
    "AU": "au", "UO": "uo", "UC": "au", "UNKNOWN": "bb",
}

_DETECTED = ("DS", "DI")
_LOSS = ("AU", "UO", "UC")

# Golden-report display order for fault sub-classes.
_SUBTYPE_ORDER = [
    "DS", "DI.CLK", "DI.SCAN", "DI.SEN", "DI.DIN", "DI.SR",
    "TI", "UU", "AU.SEQ", "AU.TC", "AU.BB", "AU", "AU.UDN",
    "UO.AAB", "AU.PC", "PU", "RE", "BL", "PT",
]

# Human-readable meaning for each sub-class token (fallback derived per base).
_SUBTYPE_MEANING: Dict[str, str] = {
    "DS": "Detected by scan pattern (simulation)",
    "DI.CLK": "Detected by implication \u2014 clock logic",
    "DI.SCAN": "Detected by implication \u2014 scan chain",
    "DI.SEN": "Detected by implication \u2014 scan enable",
    "DI.DIN": "Detected by implication \u2014 data input",
    "DI.SR": "Detected by implication \u2014 set / reset",
    "TI": "Tied / logically constant value",
    "UU": "Unused \u2014 outside the active scan model",
    "AU.SEQ": "ATPG untestable \u2014 sequential depth / state",
    "AU.TC": "ATPG untestable \u2014 tied constraint blocks test",
    "AU.BB": "ATPG untestable \u2014 black-box boundary",
    "AU": "ATPG untestable \u2014 no test possible",
    "AU.UDN": "ATPG untestable \u2014 undriven net",
    "UO.AAB": "Unobservable \u2014 asynchronous boundary",
    "AU.PC": "ATPG untestable \u2014 pin constraint",
    "PU": "Potentially detectable / unused",
    "RE": "Redundant fault",
    "BL": "Blocked fault",
    "PT": "Possibly testable (uncertain)",
}


def _subtype_impact(token: str) -> Tuple[str, str, str]:
    """Return (badge_text, badge_css, bucket) for a fault sub-class token.

    ``bucket`` is one of ``detected`` / ``loss`` / ``excluded``.  ``AU.BB`` is
    treated as an excluded black-box boundary (matching the reference report).
    """
    upper = token.upper()
    base = upper.split(".", 1)[0]
    if base == "DS" or base == "DI":
        return ("\u2713 Detected", "badge-ok", "detected")
    if upper == "AU.BB":
        return ("Black-box boundary", "badge-warn", "excluded")
    if base in ("AU", "UO", "UC"):
        return ("\u2717 Coverage loss", "badge-red", "loss")
    return ("Excluded", "badge-gray", "excluded")


def _coverage_buckets(summary) -> Tuple[int, int, int, float]:
    """Return ``(detected, loss, bb, coverage_pct)`` from the summary.

    ``loss`` excludes the ``AU.BB`` black-box boundary, matching the reference
    coverage definition ``detected / (detected + loss)``.
    """
    counts = dict(summary.subtype_counts) if summary.subtype_counts \
        else dict(summary.class_counts)
    detected = sum(n for tok, n in counts.items()
                   if _subtype_impact(tok)[2] == "detected")
    loss = sum(n for tok, n in counts.items()
               if _subtype_impact(tok)[2] == "loss")
    bb = counts.get("AU.BB", 0)
    denom = detected + loss
    cov = (100.0 * detected / denom) if denom else 0.0
    return detected, loss, bb, cov


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _esc(text: object) -> str:
    return html.escape(str(text)) if text is not None else ""


def _fmt(n: int) -> str:
    return f"{int(n):,}"


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "0.0%"
    v = 100.0 * part / whole
    return "&lt;0.1%" if 0 < v < 0.1 else f"{v:.1f}%"


def _callout(kind: str, body: str) -> str:
    """Render an info/warn/error/ok callout box (class + inline fallback)."""
    styles = {
        "info": ("#e8f4fd", "#2196F3"),
        "warn": ("#fff8e1", "#FFC107"),
        "error": ("#fdecea", "#f44336"),
        "ok": ("#e8f5e9", "#4CAF50"),
    }
    bg, accent = styles.get(kind, styles["info"])
    return (
        f'<div class="{kind}" style="background:{bg}; '
        f'border-left:4px solid {accent}; padding:8px 14px; margin:8px 0;">'
        f"{body}</div>"
    )


def _badge(text: str, css: str) -> str:
    colors = {
        "badge-red": ("#fdecea", "#c0392b"),
        "badge-warn": ("#fff8e1", "#7f6000"),
        "badge-ok": ("#e8f5e9", "#1a7f37"),
        "badge-gray": ("#f5f5f5", "#555555"),
    }
    bg, fg = colors.get(css, colors["badge-gray"])
    return (
        f'<span class="badge {css}" style="background:{bg}; color:{fg}; '
        f'padding:2px 8px; font-size:8.5pt; font-weight:bold;">{_esc(text)}</span>'
    )


def _class_span(cls: str, count: int) -> str:
    css = _CLASS_CSS.get(cls, "bb")
    return f'<span class="{css}">{_esc(cls)}: {_fmt(count)}</span>'


def _short_path(path: str, keep: int = 4) -> str:
    if not path:
        return "\u2014"
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if len(parts) <= keep:
        return "/".join(parts)
    return ".../" + "/".join(parts[-keep:])


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def _group_by_root_cause(
    report: AnalysisReport,
) -> "OrderedDict[RootCause, List[FaultAnalysisResult]]":
    return _group_by_root_cause_of(report.fault_results)


def _group_by_root_cause_of(
    results: List[FaultAnalysisResult],
) -> "OrderedDict[RootCause, List[FaultAnalysisResult]]":
    groups: "OrderedDict[RootCause, List[FaultAnalysisResult]]" = OrderedDict()
    for r in results:
        groups.setdefault(r.root_cause, []).append(r)
    return OrderedDict(
        sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    )


def _representative(results: List[FaultAnalysisResult]) -> FaultAnalysisResult:
    return max(
        results,
        key=lambda r: (
            len(r.observed_facts) + len(r.inferred_conclusions),
            len(r.fan_in) + len(r.fan_out),
        ),
    )


def _group_module(results: List[FaultAnalysisResult]) -> str:
    insts = Counter(r.instance_name for r in results if r.instance_name)
    if insts:
        return insts.most_common(1)[0][0]
    objs = Counter(r.fault.fault_object for r in results)
    return objs.most_common(1)[0][0] if objs else "\u2014"


def _group_classes(results: List[FaultAnalysisResult]) -> str:
    counts = Counter(r.fault.fault_class.value for r in results)
    return " &nbsp; ".join(
        _class_span(cls, n) for cls, n in counts.most_common()
    )


def _guess_design_name(report: AnalysisReport) -> str:
    prefixes: Counter = Counter()
    for r in report.fault_results:
        inst = r.instance_name or ""
        if inst:
            prefixes[inst.split("/")[0].split(".")[0]] += 1
    if prefixes:
        return prefixes.most_common(1)[0][0]
    if report.summary.top_instances:
        return str(report.summary.top_instances[0][0])
    return "design"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _cover(
    design: str,
    netlist_path: Optional[str],
    faults_path: Optional[str],
    constraints_path: Optional[str],
) -> str:
    def _row(label: str, value: str, mono: bool = False) -> str:
        vstyle = (
            "padding:4px 20px; border:none;"
            + (" font-family:monospace; font-size:8.5pt;" if mono else "")
        )
        return (
            '<tr><td style="padding:4px 20px; text-align:right; color:#555; '
            f'border:none;">{_esc(label)}:</td>'
            f'<td style="{vstyle}">{value}</td></tr>'
        )

    nl = os.path.basename(netlist_path) if netlist_path else "(not provided)"
    fl = os.path.basename(faults_path) if faults_path else "(not provided)"
    cn = os.path.basename(constraints_path) if constraints_path else "(none)"
    date = datetime.now().strftime("%B %d, %Y")

    return (
        '<div class="cover" style="text-align:center;">'
        "<h1>DFT/ATPG Coverage Debug Report</h1>"
        '<p class="sub" style="color:#555;">Gate-Level Stuck-At Fault Coverage '
        "Analysis</p>"
        f'<p class="sub" style="color:#555;"><code>{_esc(design)}</code></p>'
        '<hr class="divider"/>'
        '<table style="width:auto; margin:0 auto; font-size:10pt; border:none;">'
        + _row("Design", f"<b>{_esc(design)}</b>")
        + _row("ATPG run", "EDT stuck-at fault analysis")
        + _row("Fault list", _esc(fl), mono=True)
        + _row("Netlist", _esc(nl), mono=True)
        + _row("Constraints", _esc(cn), mono=True)
        + _row("Date", _esc(date))
        + _row("Skill", "<code>dft-atpg-debug</code>")
        + "</table></div>"
        '<hr class="divider"/>'
    )


def _section_constraints(report: AnalysisReport, constraints_path: Optional[str]) -> str:
    parts = ["<h2>1. ATPG Constraint Analysis</h2>"]
    top = report.summary.top_constraints
    if top:
        lines = "\n".join(
            f"{i:>2}. {key}  (x{count})"
            for i, (key, count) in enumerate(top, start=1)
        )
        parts.append(f"<pre>{_esc(lines)}</pre>")
        parts.append(
            _callout(
                "warn",
                "<b>Key observation:</b> The constraints above are correlated "
                "with coverage-loss faults. Review whether any forced value "
                "blocks controllability or observability at the fault sites in "
                "&sect;7.",
            )
        )
    elif constraints_path:
        parts.append(
            f"<p>Constraint file: <code>{_esc(os.path.basename(constraints_path))}"
            "</code></p>"
        )
        parts.append(
            _callout(
                "info",
                "A constraint file was supplied but no individual constraint "
                "was linked to a specific coverage-loss fault. The dominant "
                "causes are therefore structural (see &sect;7).",
            )
        )
    else:
        parts.append(
            _callout(
                "info",
                "<b>No constraint file analysed.</b> Constraint-related "
                "diagnoses are disabled; all coverage loss below is attributed "
                "to structural causes. Re-run with the ATPG <code>.do</code> "
                "dofile to enable constraint correlation.",
            )
        )
    return "".join(parts)


def _section_fault_stats(report: AnalysisReport) -> str:
    s = report.summary
    total = s.total_faults or 0
    # Prefer the sub-class breakdown (e.g. AU.TC, DI.CLK); fall back to coarse.
    counts = dict(s.subtype_counts) if s.subtype_counts else dict(s.class_counts)

    seen = [c for c in _SUBTYPE_ORDER if counts.get(c)]
    for c in sorted(counts, key=lambda k: counts[k], reverse=True):
        if c not in seen and counts.get(c):
            seen.append(c)

    rows = []
    for cls in seen:
        n = counts.get(cls, 0)
        btxt, bcss, _bucket = _subtype_impact(cls)
        base = cls.split(".", 1)[0].upper()
        css = _CLASS_CSS.get(base, "bb")
        meaning = _SUBTYPE_MEANING.get(
            cls, _CLASS_META.get(base, ("Other fault class", "", ""))[0]
        )
        rows.append(
            f'<tr><td class="{css}">{_esc(cls)}</td>'
            f'<td class="num {css}">{_fmt(n)}</td>'
            f'<td class="num">{_pct(n, total)}</td>'
            f"<td>{_esc(meaning)}</td>"
            f"<td>{_badge(btxt, bcss)}</td></tr>"
        )

    stats_table = (
        "<table><tr><th>Fault Class</th><th>Count</th><th>% of Total</th>"
        "<th>Meaning</th><th>Coverage Impact</th></tr>" + "".join(rows) + "</table>"
    )

    # Bucket totals from the sub-class breakdown so AU.BB is excluded from loss.
    detected, loss, bb, cov = _coverage_buckets(s)

    bb_row = (
        f'<tr><td style="padding:6px 16px;">Black-box boundary (AU.BB, excluded)</td>'
        f'<td class="num" style="padding:6px 16px; color:#7f6000;">{_fmt(bb)}</td></tr>'
        if bb else ""
    )

    metric_table = (
        '<table style="width:auto; margin-top:10px;">'
        '<tr><th colspan="2">Coverage Metric</th></tr>'
        f'<tr><td style="padding:6px 16px;"><b>Total faults in list</b></td>'
        f'<td class="num" style="padding:6px 16px;">{_fmt(total)}</td></tr>'
        f'<tr><td style="padding:6px 16px;">Total detected (DS + DI.*)</td>'
        f'<td class="num" style="padding:6px 16px; color:#1a7f37;">{_fmt(detected)}</td></tr>'
        f'<tr><td style="padding:6px 16px;">Total coverage-loss (AU* + UO*, excl. AU.BB)</td>'
        f'<td class="num" style="padding:6px 16px; color:#c0392b;">{_fmt(loss)}</td></tr>'
        + bb_row +
        '<tr style="background:#e8f5e9;">'
        '<td style="padding:8px 16px;"><b>Estimated structural coverage</b></td>'
        f'<td class="num" style="padding:8px 16px; font-size:13pt; color:#1a7f37;">'
        f"<b>~{cov:.1f}%</b></td></tr>"
        "</table>"
    )

    callout = ""
    if loss:
        callout = _callout(
            "error",
            f"<b>{_fmt(loss)} coverage-loss fault(s)</b> were identified "
            f"(~{100.0 * loss / total if total else 0:.1f}% of the list). "
            "How much of that rests on evidence actually read is broken down "
            "in &sect;3; the dominant contributors are root-caused "
            "individually in &sect;7.",
        )

    return (
        "<h2>2. Fault Statistics Summary</h2>" + stats_table + metric_table + callout
    )


def _section_evidence(report: AnalysisReport) -> str:
    """Render how much of the coverage loss rests on evidence actually read.

    This section exists because a published analysis once called a scan flop
    non-scan on the strength of a row reading ``fanin=0, fanout=0, scan=N``,
    where those values only meant the object had never been located. Unmapped
    rows and tie-driven faults are therefore counted out in the open, before
    any priority ranking is presented.
    """
    s = report.summary
    heading = "<h2>3. Evidence Quality &amp; Scan Evidence</h2>"
    loss = s.coverage_loss_count or 0
    if not loss:
        return heading + _callout(
            "ok", "No coverage-loss faults were analysed in this run.")

    mapped = s.mapped_count
    unmapped = s.unmapped_count
    tied = s.tied_constant_count
    actionable = s.actionable_loss_count
    scan = dict(s.scan_evidence_counts or {})

    basis = (
        '<table style="width:auto;">'
        '<tr><th colspan="3">Coverage-loss faults by evidence basis</th></tr>'
        "<tr><th>Basis</th><th>Count</th><th>% of loss</th></tr>"
        f'<tr><td>Mapped onto the netlist (evidence available)</td>'
        f'<td class="num">{_fmt(mapped)}</td>'
        f'<td class="num">{_pct(mapped, loss)}</td></tr>'
        f'<tr><td style="color:#7f6000;">Not mapped &mdash; connectivity '
        f'<b>unknown</b>, not zero</td>'
        f'<td class="num" style="color:#7f6000;">{_fmt(unmapped)}</td>'
        f'<td class="num">{_pct(unmapped, loss)}</td></tr>'
        f'<tr><td>Held at a hard constant (tied, expected)</td>'
        f'<td class="num">{_fmt(tied)}</td>'
        f'<td class="num">{_pct(tied, loss)}</td></tr>'
        '<tr style="background:#e8f5e9;">'
        "<td><b>Actionable coverage loss</b> (mapped, not tied)</td>"
        f'<td class="num"><b>{_fmt(actionable)}</b></td>'
        f'<td class="num"><b>{_pct(actionable, loss)}</b></td></tr>'
        "</table>"
    )

    scan_rows = "".join(
        f"<tr><td>{_esc(_SCAN_LABEL.get(k, k))}</td>"
        f'<td class="num">{_fmt(v)}</td>'
        f'<td class="num">{_pct(v, loss)}</td></tr>'
        for k, v in sorted(scan.items(), key=lambda kv: -kv[1])
    )
    scan_table = (
        '<table style="width:auto; margin-top:12px;">'
        '<tr><th colspan="3">Scan status of the fault site '
        "(from the instantiation's pin list)</th></tr>"
        "<tr><th>Verdict</th><th>Count</th><th>% of loss</th></tr>"
        + scan_rows + "</table>"
    ) if scan_rows else ""

    notes = []
    if unmapped:
        notes.append(_callout(
            "warn",
            f"<b>{_fmt(unmapped)} coverage-loss fault(s) never mapped onto "
            f"the netlist.</b> Their fan-in, fan-out and scan status are "
            f"<b>unknown</b> &mdash; they are shown as NULL and "
            f"<i>unknown</i>, never as 0 and <i>no</i>. No root cause on "
            f"these sites is provable, and they must not be counted towards "
            f"an observability or scan-boundary conclusion."))
    if tied:
        notes.append(_callout(
            "info",
            f"<b>{_fmt(tied)} fault(s) are held at a hard constant</b> by a "
            f"tie cell reached through the hierarchy. A stuck-at fault on a "
            f"constant pin is undetectable by construction, so these are "
            f"<b>expected and non-actionable</b>: waive them rather than "
            f"spending debug effort, and exclude them when ranking causes."))
    if scan.get("unknown") and not unmapped:
        notes.append(_callout(
            "info",
            "Some mapped cells expose a pin naming convention this tool does "
            "not recognise, so their scan status is reported as "
            "<i>unknown</i> rather than guessed."))

    causes = dict(s.unresolved_causes or {})
    cause_html = ""
    if causes:
        rows = "".join(
            f"<tr><td><code>{_esc(cause)}</code></td>"
            f'<td class="num">{_fmt(count)}</td>'
            f"<td>{_esc(_UNRESOLVED_MEANING.get(cause, ''))}</td></tr>"
            for cause, count in sorted(causes.items(), key=lambda kv: -kv[1])
        )
        cause_html = (
            '<table style="margin-top:12px;">'
            '<tr><th colspan="3">Why those faults did not map</th></tr>'
            "<tr><th>Cause</th><th>Count</th><th>What to do</th></tr>"
            + rows + "</table>"
        )

    ties = _tie_driver_table(report)

    return heading + basis + scan_table + cause_html + ties + "".join(notes)


#: Human labels for the tri-state scan verdict.
_SCAN_LABEL = {
    "scan": "Scan cell (scan-data input + shift-enable pins read)",
    "non_scan": "Non-scan cell (no scan-data input, no shift-enable)",
    "unknown": "Unknown \u2014 no instantiation read, or unrecognised pins",
}

#: What each unresolved-mapping cause means for the engineer.
_UNRESOLVED_MEANING = {
    "absent_leaf": ("The parent module is present but the cell is not: read "
                    "the missing library / black-box model in."),
    "ambiguous": ("The instance name repeats and the path did not narrow it: "
                  "supply the full hierarchical path."),
    "outside_scope": ("No part of the path is in this netlist: the fault list "
                      "and netlist cover different blocks."),
    "undetermined": "Inspect these paths individually.",
}


def _tie_driver_table(report: AnalysisReport) -> str:
    """Rank the tie cells holding the most fault sites at a constant."""
    counter: Counter = Counter()
    detail: dict = {}
    for r in report.fault_results:
        tie = getattr(r, "tie_driver", None)
        if not tie:
            continue
        key = tie.get("instance") or "(unnamed)"
        counter[key] += 1
        detail.setdefault(key, tie)
    if not counter:
        return ""

    rows = "".join(
        f"<tr><td><code>{_esc(_short_path(name, 3))}</code></td>"
        f"<td><code>{_esc(detail[name].get('cell_type') or '')}</code></td>"
        f"<td class=\"num\">{_esc(detail[name].get('value') or '?')}</td>"
        f"<td class=\"num\">{_esc(detail[name].get('levels') or 0)}</td>"
        f'<td class="num">{_fmt(count)}</td></tr>'
        for name, count in counter.most_common(10)
    )
    return (
        '<table style="margin-top:12px;">'
        '<tr><th colspan="5">Constant drivers holding fault sites '
        "(resolved across hierarchy)</th></tr>"
        "<tr><th>Tie instance</th><th>Cell type</th><th>Value</th>"
        "<th>Levels away</th><th>Faults</th></tr>" + rows + "</table>"
    )


def _section_triage(report: AnalysisReport,
                    category_dumps: Optional[Sequence[Any]] = None) -> str:
    """Render the coverage triage: categories, hotspots and blocking evidence.

    Args:
        report: The analysed report.
        category_dumps: Optional per-category fault dumps written beside this
            HTML file; each selected category then links to the file holding
            its faults. ``None`` renders no links.
    """
    stats = getattr(report, "statistics", None)
    heading = "<h2>4. Coverage Triage</h2>"
    if stats is None:
        return heading + _callout(
            "info",
            "No coverage triage is stored in this report. It was produced "
            "before the triage analysis existed &mdash; re-run <b>Analyze</b> "
            "to generate it.")

    parts = [heading]
    # The detected / loss headline lives in &sect;2 and the evidence basis in
    # &sect;3. Repeating either here would show the same quantity twice, two
    # screens apart, computed two different ways.
    parts.append(_callout(
        "info",
        "Fault counts are in &sect;2 and the evidence basis in &sect;3. Note "
        "that those figures are counted from the fault list: they are "
        "<b>not</b> the ATPG tool's test-coverage figure, which also accounts "
        "for fault collapsing and untestable-fault credit."))

    loss_stats = stats.loss_stats
    if loss_stats:
        by_id = {c.subclass_id: c
                 for c in (getattr(report, "selected_categories", None) or [])}
        rows = []
        for stat in loss_stats:
            verdict = getattr(by_id.get(stat.subclass_id), "verdict", None)
            actionable = getattr(verdict, "actionable", _DASH)
            css = {"true": "badge-red", "partial": "badge-warn"}.get(
                actionable, "badge-gray")
            confidence = getattr(getattr(verdict, "confidence", None),
                                 "value", _DASH)
            rows.append(
                f"<tr><td><code>{_esc(stat.subclass_id)}</code></td>"
                f'<td class="num au">{_fmt(stat.count)}</td>'
                f'<td class="num">{stat.pct:.2f}%</td>'
                f'<td class="num">{_fmt(stat.sa0)}</td>'
                f'<td class="num">{_fmt(stat.sa1)}</td>'
                f'<td class="num">{stat.sa_asymmetry:.2f}</td>'
                f"<td>{_badge(actionable, css)}</td>"
                f"<td>{_esc(confidence)}</td></tr>")
        parts.append(
            "<h3>4.1 Coverage-loss categories</h3>"
            "<table><tr><th>Category</th><th>Faults</th><th>% of all</th>"
            "<th>sa0</th><th>sa1</th><th>Imbalance</th>"
            "<th>Worth acting on</th><th>Confidence</th></tr>"
            + "".join(rows) + "</table>")

    selected = getattr(report, "selected_categories", None) or []
    if selected:
        dumps_by_id = {d.subclass_id: d for d in (category_dumps or [])}
        items = []
        for cat in selected:
            info = cat.stat.info
            title = f" &mdash; {_esc(info.title)}" if info else ""
            detail = [f"<b><code>{_esc(cat.subclass_id)}</code></b>{title}: "
                      f"{_esc(cat.reason)}"]
            if info:
                detail.append(f"<br>{_esc(info.meaning)}")
            verdict = getattr(cat, "verdict", None)
            if verdict is not None:
                detail.append(
                    f"<br><i>Verdict:</i> <b>{_esc(verdict.actionable)}</b> "
                    f"({_esc(verdict.confidence.value)} confidence) &mdash; "
                    f"{_esc(verdict.reason)}")
            links = _dump_links_html(dumps_by_id.get(cat.subclass_id))
            if links:
                detail.append(links)
            items.append("<li>" + "".join(detail) + "</li>")
        parts.append("<h3>4.2 Selected for investigation</h3><ol>"
                     + "".join(items) + "</ol>")

    parts.append(_triage_hotspots(selected))
    parts.append(_triage_blocking(selected))
    parts.append(_triage_profiles(selected))
    return "".join(parts)


def _dump_links_html(dump: Any) -> str:
    """Render links to the file holding one category's faults, if written."""
    if dump is None:
        return ""
    links = []
    if dump.csv_href:
        links.append(f'<a href="{_esc(dump.csv_href)}">CSV</a>')
    if dump.json_href:
        links.append(f'<a href="{_esc(dump.json_href)}">JSON</a>')
    if not links:
        return ""
    tail = ""
    if dump.truncated:
        tail = " (the JSON is capped; the CSV holds every fault)"
    return (f"<br><i>All {_fmt(dump.dumped_count)} faults in this "
            f"category:</i> " + " &middot; ".join(links) + _esc(tail))


def _triage_hotspots(selected: List) -> str:
    """Render where each category's faults concentrate in the hierarchy."""
    with_clusters = [c for c in selected
                     if getattr(c, "clusters", None) and c.clusters.clusters]
    if not with_clusters:
        return ""

    blocks = ["<h3>4.3 Where the loss concentrates</h3>", _callout(
        "warn",
        "A dominant prefix shows <b>where</b> to look. It is not a root "
        "cause. Sample paths are quoted verbatim and can be pasted into an "
        "ATPG tool unmodified.")]
    for cat in with_clusters:
        cr = cat.clusters
        rows = [
            f"<tr><td><code>{_esc(c.prefix)}</code></td>"
            f'<td class="num au">{_fmt(c.count)}</td>'
            f'<td class="num">{c.pct:.1f}%</td>'
            f'<td class="num">{_fmt(c.sa0)}</td>'
            f'<td class="num">{_fmt(c.sa1)}</td></tr>'
            for c in cr.clusters[:5]
        ]
        blocks.append(
            f"<p><b><code>{_esc(cat.subclass_id)}</code></b> &mdash; "
            f"{_fmt(cr.total_faults)} fault(s) in {len(cr.clusters)} cluster(s) "
            f"at depth {cr.depth} ({_esc(cr.depth_note)})</p>"
            "<table><tr><th>Hierarchy prefix</th><th>Faults</th><th>%</th>"
            "<th>sa0</th><th>sa1</th></tr>" + "".join(rows) + "</table>")
        top = cr.top
        if top is not None and top.samples:
            samples = "".join(f"<li><code>{_esc(s)}</code></li>"
                              for s in top.samples)
            blocks.append(f"<p>Sample faults from <code>{_esc(top.prefix)}</code>"
                          f":</p><ul>{samples}</ul>")
    return "".join(blocks)


def _triage_blocking(selected: List) -> str:
    """Render the traced blocking sources for the attributable categories.

    Hard tie cells are deliberately NOT listed here: &sect;3 already ranks
    them, resolved across hierarchy feedthrough ports and with the constant
    value, which is strictly better evidence than this bounded module-local
    cone walk. What remains is what &sect;3 cannot tell you -- drivers that are
    constant *in practice* but could be changed: a test data register that can
    be reprogrammed, or an unscanned flop that ATPG cannot drive.
    """
    entries = []
    for cat in selected:
        att = getattr(cat, "attribution", None)
        if att is None or not att.attributed:
            continue
        changeable = [s for s in att.tie_sources if s.kind != "tie_cell"]
        if changeable or att.constraint_sources:
            entries.append((cat, att, changeable))
    if not entries:
        return ""

    blocks = ["<h3>4.4 What is blocking these faults</h3>", _callout(
        "warn",
        "Derived by tracing fan-in cones through the netlist. This is a "
        "structural <b>estimate</b> of the blocking source, not the ATPG "
        "tool's own attribution &mdash; confirm before acting. Hard tie cells "
        "are excluded here because &sect;3 lists them with a better trace.")]
    for cat, att, changeable in entries:
        blocks.append(
            f"<p><b><code>{_esc(cat.subclass_id)}</code></b> &mdash; "
            f"{_fmt(att.attributed)} of {_fmt(att.analysed)} fault(s) traced "
            f"({att.coverage:.0%}), verdict "
            f"<code>{_esc(att.verdict)}</code><br>{_esc(att.note)}</p>")
        if changeable:
            rows = "".join(
                f"<tr><td><code>{_esc(s.driver)}</code></td>"
                f"<td>{_esc(s.cell_type or _DASH)}</td>"
                f"<td>{_esc(s.tie_value or '?')}</td>"
                f"<td>{_esc(s.kind)}</td>"
                f"<td>{'yes' if s.is_configurable else 'no'}</td>"
                f'<td class="num au">{_fmt(s.count)}</td></tr>'
                for s in changeable[:5])
            blocks.append(
                "<table><tr><th>Driver held constant</th><th>Cell type</th>"
                "<th>Holding value</th><th>Kind</th><th>Reprogrammable?</th>"
                "<th>Faults</th></tr>" + rows + "</table>")
        if att.constraint_sources:
            rows = "".join(
                f"<tr><td><code>{_esc(s.signal)}</code></td>"
                f"<td>{_esc(s.kind or _DASH)}</td>"
                f"<td>{_esc(s.value or '?')}</td>"
                f'<td class="num au">{_fmt(s.count)}</td></tr>'
                for s in att.constraint_sources[:5])
            blocks.append(
                "<table><tr><th>Constrained signal</th><th>Kind</th>"
                "<th>Value</th><th>Faults</th></tr>" + rows + "</table>")
    return "".join(blocks)


def _triage_profiles(selected: List) -> str:
    """Render why each aborted category was structurally hard to test."""
    profiled = [c for c in selected if getattr(c, "reachability", None)
                and c.reachability.profiled]
    if not profiled:
        return ""

    blocks = ["<h3>4.5 Why these faults were hard to test</h3>", _callout(
        "warn",
        "Estimated by walking the netlist. A narrow bottleneck and a "
        "reconvergent cone need <b>opposite</b> fixes &mdash; more abort "
        "budget helps the first and is wasted on the second &mdash; so "
        "confirm the signature before acting.")]
    for cat in profiled:
        prof = cat.reachability
        rows = "".join(
            f"<tr><td>{_esc(row['label'])}</td>"
            f'<td class="num au">{_fmt(row["count"])}</td>'
            f"<td>{_esc(row['meaning'])}</td></tr>"
            for row in prof.as_dict()["signatures"])
        blocks.append(
            f"<p><b><code>{_esc(cat.subclass_id)}</code></b> &mdash; "
            f"{_fmt(prof.profiled)} of {_fmt(prof.analysed)} site(s) profiled; "
            f"dominant signature <code>{_esc(prof.dominant)}</code> "
            f"({prof.dominant_share:.0%})<br>{_esc(prof.note)}</p>"
            "<table><tr><th>Signature</th><th>Sites</th>"
            "<th>What it means</th></tr>" + rows + "</table>")
    return "".join(blocks)


def _section_fix_plan(report: AnalysisReport) -> str:
    """Render the ranked fix proposals with their evidence and commands."""
    recommendations = getattr(report, "recommendations", None) or []
    heading = "<h2>5. Fix Plan</h2>"
    if not recommendations:
        return ""

    parts = [heading, _callout(
        "info",
        "Commands are provided for you to run in your own ATPG session; this "
        "tool never executes them. Where an action is marked as needing "
        "measurement, <b>no coverage gain is predicted</b> &mdash; the re-run "
        "is what establishes the benefit.")]

    for rec in recommendations:
        detail = [
            f"<h3>5.{rec.rank} {_esc(rec.title)}</h3>",
            f"<p><b>Category:</b> <code>{_esc(rec.subclass_id)}</code> "
            f"({_fmt(rec.fault_count)} faults, {rec.pct:.2f}%)"
            f" &nbsp;&middot;&nbsp; <b>worth acting on:</b> "
            f"{_esc(rec.actionable)}"
            f" &nbsp;&middot;&nbsp; <b>confidence:</b> "
            f"{_esc(rec.confidence.value)}"
            f" &nbsp;&middot;&nbsp; <b>effort / risk:</b> "
            f"{_esc(rec.fix.effort)} / {_esc(rec.fix.risk)}</p>",
        ]
        if rec.hotspot:
            detail.append(f"<p><b>Concentrated under:</b> "
                          f"<code>{_esc(rec.hotspot)}</code></p>")
        detail.append(f"<p><b>Why:</b> {_esc(rec.fix.rationale)}</p>")
        if rec.fix.preconditions:
            detail.append("<p><b>Confirm first:</b></p><ul>" + "".join(
                f"<li>{_esc(p)}</li>" for p in rec.fix.preconditions) + "</ul>")
        if rec.evidence:
            detail.append("<p><b>Evidence:</b></p><ul>" + "".join(
                f"<li>{_esc(e)}</li>" for e in rec.evidence) + "</ul>")
        if rec.fix.expected_effect:
            detail.append(f"<p><b>Expected outcome:</b> "
                          f"{_esc(rec.fix.expected_effect)}</p>")
        if rec.caveats:
            detail.append(_callout("warn", "<b>Caveats:</b><ul>" + "".join(
                f"<li>{_esc(c)}</li>" for c in rec.caveats) + "</ul>"))
        if rec.fix.commands:
            body = _esc("\n".join(rec.fix.commands))
            detail.append(
                "<pre style='background:#f5f5f5; padding:10px; "
                "border:1px solid #ddd; white-space:pre-wrap;'>"
                + body + "</pre>")
        parts.append("".join(detail))
    return "".join(parts)


def _section_hotspots(report: AnalysisReport) -> str:
    insts = report.summary.top_instances
    if not insts:
        return ""
    s = report.summary
    # Percentages are of the ACTIONABLE loss, which is what this ranking is
    # built from; using the raw loss total here would make every row look
    # smaller than it is.
    loss = s.actionable_loss_count or s.coverage_loss_count \
        or sum(c for _, c in insts) or 1
    rows = []
    for rank, (name, count) in enumerate(insts, start=1):
        rows.append(
            f'<tr><td class="num">{rank}</td>'
            f"<td><code>{_esc(_short_path(str(name)))}</code></td>"
            f'<td class="num au">{_fmt(count)}</td>'
            f'<td class="num">{_pct(count, loss)}</td></tr>'
        )
    table = (
        "<table><tr><th>Rank</th><th>Module / Instance</th>"
        "<th>Actionable Faults</th><th>% of Actionable Loss</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    note = _callout(
        "info",
        "Ranked over the <b>actionable</b> coverage loss only &mdash; faults "
        "that mapped onto the netlist and are not held at a hard constant. "
        "Tie cells are excluded deliberately: a single constant driver can "
        "hold more faults than every real hotspot combined and would other"
        "wise fill this table. They are listed in &sect;3.")
    return "<h2>6. Module-Level Hotspot Analysis</h2>" + note + table


def _rc_box(
    rank: int,
    rc: RootCause,
    results: List[FaultAnalysisResult],
    total_loss: int,
) -> str:
    count = len(results)
    share = count / total_loss if total_loss else 0.0
    if share >= 0.40:
        sev_txt, sev_css = f"HIGH \u2014 {_fmt(count)} faults", "badge-red"
    elif share >= 0.10:
        sev_txt, sev_css = f"MEDIUM \u2014 {_fmt(count)} faults", "badge-warn"
    else:
        sev_txt, sev_css = f"LOW \u2014 {_fmt(count)} faults", "badge-gray"

    rep = _representative(results)
    label = _RC_LABELS.get(rc, rc.value)

    any_ctrl = any(r.controllability_issue for r in results)
    any_obs = any(r.observability_issue for r in results)
    any_con = any(r.constraint_related for r in results)
    scan_states = {r.scan_boundary_state for r in results}
    if "yes" in scan_states:
        scan_txt = "Yes"
    elif "no" in scan_states:
        scan_txt = "No"
    else:
        scan_txt = "Unknown (no object in this group mapped onto the netlist)"

    mechanism = "; ".join(rep.inferred_conclusions[:3]) or (
        "Structural condition prevents test generation at the fault site."
    )

    attr_rows = (
        f'<tr><th style="width:160px;">Attribute</th><th>Details</th></tr>'
        f"<tr><td>Module / Instance</td>"
        f"<td><code>{_esc(_short_path(_group_module(results)))}</code></td></tr>"
        f"<tr><td>Fault classes</td><td>{_group_classes(results)}</td></tr>"
        f"<tr><td>Example cell</td>"
        f"<td><code>{_esc(rep.cell_type or 'unknown')}</code> @ "
        f"<code>{_esc(_short_path(rep.instance_name or ''))}</code></td></tr>"
        f"<tr><td>Mechanism</td><td>{_esc(mechanism)}</td></tr>"
        f"<tr><td>Controllability issue?</td>"
        f"<td>{'Yes' if any_ctrl else 'No'}</td></tr>"
        f"<tr><td>Observability issue?</td>"
        f"<td>{'Yes' if any_obs else 'No'}</td></tr>"
        f"<tr><td>Constraint-related?</td>"
        f"<td>{'Yes' if any_con else 'No'}</td></tr>"
        f"<tr><td>Scan boundary involved?</td>"
        f"<td>{_esc(scan_txt)}</td></tr>"
    )

    path = ""
    fan_in = rep.fan_in[:4]
    fan_out = rep.fan_out[:4]
    if not rep.connectivity_known:
        path = _callout(
            "info",
            "Connectivity unknown — this fault object was never mapped onto "
            "the netlist, so no fan-in/fan-out was measured. Do not read the "
            "absence of a cone as an absence of connections.",
        )
    elif fan_in or fan_out:
        fi = ", ".join(_short_path(x, 2) for x in fan_in) or "(primary input / none)"
        fo = ", ".join(_short_path(x, 2) for x in fan_out) or "(no observable sink)"
        cell = rep.instance_name or rep.fault.fault_object
        path = (
            "<pre>fan-in : "
            + _esc(fi)
            + "\n          |\n          v\n  ["
            + _esc(_short_path(cell, 3))
            + "]  ("
            + _esc(rep.cell_type or "cell")
            + ")\n          |\n          v\nfan-out: "
            + _esc(fo)
            + "</pre>"
        )

    action_kind = "warn" if share >= 0.10 else "info"
    rec = rep.recommended_step or (
        "Review the boundary structurally; file a waiver if the structure is "
        "architecturally non-testable."
    )
    action = _callout(action_kind, f"<b>Recommended action:</b> {_esc(rec)}")

    return (
        '<div class="rc-box" style="border:2px solid #003366; padding:14px 18px; '
        'margin:12px 0;">'
        f'<h3 style="color:#003366;">Root Cause {rank} \u2014 {_esc(label)} '
        f'<span style="margin-left:10px;">{_badge(sev_txt, sev_css)}</span></h3>'
        f"<table>{attr_rows}</table>"
        f"{path}{action}"
        "</div>"
    )


def _section_root_causes(report: AnalysisReport, max_boxes: int = 8) -> str:
    groups = _group_by_root_cause(report)
    total_loss = report.summary.coverage_loss_count or sum(
        len(v) for v in groups.values()
    ) or 1
    boxes = []
    for rank, (rc, results) in enumerate(groups.items(), start=1):
        if rank > max_boxes:
            break
        boxes.append(_rc_box(rank, rc, results, total_loss))
    return "<h2>7. Root Cause Analysis</h2>" + "".join(boxes)


def _section_coverage_table(report: AnalysisReport) -> str:
    groups = _group_by_root_cause(report)
    rows = []
    for idx, (rc, results) in enumerate(groups.items(), start=1):
        rep = _representative(results)
        non_scan = "Yes" if any(r.scan_boundary_involved for r in results) else "No"
        fix = rep.recommended_step or "Investigate structurally / file waiver"
        rows.append(
            f'<tr><td class="num">{idx}</td>'
            f"<td><code>{_esc(_short_path(_group_module(results)))}</code></td>"
            f"<td>{_group_classes(results)}</td>"
            f'<td class="num au">{_fmt(len(results))}</td>'
            f"<td>{_esc(_RC_LABELS.get(rc, rc.value))}</td>"
            f"<td>{non_scan}</td>"
            f"<td>{_esc(fix)}</td></tr>"
        )
    table = (
        '<table style="font-size:9pt;"><tr><th>#</th>'
        "<th>Module / Instance Path</th><th>Fault Classes</th><th>Count</th>"
        "<th>Root Cause</th><th>Non-scan?</th><th>Fix?</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return "<h2>8. Coverage-Loss Summary Table</h2>" + table


def _section_conclusions(report: AnalysisReport) -> str:
    """State the primary actionable gap and hand off to the fix plan.

    Deliberately short. The ranked, evidence-backed, command-carrying plan is
    &sect;5; a second priority table here would be the same advice twice, and
    the weaker of the two. The projected-coverage figure this section used to
    end with has also been removed: it was an unmeasured prediction, and no
    coverage gain can be established without re-running ATPG.
    """
    s = report.summary
    actionable = [
        r for r in report.fault_results
        if r.connectivity_known and r.root_cause is not RootCause.TIED_CONSTANT
    ]
    groups = _group_by_root_cause_of(actionable)
    heading = "<h2>9. Conclusions &amp; Recommended Actions</h2>"

    if not groups:
        body = _callout(
            "warn",
            "<b>No actionable coverage loss could be ranked.</b> Every "
            "coverage-loss fault either failed to map onto the netlist or is "
            "held at a hard constant &mdash; see &sect;3. Close those gaps "
            "before drawing a conclusion about the dominant mechanism.")
        return heading + body + _footer()

    total_actionable = len(actionable)
    top_rc, top_results = next(iter(groups.items()))
    top_count = len(top_results)
    top_share = 100.0 * top_count / total_actionable if total_actionable else 0.0

    primary = _callout(
        "error",
        f"<b>Primary actionable gap: "
        f"{_esc(_RC_LABELS.get(top_rc, top_rc.value))} — {_fmt(top_count)} of "
        f"{_fmt(total_actionable)} actionable fault(s) ({top_share:.0f}%).</b>"
        f"<br/>Ranked over the actionable population only — the "
        f"{_fmt(s.unmapped_count)} unmapped and {_fmt(s.tied_constant_count)} "
        f"tied fault(s) in &sect;3 are excluded, because a ranking that "
        f"includes them measures artefacts rather than mechanisms."
        f"<br/>The concrete actions, with commands and caveats, are in "
        f"&sect;5 &mdash; this section deliberately does not repeat them.")

    caveat = ""
    if s.coverage_loss_count and total_actionable < 0.5 * s.coverage_loss_count:
        caveat = _callout(
            "warn",
            f"<b>Read the ranking with care.</b> Only "
            f"{_pct(total_actionable, s.coverage_loss_count)} of the "
            f"coverage loss is actionable; the rest is unmapped or tied. The "
            f"headline loss figure is dominated by those two populations, so "
            f"fix them before treating this ranking as the design's real "
            f"coverage profile.")

    return heading + primary + caveat + _footer()


def _footer() -> str:
    return (
        '<hr class="divider" style="margin-top:30px;"/>'
        '<p style="font-size:8pt; color:#888; text-align:center;">'
        f"Report generated {datetime.now().strftime('%B %d, %Y %H:%M')} "
        "&nbsp;|&nbsp; ATPG Coverage-Loss Debug Agent + dft-atpg-debug skill</p>"
        '<p class="no-print" style="margin-top:14px; padding:10px; '
        'background:#fffbdd; border:1px solid #f0d060; font-size:9.5pt;">'
        "<b>To save as PDF:</b> use <b>Open Report in Browser</b>, then "
        "Ctrl+P &rarr; Save as PDF (A4, enable \u201cPrint backgrounds\u201d).</p>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_html_report(
    report: AnalysisReport,
    design_name: Optional[str] = None,
    netlist_path: Optional[str] = None,
    faults_path: Optional[str] = None,
    constraints_path: Optional[str] = None,
    analyst_note: Optional[str] = None,
    category_dumps: Optional[Sequence[Any]] = None,
) -> str:
    """Return a complete, self-contained document-style HTML report.

    Args:
        report: The analysis result to render.
        design_name: Optional design label for the cover (inferred otherwise).
        netlist_path / faults_path / constraints_path: Optional source file
            paths shown on the cover page.
        analyst_note: Optional analyst note / edit banner shown at the top.
        category_dumps: Optional per-category fault dumps written beside this
            HTML file, linked from &sect;4.2.

    Returns:
        A full ``<html>`` document string.
    """
    design = design_name or _guess_design_name(report)

    note_html = ""
    if analyst_note and analyst_note.strip():
        note_html = _callout(
            "warn",
            "<b>Analyst note / edits:</b><br>"
            + _esc(analyst_note.strip()).replace("\n", "<br>"))

    if report.fault_results:
        body = (
            _cover(design, netlist_path, faults_path, constraints_path)
            + note_html
            + _section_constraints(report, constraints_path)
            + _section_fault_stats(report)
            + _section_evidence(report)
            + _section_triage(report, category_dumps)
            + _section_fix_plan(report)
            + _section_hotspots(report)
            + _section_root_causes(report)
            + _section_coverage_table(report)
            + _section_conclusions(report)
        )
    else:
        body = (
            _cover(design, netlist_path, faults_path, constraints_path)
            + note_html
            + _section_fault_stats(report)
            + _callout(
                "ok",
                "<b>No coverage-loss faults (AU / UO / UC) were found.</b> "
                "There is nothing to root-cause for this run.",
            )
        )

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>'
        f"<title>DFT/ATPG Coverage Debug \u2014 {_esc(design)}</title>"
        f"<style>{_STYLE}</style></head>"
        '<body><div class="page">'
        + body
        + "</div></body></html>"
    )
