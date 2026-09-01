"""Guardrails that keep generated output honest.

Two failure modes matter enough to check mechanically rather than by review.

**Fabricated hierarchy paths.** A path that has been shortened with an ellipsis,
or stitched together from parts that look plausible, will not resolve when
pasted into a tool. Worse, it looks authoritative. Every path this tool emits
must appear verbatim in a source artefact, or be a component-aligned prefix of
one.

**Unmeasured coverage claims.** The gain from a fix can only be established by
re-running ATPG and comparing statistics. Predicting a percentage from cluster
sizes or sample hit-rates produces a number that reads like evidence and is
not, so any such claim is flagged.

The checks run over the tool's own output as a self-audit, and are exposed so
the LLM layer can verify a path before quoting it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

#: Markers that indicate a path was shortened rather than quoted in full.
ELLIPSIS_MARKERS = ("...", "\u2026")

#: A path-like token: at least two components joined by a hierarchy separator.
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_$\[\].\\]+(?:/[A-Za-z0-9_$\[\]./\\]+)+")

#: Claims of a coverage gain that has not been measured by a re-run.
_CLAIM_PATTERNS = (
    re.compile(r"(recover|regain|gain|improv|increas)\w*\s+(?:of\s+|by\s+)?"
               r"[+-]?\d+(?:\.\d+)?\s*%", re.I),
    re.compile(r"[+-]?\d+(?:\.\d+)?\s*%\s*(?:coverage\s+)?"
               r"(gain|improvement|recovery|uplift)", re.I),
    re.compile(r"will\s+(recover|regain|add|gain|improve)\b", re.I),
    re.compile(r"expected\s+(?:coverage\s+)?(gain|uplift|improvement)", re.I),
    re.compile(r"(estimated|projected)\s+(?:coverage\s+)?"
               r"(gain|recovery|improvement)", re.I),
)

#: Tokens that look path-like but are placeholders in a command template.
_PLACEHOLDER = re.compile(r"[<>{}]")

#: A constraint or tie value code such as ``C0``, ``CX``, ``T1``, ``sa0``.
#: Prose pairs these with a slash — "constrained to C0/C1", "the sa0/sa1
#: split" — which is not a hierarchy path and must not be checked as one.
_VALUE_CODE = re.compile(r"^[A-Za-z]{0,2}[0-9Xx]$")


def _is_value_code_pair(token: str) -> bool:
    """True when every component of *token* is a bare value code."""
    parts = [p for p in token.split("/") if p]
    return bool(parts) and all(_VALUE_CODE.match(p) for p in parts)


@dataclass
class Issue:
    """One guardrail violation found in generated text.

    Attributes:
        kind: ``elided_path``, ``unknown_path`` or ``unmeasured_claim``.
        text: The offending fragment, quoted as found.
        context: Where it was found, for the audit trail.
    """

    kind: str
    text: str
    context: str = ""

    def __str__(self) -> str:  # pragma: no cover - formatting only
        where = f" in {self.context}" if self.context else ""
        return f"[{self.kind}] {self.text!r}{where}"

    def as_dict(self) -> Dict[str, str]:
        """Plain-dict view for serialisation and tool responses."""
        return {"kind": self.kind, "text": self.text, "context": self.context}


class PathRegistry:
    """Every hierarchy path that genuinely appeared in a source artefact.

    A path is accepted when it matches a source path exactly, or when it is a
    *component-aligned prefix* of one. Prefixes are legitimate because that is
    what a cluster is — every component came from a real path — whereas a
    partial component such as ``top/u_al`` is not.
    """

    def __init__(self, paths: Iterable[str] = ()) -> None:
        self._exact: Set[str] = set()
        self._prefixes: Set[str] = set()
        for path in paths:
            self.add(path)

    @staticmethod
    def _normalise(path: str) -> str:
        """Reduce a path to a comparable form without losing components."""
        text = (path or "").strip().strip('"').strip("'")
        text = text.replace("\\", "").replace(".", "/")
        text = re.sub(r"/+", "/", text)
        return text.strip("/")

    def add(self, path: str) -> None:
        """Register *path* and every component-aligned prefix of it."""
        norm = self._normalise(path)
        if not norm:
            return
        self._exact.add(norm)
        parts = norm.split("/")
        for end in range(1, len(parts)):
            self._prefixes.add("/".join(parts[:end]))

    def add_all(self, paths: Iterable[str]) -> None:
        for path in paths:
            self.add(path)

    def __len__(self) -> int:
        return len(self._exact)

    def is_known(self, path: str) -> bool:
        """True when *path* is a source path or a prefix of one."""
        norm = self._normalise(path)
        if not norm:
            return False
        return norm in self._exact or norm in self._prefixes

    def validate(self, path: str, context: str = "") -> Optional[Issue]:
        """Return an :class:`Issue` for *path*, or ``None`` when it is sound."""
        if any(marker in path for marker in ELLIPSIS_MARKERS):
            return Issue("elided_path", path, context)
        if not self.is_known(path):
            return Issue("unknown_path", path, context)
        return None

    @classmethod
    def from_parts(cls, fault_results: Iterable[Any] = (),
                   constraints: Iterable[Any] = (),
                   faults: Iterable[Any] = (),
                   netlist: Any = None) -> "PathRegistry":
        """Build a registry from the parsed artefacts directly.

        Used by the tool layer, which holds the evidence but not a full
        report object.
        """
        registry = cls()
        for fault in faults or ():
            registry.add(getattr(fault, "fault_object", ""))
            registry.add(getattr(fault, "normalized_object", ""))
        for result in fault_results or ():
            registry.add(getattr(result.fault, "fault_object", ""))
            instance = getattr(result.mapping, "instance_name", None)
            if instance:
                registry.add(instance)
        for record in constraints or ():
            registry.add(getattr(record, "signal", "") or "")
            registry.add(getattr(record, "normalized_signal", "") or "")
        for module in (getattr(netlist, "modules", None) or {}).values():
            for name in getattr(module, "instances", {}):
                registry.add(name)
        return registry

    @classmethod
    def from_report(cls, report: Any) -> "PathRegistry":
        """Build a registry from every path a report was derived from.

        Sources are the fault list, the constraint file and the netlist
        instance names — the three artefacts the analysis is allowed to quote.
        """
        registry = cls.from_parts(
            fault_results=getattr(report, "fault_results", None) or (),
            constraints=getattr(report, "constraints", None) or (),
            faults=getattr(report, "faults", None) or (),
            netlist=getattr(report, "netlist", None),
        )
        logger.debug("Path registry built from %d source path(s).",
                     len(registry))
        return registry


def scan_paths(text: str, registry: PathRegistry,
               context: str = "") -> List[Issue]:
    """Return path problems found in *text*.

    Args:
        text: Generated prose, a report section or an LLM answer.
        registry: The paths that may legitimately be quoted.
        context: Label describing where *text* came from.

    Returns:
        One :class:`Issue` per distinct offending path.
    """
    issues: List[Issue] = []
    seen: Set[str] = set()

    for marker in ELLIPSIS_MARKERS:
        for match in re.finditer(
                r"[A-Za-z0-9_$\[\]./\\]*" + re.escape(marker)
                + r"[A-Za-z0-9_$\[\]./\\]*", text):
            token = match.group(0)
            if "/" in token and token not in seen:
                seen.add(token)
                issues.append(Issue("elided_path", token, context))

    for match in _PATH_TOKEN.finditer(text):
        token = match.group(0).rstrip(".,;:)`'\"")
        if token in seen or _PLACEHOLDER.search(token):
            continue
        if any(marker in token for marker in ELLIPSIS_MARKERS):
            continue
        if _is_value_code_pair(token):
            continue
        seen.add(token)
        issue = registry.validate(token, context)
        if issue is not None:
            issues.append(issue)
    return issues


def scan_claims(text: str, context: str = "") -> List[Issue]:
    """Return unmeasured coverage-gain claims found in *text*.

    Several patterns can match the same sentence — "will recover 5%" trips
    both the verb rule and the percentage rule — so overlapping matches are
    collapsed into a single issue. One claim, one warning.
    """
    matches = []
    for pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), match.group(0).strip()))
    matches.sort(key=lambda m: (m[0], -m[1]))

    issues: List[Issue] = []
    covered_to = -1
    for start, end, fragment in matches:
        if start < covered_to:
            continue
        covered_to = end
        issues.append(Issue("unmeasured_claim", fragment, context))
    return issues


def check_text(text: str, registry: Optional[PathRegistry] = None,
               context: str = "") -> List[Issue]:
    """Run both guardrails over *text*.

    Args:
        text: The content to audit.
        registry: Legitimate paths. When ``None``, only claims are checked,
            since without sources no path can be verified either way.
        context: Label describing where *text* came from.

    Returns:
        All issues found, path problems first.
    """
    issues: List[Issue] = []
    if registry is not None:
        issues.extend(scan_paths(text, registry, context))
    issues.extend(scan_claims(text, context))
    return issues


def audit_report(report: Any) -> List[Issue]:
    """Audit a report's own generated recommendations.

    This is a self-check: it verifies that the evidence and notes this tool
    produced quote only real paths and predict no unmeasured gain. Violations
    are a defect in the tool, not in the design being analysed.

    Args:
        report: A populated ``AnalysisReport``.

    Returns:
        Every issue found across the recommendations and category notes.
    """
    registry = PathRegistry.from_report(report)
    issues: List[Issue] = []

    for rec in getattr(report, "recommendations", None) or ():
        where = f"recommendation {rec.rank} ({rec.subclass_id})"
        for line in rec.evidence:
            issues.extend(check_text(line, registry, where))
        for line in rec.caveats:
            issues.extend(scan_claims(line, where))
        if rec.hotspot:
            issue = registry.validate(rec.hotspot, where)
            if issue is not None:
                issues.append(issue)

    for category in getattr(report, "selected_categories", None) or ():
        where = f"category {category.subclass_id}"
        for source in (getattr(category, "attribution", None),
                       getattr(category, "reachability", None)):
            note = getattr(source, "note", "")
            if note:
                issues.extend(check_text(note, registry, where))

    if issues:
        logger.warning("Report self-audit found %d guardrail issue(s).",
                       len(issues))
    return issues


def issues_as_warnings(issues: Iterable[Issue]) -> List[str]:
    """Render *issues* as warning strings for a report's warning list."""
    rendered = []
    for issue in issues:
        if issue.kind == "elided_path":
            rendered.append(
                f"Guardrail: a shortened path '{issue.text}' was emitted in "
                f"{issue.context or 'generated output'}. Paths must be quoted "
                f"in full or they will not resolve.")
        elif issue.kind == "unknown_path":
            rendered.append(
                f"Guardrail: path '{issue.text}' in "
                f"{issue.context or 'generated output'} does not appear in "
                f"any source artefact.")
        else:
            rendered.append(
                f"Guardrail: '{issue.text}' in "
                f"{issue.context or 'generated output'} predicts a coverage "
                f"gain that has not been measured by a re-run.")
    return rendered
