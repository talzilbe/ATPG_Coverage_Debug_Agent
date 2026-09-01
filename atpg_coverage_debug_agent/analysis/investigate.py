"""Shared, deterministic query functions for interactive fault investigation.

Both the investigative *skills* (exposed to the HTTP tool-calling agent) and the
*MCP server* (exposed to the GitHub Copilot CLI) call into this one module so
the exact same auditable logic backs every tool, regardless of backend.

Every function operates purely on already-parsed / already-analysed data
(``fault_results``, ``constraints``, ``summary``, ``netlist``) and returns plain
JSON-serialisable Python (dicts / lists / scalars). Nothing here calls an LLM or
mutates its inputs.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import regression


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
def _enum_value(v: Any) -> Any:
    """Return ``.value`` for enums, else the object unchanged."""
    return getattr(v, "value", v)


def serialize_fault_result(fr: Any, full: bool = False) -> Dict[str, Any]:
    """Convert a ``FaultAnalysisResult`` into a JSON-serialisable dict.

    Args:
        fr:   The fault analysis result.
        full: When True, include the heavier fan-in/out lists, observed facts,
              inferred conclusions and full evidence. When False, return a
              compact summary row.
    """
    mapping = fr.mapping
    connectivity_known = bool(getattr(fr, "connectivity_known", True))
    row: Dict[str, Any] = {
        "fault_object": fr.fault.fault_object,
        "fault_class": _enum_value(fr.fault.fault_class),
        "instance": mapping.instance_name or None,
        "cell_type": mapping.cell_type or None,
        "confidence": _enum_value(mapping.confidence),
        # None, never 0, when the object never mapped: an unmapped object
        # carries no connectivity evidence whatsoever.
        "fan_in_count": len(fr.fan_in) if connectivity_known else None,
        "fan_out_count": len(fr.fan_out) if connectivity_known else None,
        "connectivity_known": connectivity_known,
        "controllability_issue": bool(fr.controllability_issue),
        "observability_issue": bool(fr.observability_issue),
        "constraint_related": bool(fr.constraint_related),
        # Tri-state: "yes" / "no" / "unknown".
        "scan_boundary_involved": getattr(
            fr, "scan_boundary_state",
            "yes" if fr.scan_boundary_involved else "no"),
        # Scan status of the cell itself, read from its pin list.
        "scan_cell_state": getattr(fr, "scan_cell_state", "unknown"),
        "root_cause": _enum_value(fr.root_cause),
    }
    tie = getattr(fr, "tie_driver", None)
    if tie:
        row["tie_driver"] = dict(tie)
    if full:
        row.update({
            "normalized_object": fr.fault.normalized_object,
            "fault_type": fr.fault.fault_type,
            "line_number": fr.fault.line_number,
            "matched_net": mapping.matched_net,
            "mapping_candidates": list(mapping.candidates or []),
            "mapping_evidence": list(mapping.evidence or []),
            "fan_in": list(fr.fan_in) if connectivity_known else None,
            "fan_out": list(fr.fan_out) if connectivity_known else None,
            "scan_evidence": getattr(fr, "scan_evidence", ""),
            "observed_facts": list(fr.observed_facts or []),
            "inferred_conclusions": list(fr.inferred_conclusions or []),
            "evidence": list(fr.evidence or []),
            "recommended_step": fr.recommended_step,
        })
        if not connectivity_known:
            row["connectivity_note"] = (
                "This object was never mapped onto the netlist. fan_in_count, "
                "fan_out_count and fan-in/out lists are null because nothing "
                "was measured -- not because the node is unconnected. Scan "
                "status cannot be determined from this row; netlist pin "
                "evidence is required."
            )
    return row


def scan_status(netlist: Any, target: str) -> Dict[str, Any]:
    """Answer "is this a scan cell?" strictly from netlist pin evidence.

    Args:
        netlist: The parsed netlist, or ``None`` when unavailable.
        target: Fault object or hierarchical instance path.

    Returns:
        The :meth:`~..analysis.scan_status.ScanStatus.as_dict` payload. With no
        netlist loaded the verdict is ``unresolved`` and ``answer`` is the
        required sentence -- a fault-table row can never decide scan status.
    """
    from . import scan_status as scan_status_mod

    target = (target or "").strip()
    if not target:
        return {"error": "Provide a fault object or instance path."}
    if netlist is None or not getattr(netlist, "modules", None):
        return scan_status_mod.unresolved(
            target,
            "No parsed netlist is available in this session. Load the "
            "hierarchical netlist and re-run; fault-table fields carry no pin "
            "evidence.",
        ).as_dict()

    conn = getattr(netlist, "_atpg_connectivity", None)
    mapper = getattr(netlist, "_atpg_mapper", None)
    if conn is None or mapper is None:
        from .connectivity import ConnectivityModel
        from .mapper import FaultMapper

        conn = ConnectivityModel(netlist)
        mapper = FaultMapper(conn)
        try:
            netlist._atpg_connectivity = conn
            netlist._atpg_mapper = mapper
        except Exception:  # pragma: no cover - immutable netlist stand-ins
            pass

    return scan_status_mod.determine_scan_status(
        target, mapper, conn, netlist).as_dict()


def diagnose_unresolved_tool(fault_results: Any, netlist: Any,
                             limit: int = 20) -> Dict[str, Any]:
    """Explain why fault objects failed to map onto the netlist.

    Args:
        fault_results: The analysed coverage-loss faults.
        netlist: The parsed netlist, or ``None``.
        limit: Max groups to return.

    Returns:
        The :meth:`~..analysis.unresolved.UnresolvedDiagnosis.as_dict` payload.
    """
    from .unresolved import diagnose_unresolved

    if netlist is None or not getattr(netlist, "modules", None):
        return {"error": ("No parsed netlist in this session, so mapping "
                          "failures cannot be attributed. Load the "
                          "hierarchical netlist and re-run.")}
    return diagnose_unresolved(fault_results, netlist,
                               max_groups=max(1, int(limit))).as_dict()


def serialize_constraint(c: Any) -> Dict[str, Any]:
    return {
        "kind": getattr(c, "kind", None),        "signal": getattr(c, "signal", None),
        "normalized_signal": getattr(c, "normalized_signal", None),
        "value": getattr(c, "value", None),
        "line_number": getattr(c, "line_number", None),
        "notes": getattr(c, "notes", ""),
        "raw_text": getattr(c, "raw_text", ""),
    }


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------
def _matches_fault(fr: Any, query: str) -> bool:
    """Case-insensitive substring match against a fault's identifying fields."""
    q = query.lower()
    return (
        q in (fr.fault.fault_object or "").lower()
        or q in (fr.fault.normalized_object or "").lower()
        or q in (fr.mapping.instance_name or "").lower()
    )


