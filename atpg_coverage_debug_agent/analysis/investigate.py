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
from ..models import VerdictConfidence


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
def _enum_value(v: Any) -> Any:
    """Return ``.value`` for enums, else the object unchanged."""
    return getattr(v, "value", v)


def dotted_class_of(fr: Any) -> str:
    """Return the dotted category id (``AU.TC``) of a fault analysis result.

    Reads ``FaultRecord.dotted_class`` when present and falls back to the bare
    fault class, so it works for both live records and the lightweight objects
    :func:`rehydrate` builds from a serialised payload.
    """
    fault = getattr(fr, "fault", None)
    if fault is None:
        return ""
    dotted = getattr(fault, "dotted_class", None)
    if dotted:
        return str(dotted)
    return str(_enum_value(getattr(fault, "fault_class", "")) or "")


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
        # The ATPG tool's own dotted root-cause label (AU.TC, UO.AAB, ...).
        # This is the key the coverage triage groups on, so it must survive
        # the round-trip into the out-of-process MCP server.
        "dotted_class": dotted_class_of(fr),
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
            "raw_class_token": getattr(fr.fault, "raw_class_token", "") or "",
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


def scan_status(netlist: Any, target: str, fault_results: Any = None,
                design: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Answer "is this a scan cell?" strictly from netlist pin evidence.

    Three sources are tried, in descending order of what they can prove, and
    the answer always says which one it used:

    1. the live parsed netlist, which also supports the three corroborations;
    2. the instantiation recorded for this fault site during the analysis
       pass -- literally the same text, carried in the exported evidence, so
       an out-of-process server is not blind;
    3. nothing, in which case the verdict is ``unresolved``.

    Source 2 exists because of a concrete failure: in one session
    ``get_fault_detail`` returned instantiations with line numbers while this
    tool answered "no parsed netlist is available", and the agent is required
    to prove scan status *here*. Two tools disagreeing about whether the
    design was loaded silently closed off the agent's primary evidence path.

    Args:
        netlist: The parsed netlist, or ``None`` when out of process.
        target: Fault object or hierarchical instance path.
        fault_results: Analysed faults, whose recorded instantiations are the
            fallback pin evidence.
        design: The shared design handle (see :func:`serialize_design`), used
            to report accurately whether a netlist was parsed at all.

    Returns:
        The :meth:`~..analysis.scan_status.ScanStatus.as_dict` payload, with a
        ``source`` key naming the evidence it rests on.
    """
    from . import scan_status as scan_status_mod

    target = (target or "").strip()
    if not target:
        return {"error": "Provide a fault object or instance path."}

    if netlist is not None and getattr(netlist, "modules", None):
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

        payload = scan_status_mod.determine_scan_status(
            target, mapper, conn, netlist).as_dict()
        payload["source"] = "live parsed netlist"
        return payload

    match = _match_for_scan_evidence(fault_results, target)
    if match is not None:
        payload = scan_status_mod.classify_instantiation_text(
            target,
            instantiation=str(getattr(match, "scan_evidence", "") or ""),
            instance=getattr(getattr(match, "mapping", None),
                             "instance_name", None),
            cell_type=getattr(getattr(match, "mapping", None),
                              "cell_type", None),
        ).as_dict()
        payload["source"] = (
            "instantiation recorded during the analysis pass (same text "
            "get_fault_detail returns for this site)")
        return payload

    return _scan_status_unavailable(target, fault_results, design)


def _match_for_scan_evidence(fault_results: Any, target: str) -> Any:
    """Return the fault result whose recorded instantiation covers *target*."""
    if not fault_results:
        return None
    for fr in fault_results:
        if not str(getattr(fr, "scan_evidence", "") or "").strip():
            continue
        if _matches_fault(fr, target):
            return fr
    return None


def _scan_status_unavailable(target: str, fault_results: Any,
                             design: Optional[Dict[str, Any]]
                             ) -> Dict[str, Any]:
    """Build the unresolved answer, saying exactly what is and is not held.

    Never claims the design was not parsed when it was: the handle records
    that, and an inaccurate "not loaded" is what stopped the agent asking.
    """
    from . import scan_status as scan_status_mod

    parsed = bool((design or {}).get("netlist_parsed"))
    if parsed:
        blocker = (
            f"The netlist WAS parsed for this analysis "
            f"({(design or {}).get('modules', '?')} module(s), "
            f"{(design or {}).get('instances', '?')} instance(s)), but the "
            f"live object is not held in this process and no instantiation "
            f"was recorded for '{target}'. Either the object never mapped "
            f"onto an instance, or it is outside the analysed coverage-loss "
            f"population. Check get_fault_detail and diagnose_unresolved for "
            f"this site before concluding anything about its scan status.")
    else:
        blocker = (
            "No netlist was parsed for this analysis at all, so no pin "
            "evidence exists anywhere in this session. Re-run with the "
            "hierarchical netlist; fault-table fields carry no pin evidence.")
    payload = scan_status_mod.unresolved(target, blocker).as_dict()
    payload["source"] = "none"
    payload["netlist_parsed"] = parsed
    payload["sites_with_recorded_pin_evidence"] = sum(
        1 for fr in (fault_results or [])
        if str(getattr(fr, "scan_evidence", "") or "").strip())
    return payload


def diagnose_unresolved_tool(fault_results: Any, netlist: Any,
                             limit: int = 20,
                             design: Optional[Dict[str, Any]] = None
                             ) -> Dict[str, Any]:
    """Explain why fault objects failed to map onto the netlist.

    Recomputed from the live netlist when one is held; otherwise the
    diagnosis the analysis pass already produced is returned. The tool must
    not report "no netlist in this session" while the analysis that produced
    the session clearly had one.

    Args:
        fault_results: The analysed coverage-loss faults.
        netlist: The parsed netlist, or ``None``.
        limit: Max groups to return.
        design: The shared design handle, carrying the recorded diagnosis.

    Returns:
        The :meth:`~..analysis.unresolved.UnresolvedDiagnosis.as_dict`
        payload, with a ``source`` key naming where it came from.
    """
    from .unresolved import diagnose_unresolved

    if netlist is not None and getattr(netlist, "modules", None):
        payload = diagnose_unresolved(
            fault_results, netlist, max_groups=max(1, int(limit))).as_dict()
        payload["source"] = "recomputed from the live parsed netlist"
        return payload

    recorded = (design or {}).get("unresolved_diagnosis")
    if recorded:
        payload = dict(recorded)
        groups = list(payload.get("groups") or [])
        cap = max(1, int(limit))
        if len(groups) > cap:
            payload["groups"] = groups[:cap]
            payload["groups_total"] = len(groups)
            payload["groups_truncated"] = True
        payload["source"] = (
            "the diagnosis computed during the analysis pass, when the "
            "netlist was parsed")
        return payload

    if (design or {}).get("netlist_parsed"):
        return {"error": ("The netlist was parsed for this analysis but no "
                          "mapping diagnosis was recorded, which happens "
                          "when every fault object mapped successfully. "
                          "Check report_context for the mapped/unmapped "
                          "split before assuming otherwise."),
                "source": "design handle"}
    return {"error": ("No netlist was parsed for this analysis, so mapping "
                      "failures cannot be attributed. Re-run with the "
                      "hierarchical netlist."),
            "source": "none"}


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


def list_category_faults(fault_results: Any, subclass: str,
                         limit: int = 50, offset: int = 0,
                         full: bool = False) -> Dict[str, Any]:
    """Return every analysed fault in one dotted coverage-loss category.

    The triage names the categories worth debugging but reports only their
    counts. This walks the other way: given a category id, it returns the
    faults themselves, paged, so a whole bucket can be read without guessing
    at an instance substring.

    Args:
        fault_results: The analysed coverage-loss faults.
        subclass: A dotted category id such as ``AU.TC``, or a bare family
            such as ``AU``. Matching is exact and case-insensitive; it is not
            a substring match, so ``AU`` does not pull in ``AU.TC``.
        limit: Max rows in this page.
        offset: How many matches to skip, for paging through a large category.
        full: Include the heavier per-fault evidence instead of compact rows.

    Returns:
        The page plus the total match count, or an ``error`` when *subclass*
        is missing. A category that matches nothing returns an empty page with
        the ids that do exist, rather than a bare zero.
    """
    wanted = (subclass or "").strip().upper()
    if not wanted:
        return {"error": ("A dotted 'subclass' is required, for example "
                          "'AU.TC'. Call coverage_triage to see which "
                          "categories this run has.")}

    results = list(fault_results or [])
    matched = [fr for fr in results
               if dotted_class_of(fr).upper() == wanted]
    if not matched:
        available = sorted({dotted_class_of(fr) for fr in results
                            if dotted_class_of(fr)})
        return {
            "subclass": subclass,
            "total_matched": 0,
            "returned": 0,
            "faults": [],
            "available_subclasses": available,
            "note": ("No analysed fault carries this category id. Matching is "
                     "exact, so 'AU' and 'AU.TC' are different categories."),
        }

    start = max(0, int(offset))
    page = matched[start: start + max(1, int(limit))]
    payload: Dict[str, Any] = {
        "subclass": subclass,
        "total_matched": len(matched),
        "offset": start,
        "returned": len(page),
        "faults": [serialize_fault_result(fr, full=bool(full)) for fr in page],
    }
    if start + len(page) < len(matched):
        payload["more"] = True
        payload["next_offset"] = start + len(page)
    return payload


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
                    triage: Optional[Dict[str, Any]] = None,
                    context: Optional[Dict[str, Any]] = None,
                    design: Optional[Dict[str, Any]] = None,
                    stamp: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Serialise everything the investigative tools need into a plain dict.

    The result is JSON-serialisable so it can be written to a file and read by a
    separate MCP server process. When *netlist* is None, a caller-supplied
    *adjacency* (e.g. from a reloaded report) is used for path tracing. When a
    *compare* baseline payload is given, the regression tools are enabled, and
    when a *triage* payload is given the coverage-triage tools are enabled.

    The *stamp* names the design and the input files this evidence was built
    from. It is written into the file so an evidence file found on disk can
    always be tied back to its run, and never mistaken for a stale one left by
    an unrelated design.
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
    if stamp:
        evidence["stamp"] = stamp
    if compare:
        evidence["compare"] = compare
    if triage:
        evidence["triage"] = triage
    if context:
        evidence["context"] = context
    if design:
        evidence["design"] = design
    return evidence


def serialize_design(netlist: Any, report: Any = None) -> Dict[str, Any]:
    """Serialise the shared parsed-design handle.

    Every tool answers from this one handle, so no two tools can disagree
    about whether a design was loaded. It carries what the netlist *was*
    (parsed or not, and how big) plus the results the analysis pass already
    derived from it, so an out-of-process server answers from recorded
    evidence instead of denying the netlist ever existed.

    Args:
        netlist: The parsed netlist, or ``None``.
        report: The ``AnalysisReport``, for the design name, source paths and
            the recorded mapping diagnosis.

    Returns:
        A small JSON-serialisable dict. Always includes ``netlist_parsed``.
    """
    modules = getattr(netlist, "modules", None) or {}
    sources = dict(getattr(report, "sources", None) or {})
    diagnosis = getattr(report, "unresolved_diagnosis", None)
    payload: Dict[str, Any] = {
        "netlist_parsed": bool(modules),
        "design": sources.get("design"),
        "sources": {
            "netlist": sources.get("netlist"),
            "faults": sources.get("faults"),
            "constraints": sources.get("constraints"),
        },
        "modules": len(modules),
        "instances": sum(len(m.instances) for m in modules.values()),
        "top_module": getattr(netlist, "top_module", None),
        "note": ("The single parsed-design handle every tool answers from. "
                 "If 'netlist_parsed' is true, the design WAS read during the "
                 "analysis pass even when the live object is not held in this "
                 "process; a tool must not report the netlist as missing in "
                 "that case."),
    }
    if diagnosis is not None and hasattr(diagnosis, "as_dict"):
        payload["unresolved_diagnosis"] = diagnosis.as_dict()
    return payload


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


def serialize_context(report: Any) -> Dict[str, Any]:
    """Serialise the evidence-quality context the tools expose.

    These numbers already appear in the report, but the model could only see
    the truncated sample folded into its prompt. Everything here qualifies how
    far the other tools' answers can be trusted, so it needs to be queryable.

    Args:
        report: An ``AnalysisReport``, or ``None``.

    Returns:
        A JSON-serialisable dict, empty when there is nothing to report.
    """
    if report is None:
        return {}
    summary = getattr(report, "summary", None)
    payload: Dict[str, Any] = {}

    # The complete fault census travels inline, always. This is the tool the
    # model is told to call first, so a residual it cannot attribute must be
    # impossible from here on. Counts are small; it is samples and prose that
    # make a payload large, so completeness costs nothing worth saving.
    from .census import build_census

    payload["census"] = build_census(report).as_dict()

    if summary is not None:
        mapped = getattr(summary, "mapped_count", 0) or 0
        unmapped = getattr(summary, "unmapped_count", 0) or 0
        total_loss = mapped + unmapped
        payload["evidence"] = {
            "coverage_loss_faults": getattr(summary, "coverage_loss_count", 0),
            "mapped_onto_netlist": mapped,
            "never_mapped": unmapped,
            "mapped_share": (round(mapped / total_loss, 4)
                             if total_loss else None),
            "scan_status": dict(
                getattr(summary, "scan_evidence_counts", {}) or {}),
            "held_at_a_hard_constant": getattr(
                summary, "tied_constant_count", 0),
            "actionable_loss": getattr(summary, "actionable_loss_count", 0),
            "why_unmapped": dict(
                getattr(summary, "unresolved_causes", {}) or {}),
            "note": ("An unmapped fault has UNKNOWN connectivity, not zero. "
                     "Nothing structural may be concluded from one. Faults "
                     "held at a hard constant are undetectable by design and "
                     "are excluded from 'actionable_loss'."),
        }

    payload["patterns"] = [
        {"kind": g.kind, "key": g.key, "count": g.count,
         "sample_faults": list(g.sample_faults)}
        for g in (getattr(report, "pattern_groups", None) or [])
    ]
    payload["warnings"] = list(getattr(report, "warnings", None) or [])

    # What the inputs declared and how well they were understood. The model
    # must be able to tell "no constraint affects this fault" apart from "the
    # constraint file was only partly parsed", and must never quote a coverage
    # figure without knowing whether the census is collapsed.
    header = getattr(report, "fault_list_header", None)
    if header is not None:
        payload["fault_list"] = header.as_dict()

    diagnostics = getattr(report, "class_diagnostics", None)
    if diagnostics is not None and getattr(diagnostics, "tokens", None):
        payload["unrecognised_fault_classes"] = diagnostics.as_dict()

    constraint_status = getattr(report, "constraint_diagnostics", None)
    if constraint_status:
        payload["constraint_parsing"] = dict(constraint_status)
        payload["constraint_parsing"]["note"] = (
            "'unresolved' directives were recognised but could not be "
            "evaluated. While that count is non-zero, a fault with no "
            "constraint hit is NOT proven unconstrained."
        )

    statistics = getattr(report, "statistics", None)
    if statistics is not None and hasattr(statistics, "metrics"):
        payload["coverage_metrics"] = statistics.metrics()

    config_used = getattr(report, "analysis_config", None)
    if config_used:
        payload["analysis_config"] = dict(config_used)

    edits = getattr(report, "edits", None) or {}
    if edits:
        payload["waivers"] = {
            "excluded_classes": list(edits.get("excluded_classes", [])),
            "excluded_subtypes": list(edits.get("excluded_subtypes", [])),
            "excluded_ids": list(edits.get("excluded_ids", [])),
            "removed_count": edits.get("removed_count", 0),
            "note": edits.get("note", ""),
            "caveat": ("An analyst removed these faults from the totals. "
                       "Every count in this session is AFTER that removal."),
        }
    return payload


#: Sections :func:`report_context` can return, in the order it returns them.
#: ``census`` is first and is returned unconditionally: it is the one thing
#: that makes an unexplained residual impossible, and a caller must not be
#: able to filter it away by asking for something else.
CONTEXT_SECTIONS = ("census", "evidence", "coverage_metrics", "fault_list",
                    "unrecognised_fault_classes", "constraint_parsing",
                    "analysis_config", "patterns", "warnings", "waivers")


def report_context(context: Optional[Dict[str, Any]],
                   section: Optional[str] = None,
                   limit: int = 20) -> Dict[str, Any]:
    """Return the evidence-quality context, optionally one section of it.

    The complete fault census is always included, whatever *section* asks
    for, and it is never abridged. Everything else may be capped by *limit*;
    a capped list says so and reports its true length.
    """
    if not context:
        return {"error": ("No report context available. Run an analysis "
                          "first.")}
    wanted = (section or "").strip().lower()
    if wanted and wanted not in CONTEXT_SECTIONS:
        return {"error": f"Unknown section '{section}'. Use one of: "
                         + ", ".join(CONTEXT_SECTIONS)
                         + ", or leave it empty for all."}

    cap = max(1, int(limit))
    out: Dict[str, Any] = {}
    for key in CONTEXT_SECTIONS:
        value = context.get(key)
        if value is None:
            continue
        # The census is exempt from both the section filter and the cap.
        if key == "census":
            out[key] = value
            continue
        if wanted and key != wanted:
            continue
        if isinstance(value, list):
            out[key] = value[:cap]
            if len(value) > cap:
                out[f"{key}_total"] = len(value)
                out[f"{key}_truncated"] = True
        else:
            out[key] = value
    if len(out) <= 1 and wanted:
        out["note"] = "Nothing recorded for that section in this run."
        out["sections_available"] = [k for k in CONTEXT_SECTIONS
                                     if context.get(k)]
    return out


def report_insufficient_evidence(question: str, missing: str = "",
                                 would_settle_it: str = "") -> Dict[str, Any]:
    """Record that the evidence does not settle *question*.

    This exists so that "not determined" is a first-class action with its own
    tool call, rather than something the model has to phrase its way into
    against the pull of sounding helpful.
    """
    question = (question or "").strip()
    if not question:
        return {"error": "State the question you cannot answer."}
    return {
        "verdict": "insufficient_evidence",
        "confidence": VerdictConfidence.INSUFFICIENT.value,
        "question": question,
        "missing": (missing or "").strip(),
        "would_settle_it": (would_settle_it or "").strip(),
        "acknowledged": (
            "Recorded. Report this as the answer. Reporting that the "
            "available evidence does not support a conclusion is a correct "
            "result, and is preferred over a confident guess. Do not follow "
            "it with a speculative root cause. Note: this is for evidence "
            "that does NOT EXIST. If the evidence exists but you have not "
            "retrieved it yet -- a truncated tool result, a spilled payload, "
            "an uncalled tool -- retrieve it instead of declaring it "
            "unresolved."),
    }


def report_handoff_gap(observed: str, expected: str = "", where: str = "",
                       question: str = "",
                       context: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """Record that the numbers handed over do not reconcile with each other.

    This is deliberately a separate channel from
    :func:`report_insufficient_evidence`. That one means "the evidence does
    not exist"; this one means "the evidence I was given contradicts itself".
    Without somewhere to put the second, the path of least resistance for a
    language model is to close the arithmetic on its own -- which is exactly
    what produced an invented fault category holding a 133k residual, printed
    in a format that made it look like a real bucket.

    The correct behaviour is: report the inconsistency, name both figures, and
    stop. Never name or attribute the residual.

    Args:
        observed: What the digest or a tool actually reported.
        expected: What it should have been, and how that was derived.
        where: The section, tool or block the inconsistency was found in.
        question: The question the inconsistency blocks, if any.
        context: The report context, so the answer can quote the authoritative
            census the model should have been reconciling against.

    Returns:
        A structured complaint, plus the complete census when one is held.
    """
    observed = (observed or "").strip()
    if not observed:
        return {"error": ("State what you observed that does not reconcile, "
                          "quoting both figures.")}
    payload: Dict[str, Any] = {
        "verdict": "handoff_gap",
        "observed": observed,
        "expected": (expected or "").strip(),
        "where": (where or "").strip(),
        "blocked_question": (question or "").strip(),
        "acknowledged": (
            "Recorded as a defect in the hand-off, not in the design. Report "
            "it as such: name both figures and the block they came from, and "
            "stop. Do NOT invent a category, bucket or label for the "
            "difference, do not distribute it across existing categories, and "
            "do not compute any coverage metric from a class list that failed "
            "the sum check."),
    }
    census = (context or {}).get("census")
    if census:
        payload["authoritative_census"] = census
        payload["next_step"] = (
            "The complete census is included above and is the figure of "
            "record. If it reconciles, re-derive from it and say which block "
            "was the subset. If it does not reconcile either, the analyser "
            "itself is at fault; report that and stop.")
    else:
        payload["next_step"] = (
            "No census is held in this session, so the inconsistency cannot "
            "be resolved from here. Report it and stop.")
    return payload


def coverage_triage(triage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the fault-class breakdown and the categories chosen to debug."""
    if not triage:
        return dict(_NO_TRIAGE)
    stats = triage.get("statistics", {})
    categories = [c for c in stats.get("subclasses", []) if c.get("count")]
    counted = sum(int(c.get("count", 0) or 0) for c in categories)
    total = int(stats.get("total_faults", 0) or 0)
    return {
        "totals": {
            "total_faults": total,
            "detected": stats.get("detected_count", 0),
            "coverage_loss": stats.get("loss_count", 0),
            "detected_pct": stats.get("detected_pct", 0.0),
            "loss_pct": stats.get("loss_pct", 0.0),
        },
        # Emitted next to the categories so the sum check is a read, not an
        # exercise. 'categories' below is the COMPLETE class list.
        "census_check": {
            "categories": len(categories),
            "sum_of_category_counts": counted,
            "total_faults": total,
            "delta": total - counted,
            "reconciles": counted == total,
            "instruction": ("'categories' is complete and must sum to "
                            "total_faults. If it does not, call "
                            "report_handoff_gap and stop; do not name the "
                            "difference."),
        },
        "coverage_metrics": stats.get("metrics"),
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


def _rehydrated_dotted_class(d: Dict[str, Any]) -> str:
    """Recover a serialised fault row's dotted category id.

    Payloads written before this field existed are reconstructed from the
    class token when one is present, so an older evidence file still groups
    correctly instead of silently matching nothing.
    """
    dotted = str(d.get("dotted_class") or "").strip()
    if dotted:
        return dotted
    base = str(d.get("fault_class") or "").strip()
    token = str(d.get("raw_class_token") or "").strip()
    if base and "." in token:
        sub = token.split(".", 1)[1].strip().upper()
        if sub:
            return f"{base}.{sub}"
    return base


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
            dotted_class=_rehydrated_dotted_class(d),
            raw_class_token=d.get("raw_class_token", ""),
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
            "scan-in / shift-enable / scan-out pins, three corroborating "
            "checks, and a 'source' field naming the evidence used: the live "
            "netlist, or the instantiation recorded for that site during the "
            "analysis pass (the same text get_fault_detail returns). This is "
            "the ONLY admissible basis for a scan-status claim: when neither "
            "exists it returns 'Unresolved - scan status cannot be determined "
            "without netlist pin evidence' and says whether a netlist was "
            "parsed at all. Fault-table fan-in/fan-out/confidence values "
            "never decide scan status."),
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
    "list_category_faults": {
        "description": (
            "List every analysed fault in ONE coverage-loss category, by its "
            "dotted id (AU.TC, UO.AAB, ...). Use this to read a whole triage "
            "bucket: coverage_triage names the categories and gives counts, "
            "this returns the faults behind a count. Matching is exact, so "
            "'AU' and 'AU.TC' are different categories. Page with offset."),
        "params": {
            "subclass": {"type": "str",
                         "description": "dotted category id, e.g. 'AU.TC'"},
            "limit": {"type": "int", "default": 50,
                      "description": "max rows in this page"},
            "offset": {"type": "int", "default": 0,
                       "description": "matches to skip, for paging"},
            "full": {"type": "bool", "default": False,
                     "description": "include full per-fault evidence"},
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
    "report_context": {
        "description": (
            "Call this FIRST. Returns the COMPLETE fault census -- every "
            "class, grouped by coverage role, with the sum check already "
            "done -- plus the state of the evidence itself: how many faults "
            "mapped onto the netlist and how many did not (and why), the "
            "scan-status split, how much of the loss sits on hard constants, "
            "the coverage metrics with their formulas, the repeated "
            "structural patterns, the parser warnings, and any analyst "
            "waivers in force. The census is always included and is never "
            "abridged, so no class listing anywhere else can leave you with "
            "an unexplained residual. Check this BEFORE trusting a count."),
        "params": {
            "section": {"type": "str", "default": "",
                        "description": ("census | evidence | "
                                        "coverage_metrics | fault_list | "
                                        "unrecognised_fault_classes | "
                                        "constraint_parsing | "
                                        "analysis_config | patterns | "
                                        "warnings | waivers; empty for all. "
                                        "The census is returned either way")},
            "limit": {"type": "int", "default": 20,
                      "description": "max rows per list (never the census)"},
        },
    },
    "report_handoff_gap": {
        "description": (
            "Report that the numbers you were handed do not reconcile with "
            "each other -- a class list that does not sum to its stated "
            "total, two sections disagreeing, a metric computed from an "
            "incomplete census. This is NOT the same as missing evidence: it "
            "means the evidence contradicts itself. Use it the moment a sum "
            "check fails, and stop there. Never invent a category, bucket or "
            "label to hold the difference, and never compute a coverage "
            "metric from a class list that failed the check."),
        "params": {
            "observed": {"type": "str",
                         "description": ("what does not reconcile, quoting "
                                         "both figures")},
            "expected": {"type": "str", "default": "",
                         "description": "what it should have been, and why"},
            "where": {"type": "str", "default": "",
                      "description": ("the section, block or tool the "
                                      "inconsistency came from")},
            "question": {"type": "str", "default": "",
                         "description": "the question this blocks"},
        },
    },
    "report_insufficient_evidence": {
        "description": (
            "Declare that the available evidence does NOT settle the "
            "question. Use this instead of producing a plausible answer you "
            "cannot support: an honest 'not determined' is a correct result "
            "here, and a confident wrong root cause costs an engineer days. "
            "Say what is missing and what would settle it."),
        "params": {
            "question": {"type": "str",
                         "description": "the question you cannot answer"},
            "missing": {"type": "str", "default": "",
                        "description": "what the evidence does not show"},
            "would_settle_it": {"type": "str", "default": "",
                                "description": ("the specific artefact, run "
                                                "or measurement that would")},
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
             triage: Optional[Dict[str, Any]] = None,
             context: Optional[Dict[str, Any]] = None,
             design: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dispatch a tool *name* with *args* to its query function.

    This is the single entry point used by both the skills and the MCP server.
    Unknown parameters are ignored; missing ones fall back to defaults. When
    *adjacency* is provided (out-of-process MCP server), ``trace_path`` uses it
    instead of a live netlist. When *compare* (a baseline report payload) is
    provided, the regression tools become available, and when *triage* is
    provided the coverage-triage tools become available. *design* is the one
    parsed-design handle every tool answers from, so no two tools can
    disagree about whether the netlist was read.
    """
    args = dict(args or {})
    if name == "report_insufficient_evidence":
        return report_insufficient_evidence(
            question=str(args.get("question", "") or ""),
            missing=str(args.get("missing", "") or ""),
            would_settle_it=str(args.get("would_settle_it", "") or ""))
    if name == "report_handoff_gap":
        return report_handoff_gap(
            observed=str(args.get("observed", "") or ""),
            expected=str(args.get("expected", "") or ""),
            where=str(args.get("where", "") or ""),
            question=str(args.get("question", "") or ""),
            context=context)
    if name == "report_context":
        return report_context(
            context,
            section=str(args.get("section", "") or "") or None,
            limit=int(args.get("limit", 20) or 20))
    if name == "scan_status":
        return scan_status(netlist, str(args.get("target", "")),
                           fault_results=fault_results, design=design)
    if name == "diagnose_unresolved":
        return diagnose_unresolved_tool(
            fault_results, netlist,
            limit=int(args.get("limit", 20) or 20), design=design)
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
    if name == "list_category_faults":
        return list_category_faults(
            fault_results,
            subclass=str(args.get("subclass", "") or ""),
            limit=int(args.get("limit", 50) or 50),
            offset=int(args.get("offset", 0) or 0),
            full=bool(args.get("full", False)),
        )
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
