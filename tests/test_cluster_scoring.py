"""Tests for hierarchy clustering and the deterministic scoring policy."""

from __future__ import annotations

import pytest

from atpg_coverage_debug_agent.analysis.cluster import (
    MIN_TOP_SHARE,
    choose_depth,
    cluster_faults,
    drill_into,
)
from atpg_coverage_debug_agent.analysis.scoring import (
    BAND_HIGH,
    BAND_LOW,
    band,
    compute_scores,
    score_category,
)
from atpg_coverage_debug_agent.analysis.statistics import (
    compute_statistics,
    enrich_categories,
    select_categories,
)
from atpg_coverage_debug_agent.models import VerdictConfidence
from atpg_coverage_debug_agent.parser.fault_parser import parse_fault_list


def _fault(dotted: str, stuck: str, path: str):
    records, _ = parse_fault_list(f"{dotted} {stuck} {path}")
    return records[0]


def _spread(paths, dotted="AU.TC", stuck="1"):
    return [_fault(dotted, stuck, p) for p in paths]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
def test_clusters_group_by_shared_hierarchy_prefix():
    faults = _spread([
        "top/core/fifo/u1/Y",
        "top/core/fifo/u2/Y",
        "top/core/alu/u3/Y",
    ])
    report = cluster_faults(faults, label="AU.TC", depth=3)

    assert report.total_faults == 3
    assert report.top.prefix == "top/core/fifo"
    assert report.top.count == 2
    assert report.top.pct == pytest.approx(66.6667, abs=0.01)


def test_cluster_samples_are_verbatim_not_normalised():
    # Dotted separators must survive into the sample, since a rewritten path
    # will not resolve when pasted into a tool.
    faults = [_fault("AU.TC", "1", "top.core.fifo.u1.Y")]
    report = cluster_faults(faults, depth=2)
    assert report.top.samples == ["top.core.fifo.u1.Y"]


def test_cluster_tracks_the_stuck_at_split():
    faults = (_spread(["top/a/u1/Y", "top/a/u2/Y"], stuck="1")
              + _spread(["top/a/u3/Y"], stuck="0"))
    report = cluster_faults(faults, depth=2)
    top = report.top
    assert (top.sa1, top.sa0) == (2, 1)
    assert top.sa_asymmetry == pytest.approx(1 / 3)


def test_empty_population_clusters_without_error():
    report = cluster_faults([], label="AU.TC")
    assert report.total_faults == 0
    assert report.clusters == []
    assert report.top is None
    assert report.top_share == 0.0


def test_auto_depth_descends_while_the_loss_stays_localised():
    # Everything lives under one deep cone, so depth should keep descending.
    faults = _spread([f"top/core/fifo/bank{i}/u/Y" for i in range(20)])
    depth, note = choose_depth(faults)
    assert depth >= 3
    assert note
    assert cluster_faults(faults, depth=depth).top.prefix == "top/core/fifo"


def test_auto_depth_stops_before_the_grouping_fragments():
    # 40 sibling blocks: descending past level 2 leaves no dominant cluster.
    faults = _spread([f"top/blk{i}/u/Y" for i in range(40)])
    depth, _ = choose_depth(faults)
    report = cluster_faults(faults, depth=depth)
    assert report.top_share >= MIN_TOP_SHARE


def test_drill_into_expands_only_the_requested_prefix():
    faults = _spread([
        "top/core/fifo/u1/Y",
        "top/core/fifo/u2/Y",
        "top/core/alu/u3/Y",
    ])
    report = drill_into(faults, "top/core/fifo", extra_depth=1)
    assert report.total_faults == 2
    assert all(c.prefix.startswith("top/core/fifo") for c in report.clusters)


def test_drill_into_unknown_prefix_says_so_rather_than_failing():
    report = drill_into(_spread(["top/a/u1/Y"]), "top/does_not_exist")
    assert report.total_faults == 0
    assert "no faults found" in report.depth_note


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (0.0, "low"),
    (BAND_LOW - 0.01, "low"),
    (0.5, "medium"),
    (BAND_HIGH, "medium"),
    (BAND_HIGH + 0.01, "high"),
    (1.0, "high"),
])
def test_score_bands_are_fixed_thresholds(value, expected):
    assert band(value) == expected


def test_concentrated_loss_scores_high_concentration():
    faults = _spread(["top/core/fifo/u%d/Y" % i for i in range(19)]
                     + ["top/other/u/Y"])
    stats = compute_statistics(faults)
    clusters = cluster_faults(faults, depth=2)
    scores = compute_scores(stats.get("AU.TC"), clusters)
    assert scores.bands["concentration"] == "high"


def test_scores_stay_zero_when_clustering_is_unavailable():
    faults = _spread([f"top/a/u{i}/Y" for i in range(20)])
    stats = compute_statistics(faults)
    scores = compute_scores(stats.get("AU.TC"), None)
    assert scores.concentration == 0.0
    assert scores.depth == 0.0
    # The stuck-at split comes from the fault list, so it survives.
    assert scores.sa_asymmetry == pytest.approx(1.0)


def test_tiny_populations_are_not_scored_at_all():
    # A single fault is trivially 100% concentrated and 100% skewed. Reading
    # signal into that would manufacture strong evidence from nothing.
    faults = _spread(["top/odd/u/Y"])
    stats = compute_statistics(faults)
    verdict = score_category(stats.get("AU.TC"), cluster_faults(faults))

    assert "low_population" in verdict.patterns
    assert "concentrated_hotspot" not in verdict.patterns
    assert verdict.scores.concentration == 0.0
    assert verdict.confidence is VerdictConfidence.REDUCED
    assert verdict.actionable == "partial"