# ---------------------------------------------------------------------------
# Query functions (each returns JSON-serialisable data)
# ---------------------------------------------------------------------------
def list_faults(fault_results: Any, fault_class: Optional[str] = None,
                instance: Optional[str] = None, root_cause: Optional[str] = None,
                controllability_only: bool = False,
                observability_only: bool = False,
                constraint_related_only: bool = False,
                scan_boundary_only: bool = False,
                limit: int = 50) -> Dict[str, Any]:
    """Return coverage-loss faults matching the given filters (compact rows)."""
    results = fault_results or []
    fc = (fault_class or "").strip().upper()
    rc = (root_cause or "").strip().lower()
    inst = (instance or "").strip().lower()

    matched: List[Dict[str, Any]] = []
    for fr in results:
        if fc and _enum_value(fr.fault.fault_class).upper() != fc:
            continue
        if inst and inst not in (fr.mapping.instance_name or "").lower():
            continue
        if rc and rc not in _enum_value(fr.root_cause).lower():
            continue
        if controllability_only and not fr.controllability_issue:
            continue
        if observability_only and not fr.observability_issue:
            continue
        if constraint_related_only and not fr.constraint_related:
            continue
        if scan_boundary_only and not fr.scan_boundary_involved:
            continue
        matched.append(serialize_fault_result(fr, full=False))

    total = len(matched)
    capped = matched[: max(1, int(limit))]
    return {
        "total_matched": total,
        "returned": len(capped),
        "faults": capped,
        "filters": {
            "fault_class": fc or None,
            "instance": instance or None,
            "root_cause": rc or None,
            "controllability_only": controllability_only,
            "observability_only": observability_only,
            "constraint_related_only": constraint_related_only,
            "scan_boundary_only": scan_boundary_only,
        },
    }


def get_fault_detail(fault_results: Any, fault: str,
                     max_matches: int = 5) -> Dict[str, Any]:
    """Return full structural evidence for the fault(s) matching *fault*."""
    if not fault or not fault.strip():
        return {"error": "A 'fault' identifier (or substring) is required."}
    matches = [fr for fr in (fault_results or []) if _matches_fault(fr, fault)]
    detail = [serialize_fault_result(fr, full=True)
              for fr in matches[: max(1, int(max_matches))]]
    return {
        "query": fault,
        "total_matched": len(matches),
        "returned": len(detail),
        "faults": detail,
    }


def why_blocked(fault_results: Any, fault: str) -> Dict[str, Any]:
    """Explain, per matching fault, whether loss is controllability/observability."""
    if not fault or not fault.strip():
        return {"error": "A 'fault' identifier (or substring) is required."}
    out: List[Dict[str, Any]] = []
    for fr in (fault_results or []):
        if not _matches_fault(fr, fault):
            continue
        ctrl = bool(fr.controllability_issue)
        obsv = bool(fr.observability_issue)
        if ctrl and obsv:
            verdict = "both controllability and observability"
        elif ctrl:
            verdict = "controllability (activation)"
        elif obsv:
            verdict = "observability (propagation)"
        else:
            verdict = "neither flagged — see root cause / evidence"
        out.append({
            "fault_object": fr.fault.fault_object,
            "fault_class": _enum_value(fr.fault.fault_class),
            "instance": fr.mapping.instance_name,
            "verdict": verdict,
            "controllability_issue": ctrl,
            "observability_issue": obsv,
            "constraint_related": bool(fr.constraint_related),
            "scan_boundary_involved": bool(fr.scan_boundary_involved),
            "root_cause": _enum_value(fr.root_cause),
            "observed_facts": list(fr.observed_facts or []),
            "evidence": list(fr.evidence or []),
            "recommended_step": fr.recommended_step,
        })
    return {"query": fault, "total_matched": len(out), "faults": out}


