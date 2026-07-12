"""Deterministic regression diff between two ATPG coverage reports.

Compares the coverage-loss fault set of a *baseline* report against the
*current* report and classifies every difference:

* **regressed** — a fault that is coverage-loss now but was not in the baseline
  (new coverage loss),
* **fixed** — a fault that was coverage-loss in the baseline but no longer is,
* **changed** — a fault present in both whose fault class or root cause changed.

All inputs are plain serialised fault dicts (see
:func:`atpg_coverage_debug_agent.analysis.investigate.serialize_fault_result`)
so this module has no dependency on the live analysis objects and is trivially
testable and reusable across processes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _key(f: Dict[str, Any]) -> str:
    return f.get("fault_object", "")


def _compact(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fault_object": f.get("fault_object"),
        "fault_class": f.get("fault_class"),
        "root_cause": f.get("root_cause"),
        "instance": f.get("instance"),
    }


def diff(baseline_faults: List[Dict[str, Any]],
         current_faults: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Classify differences between baseline and current coverage-loss faults."""
    base_map = {_key(f): f for f in (baseline_faults or []) if _key(f)}
    cur_map = {_key(f): f for f in (current_faults or []) if _key(f)}
    base_ids = set(base_map)
    cur_ids = set(cur_map)

    regressed = [_compact(cur_map[i]) for i in sorted(cur_ids - base_ids)]
    fixed = [_compact(base_map[i]) for i in sorted(base_ids - cur_ids)]

    changed: List[Dict[str, Any]] = []
    for i in sorted(cur_ids & base_ids):
        b, c = base_map[i], cur_map[i]
        if (b.get("fault_class") != c.get("fault_class")
                or b.get("root_cause") != c.get("root_cause")):
            changed.append({
                "fault_object": i,
                "base_class": b.get("fault_class"),
                "new_class": c.get("fault_class"),
                "base_root_cause": b.get("root_cause"),
                "new_root_cause": c.get("root_cause"),
                "instance": c.get("instance"),
            })

    return {
        "counts": {
            "baseline_loss": len(base_ids),
            "current_loss": len(cur_ids),
            "regressed": len(regressed),
            "fixed": len(fixed),
            "changed": len(changed),
            "net_delta": len(cur_ids) - len(base_ids),
        },
        "regressed": regressed,
        "fixed": fixed,
        "changed": changed,
    }


def _class_deltas(base_summary: Optional[Dict[str, Any]],
                  cur_summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base_counts = (base_summary or {}).get("class_counts", {}) or {}
    cur_counts = (cur_summary or {}).get("class_counts", {}) or {}
    classes = sorted(set(base_counts) | set(cur_counts))
    return {
        cls: {"baseline": int(base_counts.get(cls, 0)),
              "current": int(cur_counts.get(cls, 0)),
              "delta": int(cur_counts.get(cls, 0)) - int(base_counts.get(cls, 0))}
        for cls in classes
    }


def summary(baseline_faults: List[Dict[str, Any]],
            current_faults: List[Dict[str, Any]],
            base_summary: Optional[Dict[str, Any]] = None,
            cur_summary: Optional[Dict[str, Any]] = None,
            label: str = "") -> Dict[str, Any]:
    """High-level regression summary: counts, net delta, and per-class deltas."""
    d = diff(baseline_faults, current_faults)
    return {
        "baseline_label": label or "baseline",
        "counts": d["counts"],
        "class_deltas": _class_deltas(base_summary, cur_summary),
        "sample_regressed": [f["fault_object"] for f in d["regressed"][:10]],
        "sample_fixed": [f["fault_object"] for f in d["fixed"][:10]],
    }