def test_sparse_tail_is_marked_not_worth_acting_on():
    faults = (_spread([f"top/blk{i}/u/Y" for i in range(200)], dotted="AU.PC")
              + [_fault("AU.CC", "1", "top/odd/u/Y")])
    stats = compute_statistics(faults)
    tail = stats.get("AU.CC")
    assert tail.pct < 1.0

    verdict = score_category(tail, cluster_faults(
        [f for f in faults if f.dotted_class == "AU.CC"]))
    assert "sparse_tail" in verdict.patterns
    assert verdict.actionable == "false"
    assert verdict.confidence is VerdictConfidence.REDUCED


def test_single_polarity_population_is_flagged_asymmetric():
    faults = _spread([f"top/core/u{i}/Y" for i in range(20)], stuck="1")
    stats = compute_statistics(faults)
    verdict = score_category(stats.get("AU.TC"), cluster_faults(faults))
    assert "high_sa_asymmetry" in verdict.patterns


def test_single_cluster_is_not_called_symmetric():
    # One cluster has no siblings to be symmetric with. A zero deviation must
    # not be read as an even distribution.
    faults = _spread([f"top/core/fifo/u{i}/Y" for i in range(20)])
    stats = compute_statistics(faults)
    clusters = cluster_faults(faults, depth=3)
    assert len(clusters.clusters) == 1

    verdict = score_category(stats.get("AU.TC"), clusters)
    assert verdict.scores.symmetry == 0.0
    assert "symmetric_distribution" not in verdict.patterns


def test_evenly_sized_siblings_are_called_symmetric():
    faults = _spread([f"top/blk{i}/u{j}/Y"
                      for i in range(4) for j in range(10)])
    stats = compute_statistics(faults)
    verdict = score_category(stats.get("AU.TC"),
                             cluster_faults(faults, depth=2))
    assert "symmetric_distribution" in verdict.patterns


def test_uncatalogued_subclass_routes_to_generic():
    faults = _spread([f"top/core/u{i}/Y" for i in range(20)],
                     dotted="AU.NOSUCHTHING")
    stats = compute_statistics(faults)
    verdict = score_category(stats.get("AU.NOSUCHTHING"),
                             cluster_faults(faults))
    assert verdict.route == "generic"


def test_verdict_reason_cites_evidence_in_the_required_order():
    faults = _spread([f"top/core/u{i}/Y" for i in range(20)])
    stats = compute_statistics(faults)
    verdict = score_category(stats.get("AU.TC"), cluster_faults(faults))
    reason = verdict.reason
    assert (reason.index("concentration") < reason.index("depth")
            < reason.index("asymmetry"))


def test_scoring_is_reproducible_for_identical_input():
    faults = _spread([f"top/core/fifo/u{i}/Y" for i in range(30)])
    stats = compute_statistics(faults)

    first = score_category(stats.get("AU.TC"), cluster_faults(faults))
    second = score_category(stats.get("AU.TC"), cluster_faults(faults))
    assert first.as_dict() == second.as_dict()


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------
def test_enrich_categories_attaches_clusters_and_verdicts():
    faults = (_spread([f"top/core/fifo/u{i}/Y" for i in range(30)])
              + _spread([f"top/ctrl/u{i}/Y" for i in range(20)],
                        dotted="UO.AAB", stuck="0"))
    stats = compute_statistics(faults)
    selected = enrich_categories(select_categories(stats), faults)

    assert selected
    for category in selected:
        assert category.clusters is not None
        assert category.verdict is not None
        assert category.clusters.label == category.subclass_id

    tc = next(c for c in selected if c.subclass_id == "AU.TC")
    assert tc.clusters.top.prefix.startswith("top/core")


def test_recommendations_carry_the_hotspot_and_verdict():
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )

    faults = _spread([f"top/core/fifo/u{i}/Y" for i in range(30)])
    stats = compute_statistics(faults)
    selected = enrich_categories(select_categories(stats), faults)
    rec = build_recommendations(stats, selected)[0]

    assert rec.hotspot.startswith("top/core")
    assert rec.actionable in ("true", "partial", "false")
    assert any("[clustering_hint]" in line for line in rec.evidence)
    assert any("where to look, not why" in line for line in rec.evidence)


def test_list_clusters_tool_reads_the_serialized_payload():
    from atpg_coverage_debug_agent.analysis import investigate
    from atpg_coverage_debug_agent.analysis.recommend import (
        build_recommendations,
    )

    faults = _spread([f"top/core/fifo/u{i}/Y" for i in range(30)])
    stats = compute_statistics(faults)
    selected = enrich_categories(select_categories(stats), faults)
    payload = investigate.serialize_triage(
        stats, selected, build_recommendations(stats, selected))

    data = investigate.run_tool(
        "list_clusters", {}, fault_results=[], constraints=[], netlist=None,
        triage=payload)
    assert data["categories"]
    top = data["categories"][0]["clusters"][0]
    assert top["prefix"].startswith("top/core")
    assert top["samples"]


def test_markdown_report_shows_where_the_loss_concentrates(
        sample_netlist_path, sample_faults_path):
    from atpg_coverage_debug_agent.app import run_analysis
    from atpg_coverage_debug_agent.reporting.markdown_report import (
        render_markdown,
    )

    text = render_markdown(
        run_analysis(sample_netlist_path, sample_faults_path, None))
    assert "### Where the loss concentrates" in text
    assert "is not a root cause" in text