def suggest_test_points(fault_results: Any, limit: int = 20,
                        min_fanout: int = 0,
                        focus: str = "all") -> Dict[str, Any]:
    """Rank coverage-loss faults by impact and propose a concrete DFT fix.

    Each coverage-loss fault is assigned a primary *lever* (observability,
    controllability, constraint, or scan-boundary), a concrete recommended
    action, and an impact score derived from its fan-in / fan-out, then the
    suggestions are returned highest-impact first.
    """
    focus = (focus or "all").strip().lower()
    items: List[Dict[str, Any]] = []
    for fr in (fault_results or []):
        fo = fr.fault.fault_object
        inst = fr.mapping.instance_name or "-"
        fan_in = len(fr.fan_in)
        fan_out = len(fr.fan_out)
        if fan_out < int(min_fanout):
            continue
        cls = _enum_value(fr.fault.fault_class)
        obsv = bool(fr.observability_issue) or cls == "UO"
        ctrl = bool(fr.controllability_issue) or cls == "UC"

        if obsv:
            kind = "observability"
            action = (f"Add an observation/test point downstream of instance "
                      f"'{inst}' so this node becomes observable in test mode.")
            score = fan_out * 2 + fan_in
            rationale = (f"Unobserved with fan-out={fan_out}; an observe point "
                         "recovers this node and amplifies coverage over its "
                         "downstream cone.")
        elif ctrl:
            kind = "controllability"
            action = (f"Add a control/test point to make instance '{inst}' "
                      "controllable in test mode.")
            score = fan_in * 2 + fan_out
            rationale = (f"Uncontrolled with fan-in={fan_in}; a control point "
                         "enables fault activation.")
        elif bool(fr.constraint_related):
            kind = "constraint"
            action = (f"Review and, if safe, relax the constraint blocking "
                      f"instance '{inst}'.")
            score = fan_out + fan_in
            rationale = ("Fault is constraint-related; relaxing the blocking "
                         "constraint may recover it.")
        elif bool(fr.scan_boundary_involved):
            kind = "scan"
            action = (f"Insert scan at the non-scan boundary near instance "
                      f"'{inst}'.")
            score = fan_out + fan_in
            rationale = ("A scan/non-scan boundary is involved; scan insertion "
                         "improves access.")
        else:
            kind = "other"
            action = (f"Investigate instance '{inst}' manually; no dominant "
                      "test-point lever was detected.")
            score = fan_out + fan_in
            rationale = ("No single controllability/observability/constraint "
                         "lever dominates.")

        if focus != "all" and focus != kind:
            continue
        items.append({
            "fault_object": fo,
            "instance": inst,
            "kind": kind,
            "suggested_action": action,
            "rationale": rationale,
            "root_cause": _enum_value(fr.root_cause),
            "fan_in": fan_in,
            "fan_out": fan_out,
            "score": score,
        })

    items.sort(key=lambda x: x["score"], reverse=True)
    total = len(items)
    return {
        "total": total,
        "returned": min(total, int(limit)),
        "suggestions": items[: max(1, int(limit))],
    }


def list_constraints(constraints: Any, name: Optional[str] = None,
                     kind: Optional[str] = None,
                     limit: int = 100) -> Dict[str, Any]:
    """Return parsed constraints, optionally filtered by signal name / kind."""
    items = constraints or []
    nm = (name or "").strip().lower()
    kd = (kind or "").strip().lower()
    matched: List[Dict[str, Any]] = []
    for c in items:
        if nm and nm not in ((getattr(c, "signal", "") or "").lower()
                             + (getattr(c, "normalized_signal", "") or "").lower()):
            continue
        if kd and kd != (getattr(c, "kind", "") or "").lower():
            continue
        matched.append(serialize_constraint(c))
    return {
        "total_matched": len(matched),
        "returned": min(len(matched), int(limit)),
        "constraints": matched[: max(1, int(limit))],
    }


def trace_path(netlist: Any, from_instance: str, to_instance: str,
               max_depth: int = 8) -> Dict[str, Any]:
    """Structurally trace a driver→load path between two instances.

    Uses the connectivity model (bounded BFS). Returns the shortest path found
    within *max_depth* hops, or a report that none exists in that bound.
    """
    if not from_instance or not to_instance:
        return {"error": "Both 'from_instance' and 'to_instance' are required."}
    if netlist is None:
        return {"error": "No netlist is available for path tracing."}

    try:
        from .connectivity import ConnectivityModel
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Connectivity model unavailable: {exc}"}

    model = ConnectivityModel(netlist)

    def _keys_matching(name: str) -> List[str]:
        nl = name.lower()
        return [k for k, inst in model.instances.items()
                if nl in inst.name.lower() or nl in k.lower()]

    starts = _keys_matching(from_instance)
    goals = set(_keys_matching(to_instance))
    if not starts:
        return {"error": f"No instance matches from_instance='{from_instance}'."}
    if not goals:
        return {"error": f"No instance matches to_instance='{to_instance}'."}

    depth_cap = max(1, int(max_depth))
    for start in starts:
        visited = {start}
        # BFS frontier of (key, path)
        frontier: List[tuple] = [(start, [model.instances[start].name])]
        depth = 0
        while frontier and depth < depth_cap:
            nxt: List[tuple] = []
            for key, path in frontier:
                for succ in model.downstream(key):
                    if succ in goals:
                        return {
                            "found": True,
                            "from": model.instances[start].name,
                            "to": model.instances[succ].name,
                            "hops": len(path),
                            "path": path + [model.instances[succ].name],
                        }
                    if succ not in visited:
                        visited.add(succ)
                        nxt.append(
                            (succ, path + [model.instances[succ].name]))
            frontier = nxt
            depth += 1

    return {
        "found": False,
        "from_instance": from_instance,
        "to_instance": to_instance,
        "max_depth": depth_cap,
        "note": ("No structural driver→load path found within the depth bound. "
                 "The signals may be in different cones, separated by a "
                 "non-scan/black-box boundary, or the bound is too small."),
    }


