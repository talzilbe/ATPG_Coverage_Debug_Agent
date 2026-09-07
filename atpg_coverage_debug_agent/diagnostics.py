"""Input-quality diagnostics shared by every parser and mapper.

The analyzer used to swallow anything it did not recognise into a silent
catch-all bucket. On one partition that bucket held a low-single-digit
percentage of the whole fault population — several legitimate fault classes
the tool had simply never been taught — with no warning emitted and no sample
retained, so the loss was invisible and un-inspectable at the same time. Every
metric derived from that census was wrong by construction.

This module supplies the mechanism that replaces silent fallback:

* :class:`UnrecognisedTracker` — counts unrecognised tokens *per distinct
  token*, retains a bounded number of verbatim sample records for each, and
  compares the total against a configurable threshold.
* :class:`ThresholdExceeded` — raised when a domain marked ``fatal`` breaches
  its threshold, so a corrupt or unsupported input fails loudly instead of
  producing a plausible-looking but wrong report.
* :class:`CensusMismatch` — raised when a parsed population does not reconcile
  with the sum of its categories.

Nothing here knows about faults, constraints or netlists specifically: the same
tracker is used for all three so the "never fall back silently" rule cannot be
honoured in one place and forgotten in another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class InputQualityError(Exception):
    """Base class for input problems that must not be reported as findings."""


class ThresholdExceeded(InputQualityError):
    """Too large a share of an input could not be recognised.

    Attributes:
        domain: Which input the failure came from (``fault classes``, ...).
        count: Number of unrecognised records.
        total: Number of records inspected.
        pct: ``count / total`` as a percentage.
        threshold_pct: The configured limit that was breached.
        tokens: The distinct unrecognised tokens, largest first.
    """

    def __init__(self, domain: str, count: int, total: int, pct: float,
                 threshold_pct: float, tokens: List[str]) -> None:
        self.domain = domain
        self.count = count
        self.total = total
        self.pct = pct
        self.threshold_pct = threshold_pct
        self.tokens = list(tokens)
        listing = ", ".join(tokens[:10]) or "(none)"
        super().__init__(
            f"{count} of {total} record(s) ({pct:.4f}%) had an unrecognised "
            f"{domain}, above the configured threshold of {threshold_pct}%. "
            f"Distinct token(s): {listing}. "
            f"Extend the configuration for this input rather than letting the "
            f"records be bucketed as unknown."
        )


class CensusMismatch(InputQualityError):
    """A category breakdown does not add up to the population it describes."""


@dataclass
class UnrecognisedToken:
    """One distinct token the analyzer could not interpret.

    Attributes:
        token: The verbatim token, e.g. a fault class or a directive name.
        count: How many records carried it.
        samples: Verbatim sample records, capped by the tracker's limit.
        first_line: Line number of the first occurrence, when known.
    """

    token: str
    count: int = 0
    samples: List[str] = field(default_factory=list)
    first_line: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "count": self.count,
            "first_line": self.first_line,
            "samples": list(self.samples),
        }


@dataclass
class UnrecognisedReport:
    """The outcome of tracking one input domain.

    Attributes:
        domain: Human-readable name of what was being recognised.
        total: Records inspected.
        tokens: Distinct unrecognised tokens, largest first.
        threshold_pct: The configured threshold for this domain.
        fatal: Whether breaching the threshold raises.
    """

    domain: str
    total: int = 0
    tokens: List[UnrecognisedToken] = field(default_factory=list)
    threshold_pct: float = 0.0
    fatal: bool = False

    @property
    def count(self) -> int:
        """Total unrecognised records across every distinct token."""
        return sum(t.count for t in self.tokens)

    @property
    def pct(self) -> float:
        """Unrecognised share of the inspected population, in percent."""
        if self.total <= 0:
            return 0.0
        return 100.0 * self.count / self.total

    @property
    def exceeded(self) -> bool:
        """True when the unrecognised share is above the threshold."""
        return bool(self.tokens) and self.pct > self.threshold_pct

    def warnings(self) -> List[str]:
        """One warning per distinct token, plus a summary when over threshold.

        The per-token form is deliberate: a single aggregate line lets a
        reader assume one odd record, when the reality may be five legitimate
        classes the tool has never been taught.
        """
        out: List[str] = []
        for tok in self.tokens:
            where = f" (first at line {tok.first_line})" if tok.first_line else ""
            out.append(
                f"WARNING: unrecognised {self.domain} '{tok.token}' on "
                f"{tok.count} record(s){where}."
            )
        if self.tokens:
            out.append(
                f"WARNING: {self.count} of {self.total} record(s) "
                f"({self.pct:.4f}%) carried an unrecognised {self.domain} "
                f"across {len(self.tokens)} distinct token(s). These records "
                f"are retained and sampled, never silently reclassified."
            )
        if self.exceeded:
            out.append(
                f"WARNING: unrecognised {self.domain} share {self.pct:.4f}% "
                f"exceeds the configured threshold of {self.threshold_pct}%."
            )
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "total": self.total,
            "count": self.count,
            "pct": round(self.pct, 6),
            "threshold_pct": self.threshold_pct,
            "fatal": self.fatal,
            "exceeded": self.exceeded,
            "tokens": [t.as_dict() for t in self.tokens],
        }


class UnrecognisedTracker:
    """Accumulates unrecognised tokens for one input domain.

    Args:
        domain: What is being recognised, used verbatim in messages
            (e.g. ``"fault class"``, ``"constraint directive"``).
        threshold_pct: Share of the population above which the input is
            considered unusable.
        sample_limit: Verbatim sample records kept per distinct token.
        fatal: When True, :meth:`enforce` raises :class:`ThresholdExceeded`
            instead of only reporting.
    """

    def __init__(self, domain: str, threshold_pct: float,
                 sample_limit: int, fatal: bool) -> None:
        self.domain = domain
        self.threshold_pct = float(threshold_pct)
        self.sample_limit = max(0, int(sample_limit))
        self.fatal = bool(fatal)
        self._total = 0
        self._tokens: Dict[str, UnrecognisedToken] = {}

    def seen(self, n: int = 1) -> None:
        """Record *n* inspected records, recognised or not."""
        self._total += n

    def add(self, token: str, sample: str = "",
            line_number: Optional[int] = None) -> None:
        """Record one unrecognised *token*, retaining *sample* if there is room."""
        key = (token or "").strip() or "(empty)"
        entry = self._tokens.get(key)
        if entry is None:
            entry = UnrecognisedToken(token=key, first_line=line_number)
            self._tokens[key] = entry
        entry.count += 1
        if sample and len(entry.samples) < self.sample_limit:
            entry.samples.append(sample.strip())

    def report(self) -> UnrecognisedReport:
        """Snapshot the tracker as an :class:`UnrecognisedReport`."""
        tokens = sorted(self._tokens.values(),
                        key=lambda t: (-t.count, t.token))
        return UnrecognisedReport(
            domain=self.domain,
            total=self._total,
            tokens=tokens,
            threshold_pct=self.threshold_pct,
            fatal=self.fatal,
        )

    def enforce(self) -> UnrecognisedReport:
        """Return the report, raising when a fatal domain breached its limit."""
        report = self.report()
        if report.exceeded and self.fatal:
            raise ThresholdExceeded(
                domain=self.domain,
                count=report.count,
                total=report.total,
                pct=report.pct,
                threshold_pct=self.threshold_pct,
                tokens=[t.token for t in report.tokens],
            )
        for line in report.warnings():
            logger.warning(line)
        return report
