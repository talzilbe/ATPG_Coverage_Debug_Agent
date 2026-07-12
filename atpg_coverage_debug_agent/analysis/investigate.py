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
    row: Dict[str, Any] = {
        "fault_object": fr.fault.fault_object,
        "fault_class": _enum_value(fr.fault.fault_class),
        "instance": mapping.instance_name or None,
        "cell_type": mapping.cell_type or None,
        "confidence": _enum_value(mapping.confidence),
        "fan_in_count": len(fr.fan_in),
        "fan_out_count": len(fr.fan_out),
        "controllability_issue": bool(fr.controllability_issue),
        "observability_issue": bool(fr.observability_issue),
        "constraint_related": bool(fr.constraint_related),
        "scan_boundary_involved": bool(fr.scan_boundary_involved),
        "root_cause": _enum_value(fr.root_cause),
    }
    if full:
        row.update({
            "normalized_object": fr.fault.normalized_object,
            "fault_type": fr.fault.fault_type,
            "line_number": fr.fault.line_number,
            "matched_net": mapping.matched_net,
            "mapping_candidates": list(mapping.candidates or []),
            "mapping_evidence": list(mapping.evidence or []),
            "fan_in": list(fr.fan_in),
            "fan_out": list(fr.fan_out),
            "observed_facts": list(fr.observed_facts or []),
            "inferred_conclusions": list(fr.inferred_conclusions or []),
            "evidence": list(fr.evidence or []),
            "recommended_step": fr.recommended_step,
        })
    return row


def serialize_constraint(c: Any) -> Dict[str, Any]:
    return {
        "kind": getattr(c, "kind", None),
        "signal": getattr(c, "signal", None),
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
                    compare: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Serialise everything the investigative tools need into a plain dict.

    The result is JSON-serialisable so it can be written to a file and read by a
    separate MCP server process. When *netlist* is None, a caller-supplied
    *adjacency* (e.g. from a reloaded report) is used for path tracing. When a
    *compare* baseline payload is given, the regression tools are enabled.
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
    return evidence


class _Bag:
    """Minimal attribute container used to rehydrate serialised records."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


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
            fan_in=d.get("fan_in", []),
            fan_out=d.get("fan_out", []),
            controllability_issue=d.get("controllability_issue", False),
            observability_issue=d.get("observability_issue", False),
            constraint_related=d.get("constraint_related", False),
            scan_boundary_involved=d.get("scan_boundary_involved", False),
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
    "list_faults": {
        "description": (
            "List coverage-loss faults matching optional filters (fault class, "
            "instance substring, root-cause substring, or issue flags)."),
        "params": {
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
             compare: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dispatch a tool *name* with *args* to its query function.

    This is the single entry point used by both the skills and the MCP server.
    Unknown parameters are ignored; missing ones fall back to defaults. When
    *adjacency* is provided (out-of-process MCP server), ``trace_path`` uses it
    instead of a live netlist. When *compare* (a baseline report payload) is
    provided, the regression tools become available.
    """
    args = dict(args or {})
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