def trace_path_adjacency(adjacency: Dict[str, List[str]], from_instance: str,
                         to_instance: str, max_depth: int = 8) -> Dict[str, Any]:
    """Bounded BFS path trace over a pre-computed instance-name adjacency map.

    Used by the out-of-process MCP server, which receives a serialised
    adjacency rather than the live netlist object.
    """
    if not from_instance or not to_instance:
        return {"error": "Both 'from_instance' and 'to_instance' are required."}
    adjacency = adjacency or {}
    nodes = set(adjacency.keys())
    for succs in adjacency.values():
        nodes.update(succs)

    def _matching(name: str) -> List[str]:
        nl = name.lower()
        return [n for n in nodes if nl in n.lower()]

    starts = _matching(from_instance)
    goals = set(_matching(to_instance))
    if not starts:
        return {"error": f"No instance matches from_instance='{from_instance}'."}
    if not goals:
        return {"error": f"No instance matches to_instance='{to_instance}'."}

    depth_cap = max(1, int(max_depth))
    for start in starts:
        visited = {start}
        frontier: List[tuple] = [(start, [start])]
        depth = 0
        while frontier and depth < depth_cap:
            nxt: List[tuple] = []
            for node, path in frontier:
                for succ in adjacency.get(node, []):
                    if succ in goals:
                        return {"found": True, "from": start, "to": succ,
                                "hops": len(path), "path": path + [succ]}
                    if succ not in visited:
                        visited.add(succ)
                        nxt.append((succ, path + [succ]))
            frontier = nxt
            depth += 1

    return {
        "found": False,
        "from_instance": from_instance,
        "to_instance": to_instance,
        "max_depth": depth_cap,
        "note": ("No structural path found within the depth bound over the "
                 "serialised adjacency."),
    }


# ---------------------------------------------------------------------------
# Evidence export / rehydration (for the out-of-process MCP server)
# ---------------------------------------------------------------------------
def build_adjacency(netlist: Any) -> Dict[str, List[str]]:
    """Build an instance-name → downstream-instance-names map from a netlist."""
    if netlist is None:
        return {}
    try:
        from .connectivity import ConnectivityModel
    except Exception:  # noqa: BLE001
        return {}
    model = ConnectivityModel(netlist)
    adj: Dict[str, List[str]] = {}
    for key, inst in model.instances.items():
        succ_names: List[str] = []
        for sk in model.downstream(key):
            si = model.instances.get(sk)
            if si and si.name != inst.name:
                succ_names.append(si.name)
        if succ_names:
            bucket = adj.setdefault(inst.name, [])
            for n in succ_names:
                if n not in bucket:
                    bucket.append(n)
    return adj


