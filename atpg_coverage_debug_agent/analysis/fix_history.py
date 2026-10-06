"""What earlier re-runs measured for each fix, so the plan can learn from it.

Comparing a report from before a fix with one from after it says, per
category, how many faults actually left the coverage loss. Those outcomes are
kept in a small per-user ledger and fed back into the ranking: a fix that has
recovered faults before is promoted, one that repeatedly recovered nothing is
demoted. The ledger only ever holds measured before/after counts.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HISTORY_ENV = "ATPG_FIX_HISTORY"

#: A fix that recovered at least this share of its category on average is
#: treated as proven for that category.
PROVEN_SHARE = 0.5

#: Below this average share, after at least MIN_TRIALS_FAILED trials, a fix is
#: treated as not working for that category.
FAILED_SHARE = 0.05
MIN_TRIALS_FAILED = 2


def history_path() -> Path:
    override = os.environ.get(HISTORY_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".atpg_debug_agent" / "fix_history.json"


@dataclass
class FixOutcome:
    """Measured before/after for one recommended fix's category."""

    rank: int
    subclass: str
    fix_id: str
    title: str
    before: int
    after: int
    #: Faults of the category that are no longer coverage loss at all.
    recovered: int
    #: Faults still lost but now in a different category.
    moved: int
    #: Faults in the category that were not in it before.
    new: int

    @property
    def recovered_share(self) -> float:
        return self.recovered / self.before if self.before else 0.0

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["recovered_share"] = round(self.recovered_share, 4)
        return data


def _key(fr: Any) -> Tuple[str, str]:
    fault = fr.fault
    obj = (getattr(fault, "normalized_object", "") or fault.fault_object or "")
    return obj.lower(), str(getattr(fault, "fault_type", "") or "")


def _by_class(report: Any) -> Dict[Tuple[str, str], str]:
    return {_key(fr): (fr.fault.dotted_class or "")
            for fr in (getattr(report, "fault_results", None) or [])}


def evaluate_fix_outcomes(baseline: Any, current: Any) -> List[FixOutcome]:
    """Measure each baseline recommendation's category against *current*."""
    from .fix_plan_edits import effective_plan

    before = _by_class(baseline)
    after = _by_class(current)
    outcomes: List[FixOutcome] = []
    seen = set()
    for rec in effective_plan(baseline):
        if getattr(rec, "superseded", False):
            continue
        key = (rec.fix.fix_id, rec.subclass_id)
        if key in seen:
            continue
        seen.add(key)
        sub = rec.subclass_id
        was = {k for k, c in before.items() if c == sub}
        now = {k for k, c in after.items() if c == sub}
        recovered = sum(1 for k in was if k not in after)
        moved = sum(1 for k in was if k in after and after[k] != sub)
        outcomes.append(FixOutcome(
            rank=rec.rank, subclass=sub, fix_id=rec.fix.fix_id,
            title=rec.fix.title, before=len(was), after=len(now),
            recovered=recovered, moved=moved, new=len(now - was)))
    return outcomes


def load_history(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = Path(path) if path else history_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read fix history %s: %s", path, exc)
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def record_outcomes(outcomes: List[FixOutcome], design: str = "",
                    path: Optional[Path] = None) -> int:
    """Append *outcomes* to the ledger; returns how many were written."""
    path = Path(path) if path else history_path()
    entries = load_history(path)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    written = 0
    for outcome in outcomes:
        if outcome.before <= 0:
            continue
        entry = outcome.as_dict()
        entry.update({"design": design, "recorded": stamp})
        entries.append(entry)
        written += 1
    if written:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return written


def history_stats(path: Optional[Path] = None
                  ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(fix_id, subclass) -> trials, faults before, recovered, average share."""
    stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in load_history(path):
        key = (str(entry.get("fix_id", "")), str(entry.get("subclass", "")))
        row = stats.setdefault(key, {"trials": 0, "before": 0, "recovered": 0,
                                     "share_sum": 0.0})
        row["trials"] += 1
        row["before"] += int(entry.get("before", 0) or 0)
        row["recovered"] += int(entry.get("recovered", 0) or 0)
        row["share_sum"] += float(entry.get("recovered_share", 0.0) or 0.0)
    for row in stats.values():
        row["share"] = row.pop("share_sum") / row["trials"]
    return stats


def verdict(row: Optional[Dict[str, Any]]) -> str:
    """``proven`` / ``not_working`` / ``mixed`` / ``untried``."""
    if not row:
        return "untried"
    if row["share"] >= PROVEN_SHARE:
        return "proven"
    if row["trials"] >= MIN_TRIALS_FAILED and row["share"] < FAILED_SHARE:
        return "not_working"
    return "mixed"


def describe(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    return (f"Past outcomes: recovered {row['recovered']:,} of "
            f"{row['before']:,} fault(s) over {row['trials']} measured "
            f"re-run(s) (average {row['share']:.0%}).")