def export_evidence(fault_results: Any, constraints: Any,
                    netlist: Any,
                    adjacency: Optional[Dict[str, List[str]]] = None,
                    compare: Optional[Dict[str, Any]] = None,
                    triage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Serialise everything the investigative tools need into a plain dict.

    The result is JSON-serialisable so it can be written to a file and read by a
    separate MCP server process. When *netlist* is None, a caller-supplied
    *adjacency* (e.g. from a reloaded report) is used for path tracing. When a
    *compare* baseline payload is given, the regression tools are enabled, and
    when a *triage* payload is given the coverage-triage tools are enabled.
    """
    if netlist is not None:
        adj = build_adjacency(netlist)
    else:
        adj = adjacency or {}
    evidence = {
        "faults": [serialize_fault_result(fr, full=True)
                   for fr in (fault_results or [])],
        "constraints": [serialize_constraint(c) for c in (constraints or [])],
        "adjacency": adj,
    }
    if compare:
        evidence["compare"] = compare
    if triage:
        evidence["triage"] = triage
    return evidence


def serialize_triage(statistics: Any, selected: Any,
                     recommendations: Any) -> Dict[str, Any]:
    """Serialise the triage results into the payload the tools consume.

    Args:
        statistics: A ``DerivedStatistics``, or ``None``.
        selected: ``SelectedCategory`` objects, or ``None``.
        recommendations: ``Recommendation`` objects, or ``None``.

    Returns:
        A JSON-serialisable dict, empty when there is nothing to report.
    """
    if statistics is None:
        return {}
    return {
        "statistics": statistics.as_dict(),
        "selected": [
            {
                "rank": c.rank,
                "subclass": c.subclass_id,
                "count": c.stat.count,
                "pct": round(c.stat.pct, 4),
                "reason": c.reason,
                "verdict": (c.verdict.as_dict()
                            if getattr(c, "verdict", None) else None),
                "clusters": (c.clusters.as_dict()
                             if getattr(c, "clusters", None) else None),
                "attribution": (c.attribution.as_dict()
                                if getattr(c, "attribution", None) else None),
                "reachability": (c.reachability.as_dict()
                                 if getattr(c, "reachability", None) else None),
            }
            for c in (selected or [])
        ],
        "recommendations": [r.as_dict() for r in (recommendations or [])],
    }


_NO_TRIAGE = {
    "error": ("No coverage triage available. Run an analysis first — triage is "
              "derived from the fault list during the analysis pass.")
}


def coverage_triage(triage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the fault-class breakdown and the categories chosen to debug."""
    if not triage:
        return dict(_NO_TRIAGE)
    stats = triage.get("statistics", {})
    return {
        "totals": {
            "total_faults": stats.get("total_faults", 0),
            "detected": stats.get("detected_count", 0),
            "coverage_loss": stats.get("loss_count", 0),
            "detected_pct": stats.get("detected_pct", 0.0),
            "loss_pct": stats.get("loss_pct", 0.0),
        },
        "note": ("Percentages are aggregated from the fault list. They are not "
                 "the ATPG tool's test-coverage figure, which also accounts "
                 "for fault collapsing and untestable-fault credit."),
        "categories": [c for c in stats.get("subclasses", [])
                       if c.get("count")],
        "selected": triage.get("selected", []),
    }


def recommend_fixes(triage: Optional[Dict[str, Any]],
                    subclass: Optional[str] = None,
                    limit: int = 10) -> Dict[str, Any]:
    """Return ranked fix proposals, optionally filtered to one subclass."""
    if not triage:
        return dict(_NO_TRIAGE)
    rows = list(triage.get("recommendations", []))
    if subclass:
        key = subclass.strip().upper()
        rows = [r for r in rows if str(r.get("subclass", "")).upper() == key]
    limit = max(1, int(limit or 10))
    return {
        "total": len(rows),
        "returned": min(len(rows), limit),
        "note": ("Commands are for you to run in your own ATPG session. Where "
                 "'requires_measurement' is true, no coverage gain is "
                 "predicted — only a re-run establishes the benefit."),
        "recommendations": rows[:limit],
    }


def explain_subclass(subclass: str) -> Dict[str, Any]:
    """Explain what a dotted fault subclass means and how it is usually fixed."""
    from .recommend import explain_category  # local: keeps import graph flat

    return explain_category(subclass)


def list_clusters(triage: Optional[Dict[str, Any]],
                  subclass: Optional[str] = None,
                  limit: int = 10) -> Dict[str, Any]:
    """Return where each category's faults concentrate in the hierarchy."""
    if not triage:
        return dict(_NO_TRIAGE)

    rows = []
    for entry in triage.get("selected", []):
        if subclass and str(entry.get("subclass", "")).upper() != \
                subclass.strip().upper():
            continue
        clusters = entry.get("clusters")
        if not clusters:
            continue
        trimmed = dict(clusters)
        trimmed["clusters"] = clusters.get("clusters", [])[:max(1, limit)]
        rows.append({"subclass": entry.get("subclass"), **trimmed})

    if not rows:
        return {
            "categories": [],
            "note": ("No clustering is available. It is rebuilt from the fault "
                     "paths during analysis."),
        }
    return {
        "note": ("A dominant prefix shows where faults concentrate, not why "
                 "they are there. Sample paths are verbatim and can be pasted "
                 "into a tool unmodified."),
        "categories": rows,
    }


def list_blocking_sources(triage: Optional[Dict[str, Any]],
                          subclass: Optional[str] = None) -> Dict[str, Any]:
    """Return the constant drivers and constrained signals blocking faults."""
    if not triage:
        return dict(_NO_TRIAGE)

    rows = []
    for entry in triage.get("selected", []):
        if subclass and str(entry.get("subclass", "")).upper() != \
                subclass.strip().upper():
            continue
        attribution = entry.get("attribution")
        if attribution:
            rows.append(attribution)

    if not rows:
        return {
            "categories": [],
            "note": ("No blocking structure was attributed. Only AU.TC and "
                     "AU.PC are traced, and only when the faults map onto "
                     "netlist objects."),
        }
    return {
        "note": ("Derived by tracing fan-in cones through the netlist. This "
                 "is an estimate of what blocks the faults, not the ATPG "
                 "tool's own attribution."),
        "categories": rows,
    }


def profile_fault_sites(triage: Optional[Dict[str, Any]],
                        subclass: Optional[str] = None) -> Dict[str, Any]:
    """Return why aborted faults were structurally hard to test."""
    if not triage:
        return dict(_NO_TRIAGE)

    rows = []
    for entry in triage.get("selected", []):
        if subclass and str(entry.get("subclass", "")).upper() != \
                subclass.strip().upper():
            continue
        profile = entry.get("reachability")
        if profile:
            rows.append(profile)

    if not rows:
        return {
            "categories": [],
            "note": ("No structural profile is available. Only aborted "
                     "categories (UC.AAB, UO.AAB, UC, UO) are profiled, and "
                     "only when the faults map onto netlist objects."),
        }
    return {
        "note": ("Estimated from the netlist. A bottleneck and a reconvergent "
                 "cone need opposite fixes — more abort budget helps the "
                 "former and is wasted on the latter — so check the dominant "
                 "signature before acting."),
        "categories": rows,
    }


def verify_paths(fault_results: Any, constraints: Any, netlist: Any = None,
                 paths: Any = None, text: str = "") -> Dict[str, Any]:
    """Check hierarchy paths against the source artefacts before quoting them.

    A path that was shortened with an ellipsis, or assembled from plausible
    looking parts, will not resolve when pasted into a tool. Verify anything
    you intend to quote.
    """
    from . import guardrails

    registry = guardrails.PathRegistry.from_parts(
        fault_results=fault_results or (), constraints=constraints or (),
        netlist=netlist)

    candidates: List[str] = []
    if isinstance(paths, str):
        candidates = [p for p in re.split(r"[,\s]+", paths) if p]
    elif paths:
        candidates = [str(p) for p in paths]

    checked = []
    for path in candidates:
        issue = registry.validate(path, context="verify_paths")
        checked.append({
            "path": path,
            "ok": issue is None,
            "problem": issue.kind if issue else "",
        })

    scanned = ([i.as_dict() for i in
                guardrails.check_text(text, registry, "verify_paths")]
               if text else [])

    return {
        "source_paths_known": len(registry),
        "checked": checked,
        "text_issues": scanned,
        "note": ("A path is accepted when it matches a source path exactly or "
                 "is a component-aligned prefix of one, such as a cluster "
                 "prefix. Anything else was not in the inputs."),
    }


class _Bag:
    """Minimal attribute container used to rehydrate serialised records."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _connectivity_known(d: Dict[str, Any]) -> bool:
    """Whether a serialised fault row carries measured connectivity."""
    if "connectivity_known" in d:
        return bool(d["connectivity_known"])
    # Older payloads: an unresolved mapping never had connectivity measured.
    return str(d.get("confidence", "")).lower() != "unresolved"


def _scan_state(d: Dict[str, Any]) -> str:
    """Normalise the tri-state scan column of a serialised fault row."""
    if not _connectivity_known(d):
        return "unknown"
    raw = d.get("scan_boundary_involved", False)
    if isinstance(raw, str):
        return raw if raw in ("yes", "no", "unknown") else "no"
    return "yes" if raw else "no"


def rehydrate(evidence: Dict[str, Any]):
    """Rebuild attribute objects from an ``export_evidence`` dict.

    Returns ``(fault_results, constraints, adjacency)`` where the first two are
    lists of lightweight objects exposing the same attributes the query
    functions read, so the identical logic can run out-of-process.
    """
    faults = []
    for d in evidence.get("faults", []):
        fault = _Bag(
            fault_object=d.get("fault_object"),
            normalized_object=d.get("normalized_object", d.get("fault_object")),
            fault_class=d.get("fault_class"),
            fault_type=d.get("fault_type"),
            line_number=d.get("line_number"),
        )
        mapping = _Bag(
            instance_name=d.get("instance"),
            cell_type=d.get("cell_type"),
            confidence=d.get("confidence"),
            matched_net=d.get("matched_net"),
            candidates=d.get("mapping_candidates", []),
            evidence=d.get("mapping_evidence", []),
        )
        faults.append(_Bag(
            fault=fault,
            mapping=mapping,
            fan_in=d.get("fan_in") or [],
            fan_out=d.get("fan_out") or [],
            connectivity_known=_connectivity_known(d),
            controllability_issue=d.get("controllability_issue", False),
            observability_issue=d.get("observability_issue", False),
            constraint_related=d.get("constraint_related", False),
            scan_boundary_involved=_scan_state(d) == "yes",
            scan_boundary_state=_scan_state(d),
            scan_cell_state=d.get("scan_cell_state", "unknown"),
            scan_evidence=d.get("scan_evidence", ""),
            tie_driver=d.get("tie_driver"),
            root_cause=d.get("root_cause"),
            observed_facts=d.get("observed_facts", []),
            inferred_conclusions=d.get("inferred_conclusions", []),
            evidence=d.get("evidence", []),
            recommended_step=d.get("recommended_step", ""),
        ))
    constraints = [
        _Bag(kind=c.get("kind"), signal=c.get("signal"),
             normalized_signal=c.get("normalized_signal"), value=c.get("value"),
             line_number=c.get("line_number"), notes=c.get("notes", ""),
             raw_text=c.get("raw_text", ""))
        for c in evidence.get("constraints", [])
    ]
    return faults, constraints, evidence.get("adjacency", {})



# ---------------------------------------------------------------------------
# Tool metadata shared by skills and the MCP server
# ---------------------------------------------------------------------------
#: Machine-readable descriptions of every investigative tool. Each entry maps a
#: tool name to ``(description, parameter_schema)`` where parameter_schema is a
#: dict of ``param -> {type, description, default?}`` using skill-style types.
TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "scan_status": {
        "description": (
            "Decide whether an instance is a SCAN cell by reading its actual "
            "netlist instantiation. Returns the verbatim instantiation, the "
            "scan-in / shift-enable / scan-out pins, and three corroborating "
            "checks. This is the ONLY admissible basis for a scan-status "
            "claim: without a netlist, or when the object does not map, it "
            "returns 'Unresolved - scan status cannot be determined without "
            "netlist pin evidence.' Fault-table fan-in/fan-out/confidence "
            "values never decide scan status."),
        "params": {
            "target": {"type": "str",
                       "description": "fault object or hierarchical "
                                      "instance path"},
        },
    },
    "diagnose_unresolved": {
        "description": (
            "Explain why fault objects failed to map onto the netlist, "
            "grouped by cause: absent_leaf (the cell model is missing from "
            "the netlist), ambiguous (the name repeats and the path did not "
            "narrow it), or outside_scope (the fault list and netlist cover "
            "different blocks). Unmapped faults have UNKNOWN connectivity, "
            "so no root cause on them is provable until this is fixed."),
        "params": {
            "limit": {"type": "int", "default": 20,
                      "description": "max groups to return"},
        },
    },
    "list_faults": {
        "description": (
            "List coverage-loss faults matching optional filters (fault class, "
            "instance substring, root-cause substring, or issue flags)."),        "params": {
            "fault_class": {"type": "str", "description": "AU, UO, or UC"},
            "instance": {"type": "str", "description": "instance-name substring"},
            "root_cause": {"type": "str", "description": "root-cause substring"},
            "controllability_only": {"type": "bool", "default": False,
                                     "description": "only controllability issues"},
            "observability_only": {"type": "bool", "default": False,
                                   "description": "only observability issues"},
            "constraint_related_only": {"type": "bool", "default": False,
                                        "description": "only constraint-related"},
            "scan_boundary_only": {"type": "bool", "default": False,
                                   "description": "only scan-boundary faults"},
            "limit": {"type": "int", "default": 50,
                      "description": "max rows to return"},
        },
    },
    "get_fault_detail": {
        "description": (
            "Return full structural evidence (mapping, fan-in/out, observed "
            "facts, evidence, recommended step) for the fault(s) matching a "
            "fault-object or instance substring."),
        "params": {
            "fault": {"type": "str",
                      "description": "fault object / instance substring"},
            "max_matches": {"type": "int", "default": 5,
                            "description": "max faults to detail"},
        },
    },
    "why_blocked": {
        "description": (
            "Explain whether the coverage loss for matching fault(s) is due to "
            "controllability, observability, both, constraints, or scan "
            "boundary — with the supporting observed facts."),
        "params": {
            "fault": {"type": "str",
                      "description": "fault object / instance substring"},
        },
    },
    "list_constraints": {
        "description": (
            "List parsed constraints, optionally filtered by signal-name "
            "substring or constraint kind."),
        "params": {
            "name": {"type": "str", "description": "signal-name substring"},
            "kind": {"type": "str",
                     "description": "constraint kind (force/disable/...)"},
            "limit": {"type": "int", "default": 100,
                      "description": "max rows to return"},
        },
    },
    "suggest_test_points": {
        "description": (
            "Rank coverage-loss faults by impact and propose concrete DFT "
            "fixes (observation points, control points, constraint relaxation, "
            "or scan insertion), highest-impact first."),
        "params": {
            "limit": {"type": "int", "default": 20,
                      "description": "max suggestions to return"},
            "min_fanout": {"type": "int", "default": 0,
                           "description": "only faults with fan-out >= this"},
            "focus": {"type": "str", "default": "all",
                      "description": ("observability | controllability | "
                                      "constraint | scan | all")},
        },
    },
    "trace_path": {
        "description": (
            "Structurally trace a driver->load path between two instances "
            "(bounded BFS). Reports the shortest path found or that none "
            "exists within the depth bound."),
        "params": {
            "from_instance": {"type": "str",
                              "description": "source instance-name substring"},
            "to_instance": {"type": "str",
                            "description": "target instance-name substring"},
            "max_depth": {"type": "int", "default": 8,
                          "description": "max hops to search"},
        },
    },
    "regression_summary": {
        "description": (
            "Summarise the regression vs the loaded baseline report: counts of "
            "regressed / fixed / changed coverage-loss faults, net delta, and "
            "per-class deltas. Requires a comparison report to be loaded."),
        "params": {},
    },
    "list_regressed": {
        "description": (
            "List faults that are coverage-loss now but were NOT in the "
            "baseline report (new coverage loss). Requires a comparison "
            "report."),
        "params": {
            "limit": {"type": "int", "default": 50,
                      "description": "max rows to return"},
        },
    },
    "list_fixed": {
        "description": (
            "List faults that were coverage-loss in the baseline report but no "
            "longer are (improvements). Requires a comparison report."),
        "params": {
            "limit": {"type": "int", "default": 50,
                      "description": "max rows to return"},
        },
    },
    "list_changed": {
        "description": (
            "List faults present in both reports whose fault class or root "
            "cause changed. Requires a comparison report."),
        "params": {
            "limit": {"type": "int", "default": 50,
                      "description": "max rows to return"},
        },
    },
    "coverage_triage": {
        "description": (
            "Break the fault list down by Tessent fault class and dotted "
            "subclass (AU.PC, AU.TC, UO.AAB, ...) with stuck-at split, and "
            "report which coverage-loss categories were selected for "
            "investigation. Start here to decide what to debug."),
        "params": {},
    },
    "recommend_fixes": {
        "description": (
            "Return ranked, evidence-backed fix proposals for the selected "
            "coverage-loss categories, each with rationale, preconditions, "
            "copyable Tessent commands and caveats."),
        "params": {
            "subclass": {"type": "str", "default": "",
                         "description": ("restrict to one dotted subclass, "
                                         "e.g. 'AU.TC'; empty for all")},
            "limit": {"type": "int", "default": 10,
                      "description": "max proposals to return"},
        },
    },
    "explain_subclass": {
        "description": (
            "Explain what a dotted fault subclass means, what usually causes "
            "it, what evidence would confirm it, and which fixes apply. "
            "Works without an analysis loaded."),
        "params": {
            "subclass": {"type": "str",
                         "description": "dotted class id, e.g. 'UO.AAB'"},
        },
    },
    "list_clusters": {
        "description": (
            "Show where each coverage-loss category concentrates in the "
            "design hierarchy, with fault counts, stuck-at split and verbatim "
            "sample paths. Tells you WHERE to look, never why."),
        "params": {
            "subclass": {"type": "str", "default": "",
                         "description": ("restrict to one dotted subclass; "
                                         "empty for all")},
            "limit": {"type": "int", "default": 10,
                      "description": "max clusters per category"},
        },
    },
    "list_blocking_sources": {
        "description": (
            "Name what is blocking the faults: constant drivers (tie cells, "
            "test data registers, unscanned flops) for AU.TC and constrained "
            "signals for AU.PC, found by tracing fan-in cones. Structural "
            "estimate, not the ATPG tool's own attribution."),
        "params": {
            "subclass": {"type": "str", "default": "",
                         "description": ("restrict to one dotted subclass; "
                                         "empty for all")},
        },
    },
    "profile_fault_sites": {
        "description": (
            "Explain why aborted faults (UC.AAB / UO.AAB / UC / UO) were hard "
            "to test: low controllability, hard observability gap, "
            "observability bottleneck, reconvergent complexity or sequential "
            "depth explosion. These call for different fixes, so check this "
            "before recommending test points or a higher abort limit."),
        "params": {
            "subclass": {"type": "str", "default": "",
                         "description": ("restrict to one dotted subclass; "
                                         "empty for all")},
        },
    },
    "verify_paths": {
        "description": (
            "Check hierarchy paths against the source artefacts before "
            "quoting them. Use this for any path you are about to put in an "
            "answer: a shortened or reconstructed path will not resolve when "
            "pasted into a tool. Also flags coverage-gain claims that have "
            "not been measured by a re-run."),
        "params": {
            "paths": {"type": "str", "default": "",
                      "description": "whitespace/comma separated paths"},
            "text": {"type": "str", "default": "",
                     "description": ("optional prose to scan for bad paths "
                                     "and unmeasured claims")},
        },
    },
}


def serialize_report_for_compare(fault_results: Any, summary: Any,
                                 constraints: Any,
                                 label: str = "") -> Dict[str, Any]:
    """Serialise a report into the compact 'compare' payload used by the
    regression tools (baseline side)."""
    faults = [serialize_fault_result(fr, full=False)
              for fr in (fault_results or [])]
    summ = {}
    if summary is not None:
        summ = {
            "total_faults": getattr(summary, "total_faults", 0),
            "coverage_loss_count": getattr(summary, "coverage_loss_count", 0),
            "class_counts": dict(getattr(summary, "class_counts", {}) or {}),
        }
    return {
        "label": label,
        "faults": faults,
        "summary": summ,
        "constraints": [serialize_constraint(c) for c in (constraints or [])],
    }


def run_tool(name: str, args: Dict[str, Any], *, fault_results: Any,
             constraints: Any, netlist: Any,
             adjacency: Optional[Dict[str, List[str]]] = None,
             compare: Optional[Dict[str, Any]] = None,
             triage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dispatch a tool *name* with *args* to its query function.

    This is the single entry point used by both the skills and the MCP server.
    Unknown parameters are ignored; missing ones fall back to defaults. When
    *adjacency* is provided (out-of-process MCP server), ``trace_path`` uses it
    instead of a live netlist. When *compare* (a baseline report payload) is
    provided, the regression tools become available, and when *triage* is
    provided the coverage-triage tools become available.
    """
    args = dict(args or {})
    if name == "scan_status":
        return scan_status(netlist, str(args.get("target", "")))
    if name == "diagnose_unresolved":
        return diagnose_unresolved_tool(
            fault_results, netlist,
            limit=int(args.get("limit", 20) or 20))
    if name == "coverage_triage":
        return coverage_triage(triage)
    if name == "recommend_fixes":
        return recommend_fixes(
            triage,
            subclass=str(args.get("subclass", "") or "") or None,
            limit=int(args.get("limit", 10) or 10))
    if name == "explain_subclass":
        return explain_subclass(str(args.get("subclass", "")))
    if name == "list_clusters":
        return list_clusters(
            triage,
            subclass=str(args.get("subclass", "") or "") or None,
            limit=int(args.get("limit", 10) or 10))
    if name == "list_blocking_sources":
        return list_blocking_sources(
            triage, subclass=str(args.get("subclass", "") or "") or None)
    if name == "profile_fault_sites":
        return profile_fault_sites(
            triage, subclass=str(args.get("subclass", "") or "") or None)
    if name == "verify_paths":
        return verify_paths(
            fault_results, constraints, netlist,
            paths=args.get("paths", ""), text=str(args.get("text", "") or ""))
    if name == "list_faults":
        return list_faults(
            fault_results,
            fault_class=args.get("fault_class"),
            instance=args.get("instance"),
            root_cause=args.get("root_cause"),
            controllability_only=bool(args.get("controllability_only", False)),
            observability_only=bool(args.get("observability_only", False)),
            constraint_related_only=bool(
                args.get("constraint_related_only", False)),
            scan_boundary_only=bool(args.get("scan_boundary_only", False)),
            limit=int(args.get("limit", 50) or 50),
        )
    if name == "get_fault_detail":
        return get_fault_detail(
            fault_results, fault=str(args.get("fault", "")),
            max_matches=int(args.get("max_matches", 5) or 5))
    if name == "why_blocked":
        return why_blocked(fault_results, fault=str(args.get("fault", "")))
    if name == "list_constraints":
        return list_constraints(
            constraints, name=args.get("name"), kind=args.get("kind"),
            limit=int(args.get("limit", 100) or 100))
    if name == "suggest_test_points":
        return suggest_test_points(
            fault_results,
            limit=int(args.get("limit", 20) or 20),
            min_fanout=int(args.get("min_fanout", 0) or 0),
            focus=str(args.get("focus", "all") or "all"))
    if name == "trace_path":
        frm = str(args.get("from_instance", ""))
        to = str(args.get("to_instance", ""))
        depth = int(args.get("max_depth", 8) or 8)
        if adjacency is not None:
            return trace_path_adjacency(adjacency, frm, to, depth)
        return trace_path(netlist, from_instance=frm, to_instance=to,
                          max_depth=depth)
    if name in ("regression_summary", "list_regressed", "list_fixed",
                "list_changed"):
        if not compare:
            return {"error": ("No baseline/comparison report loaded. Use "
                              "'Compare Report' to load one first.")}
        current = [serialize_fault_result(fr) for fr in (fault_results or [])]
        baseline = compare.get("faults", [])
        if name == "regression_summary":
            return regression.summary(
                baseline, current, compare.get("summary"),
                {"class_counts": _current_class_counts(fault_results)},
                label=compare.get("label", ""))
        d = regression.diff(baseline, current)
        limit = max(1, int(args.get("limit", 50) or 50))
        if name == "list_regressed":
            return {"total": d["counts"]["regressed"],
                    "faults": d["regressed"][:limit]}
        if name == "list_fixed":
            return {"total": d["counts"]["fixed"], "faults": d["fixed"][:limit]}
        return {"total": d["counts"]["changed"], "faults": d["changed"][:limit]}
    return {"error": f"Unknown tool '{name}'."}


def _current_class_counts(fault_results: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for fr in (fault_results or []):
        cls = _enum_value(fr.fault.fault_class)
        counts[cls] = counts.get(cls, 0) + 1
    return counts
