"""Fault-disposition detection, file discovery and the pre-disposition warning.

Fixture D is the pair of synthetic fault lists below: the same design before
and after a disposition step. The AU distribution is deliberately redrawn
across it -- ``AU.TC`` dominates the phase snapshot, ``AU.BB`` dominates the
final one -- because that reordering is the whole reason a pre-disposition
snapshot is dangerous to rank a fix plan from.

Nothing here is design-specific in product code: the waiver subclass is
discovered from the data, and every file is found by pattern.
"""

import os

import pytest

from atpg_coverage_debug_agent.analysis.disposition import (
    PRE_DISPOSITION_WARNING,
    STATE_POST,
    STATE_PRE,
    STATE_UNDETERMINED,
    classify_snapshot,
    discover_waiver_subclass,
    find_fault_list_candidates,
    select_fault_list,
    subclass_delta,
)
from atpg_coverage_debug_agent.analysis.statistics import (
    compute_statistics,
    exclude_subclass,
)
from atpg_coverage_debug_agent.config.analysis_config import AnalysisConfig
from atpg_coverage_debug_agent.diagnostics import ThresholdExceeded
from atpg_coverage_debug_agent.parser.fault_parser import (
    parse_fault_list_file_ex,
)

_HEADER = """FaultInformation {
 version : 1;
 FaultType (Stuck) {
  FaultList {
   FaultCollapsing : FALSE;
   Format : Identifier, Class, Location;
   Instance ("") {
"""
_FOOTER = """   }
  }
 }
}
"""


def _write_faults(path, rows):
    """Write an MTFI fault list holding ``{class: count}`` rows."""
    lines = [_HEADER]
    index = 0
    for cls, count in rows.items():
        for _ in range(count):
            lines.append(f'      {index % 2},  {cls},'
                         f'        "/top/blk{index % 4}/u_cell{index}/y";\n')
            index += 1
    lines.append(_FOOTER)
    path.write_text("".join(lines))
    return str(path)


# Fixture D. AU.TC leads before, AU.BB leads after -- the ranking flips.
PRE_ROWS = {"DS": 400, "DI": 40, "PU": 8, "PT": 4, "UU": 10, "TI": 12,
            "BL": 2, "RE": 4, "AU.TC": 120, "AU.SEQ": 90, "AU.BB": 60,
            "AU.PC": 30, "UO.AAB": 14, "UC": 6}
POST_ROWS = {"DS": 400, "DI": 40, "PU": 8, "PT": 4, "UU": 10, "TI": 12,
             "BL": 2, "RE": 4, "AU.TC": 22, "AU.SEQ": 18, "AU.BB": 60,
             "AU.PC": 14, "AU.DISPOSITION": 206, "UO.AAB": 14, "UC": 6}


@pytest.fixture
def faultlist_dir(tmp_path):
    """A faultlist directory shaped like a real run's."""
    d = tmp_path / "faultlist"
    d.mkdir()
    # Phase snapshots, a final post-disposition list, and partial files that
    # must never be mistaken for the population.
    _write_faults(d / "design.edt.sa.a0.atpg.ph1.faults.gz", PRE_ROWS)
    _write_faults(d / "design.edt.sa.a0.atpg.ph2.faults.gz", PRE_ROWS)
    _write_faults(d / "design.edt.sa.a0.atpg.faults.fd.gz", POST_ROWS)
    _write_faults(d / "design.detected.faults.gz", {"DS": 400})
    _write_faults(d / "block_disp_faults_orig.gz", {"AU.DISPOSITION": 206})
    _write_faults(d / "design.edt.sa.xxxx.mbist_disp_faults.gz",
                  {"AU.DISPOSITION": 12})
    return d


def _stats_for(path, config=None):
    config = config or AnalysisConfig()
    parsed = parse_fault_list_file_ex(str(path), config=config)
    return compute_statistics(parsed.records, config=config)


# ---------------------------------------------------------------------------
# Waiver-subclass discovery is from the data, never assumed
# ---------------------------------------------------------------------------
def test_the_waiver_subclass_is_discovered_from_the_fault_list(faultlist_dir):
    stats = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.faults.fd.gz")
    waiver, evidence, _ = discover_waiver_subclass(stats)
    assert waiver == "AU.DISPOSITION"
    assert any("matches a configured waiver pattern" in line
               for line in evidence)


def test_a_differently_named_waiver_subclass_is_still_found(tmp_path):
    """The run chooses the name; a different flow spells it differently."""
    path = _write_faults(tmp_path / "x.faults",
                         {"DS": 100, "AU.WAIVED": 40, "UC": 4})
    waiver, _, _ = discover_waiver_subclass(_stats_for(path))
    assert waiver == "AU.WAIVED"


def test_the_waiver_pattern_list_is_configurable(tmp_path):
    path = _write_faults(tmp_path / "x.faults",
                         {"DS": 100, "AU.SITE_SPECIFIC": 40})
    assert discover_waiver_subclass(_stats_for(path))[0] is None
    config = AnalysisConfig.from_dict(
        {"waiver_subclass_patterns": ["*.SITE_SPECIFIC"]})
    assert discover_waiver_subclass(_stats_for(path, config),
                                    config)[0] == "AU.SITE_SPECIFIC"


def test_no_waiver_is_adopted_when_nothing_matches(faultlist_dir):
    stats = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.ph1.faults.gz")
    waiver, _, candidates = discover_waiver_subclass(stats)
    assert waiver is None
    assert candidates == []


def test_only_a_coverage_loss_subclass_can_be_a_waiver_candidate(tmp_path):
    """A new DI.* subtype is not a waiver bucket however undocumented it is."""
    path = _write_faults(tmp_path / "x.faults",
                         {"DS": 100, "DI.BRAND_NEW": 20, "AU.BRAND_NEW": 5})
    _, _, candidates = discover_waiver_subclass(_stats_for(path))
    assert "AU.BRAND_NEW" in candidates
    assert "DI.BRAND_NEW" not in candidates


# ---------------------------------------------------------------------------
# Pre / post classification comes from contents, with the name as tie-break
# ---------------------------------------------------------------------------
def test_a_list_holding_the_waiver_subclass_is_post_disposition(faultlist_dir):
    path = faultlist_dir / "design.edt.sa.a0.atpg.faults.fd.gz"
    state = classify_snapshot(str(path), _stats_for(path))
    assert state.state == STATE_POST
    assert state.waiver_subclass == "AU.DISPOSITION"
    assert state.waiver_count == POST_ROWS["AU.DISPOSITION"]
    assert state.has_relevant_population


def test_a_phase_tagged_list_without_the_waiver_is_pre_disposition(
        faultlist_dir):
    path = faultlist_dir / "design.edt.sa.a0.atpg.ph2.faults.gz"
    state = classify_snapshot(str(path), _stats_for(path))
    assert state.state == STATE_PRE
    assert state.phase == 2
    assert state.is_pre
    assert not state.has_relevant_population


def test_an_untagged_list_without_the_waiver_is_undetermined(tmp_path):
    """Absence of a waiver is suggestive, never proof. Say so."""
    path = _write_faults(tmp_path / "mystery.faults", PRE_ROWS)
    state = classify_snapshot(path, _stats_for(path))
    assert state.state == STATE_UNDETERMINED
    assert any("provisional" in w for w in state.warnings)


def test_contents_beat_the_filename(faultlist_dir, tmp_path):
    """A phase tag on a list that HOLDS the waiver does not make it pre."""
    path = _write_faults(tmp_path / "design.atpg.ph3.faults", POST_ROWS)
    state = classify_snapshot(path, _stats_for(path))
    assert state.state == STATE_POST


@pytest.mark.parametrize("name,phase", [
    ("d.atpg.ph6.faults.gz", 6),
    ("d.atpg.phase12.faults.gz", 12),
    ("d.atpg.pass3.faults.gz", 3),
    ("d.atpg.faults.fd.gz", None),
])
def test_phase_tags_are_read_by_pattern(name, phase):
    assert AnalysisConfig().phase_of(name) == phase


# ---------------------------------------------------------------------------
# File discovery by pattern
# ---------------------------------------------------------------------------
def test_partial_files_are_never_fault_list_candidates(faultlist_dir):
    names = {c.name for c in find_fault_list_candidates(str(faultlist_dir))}
    assert "block_disp_faults_orig.gz" not in names
    assert "design.edt.sa.xxxx.mbist_disp_faults.gz" not in names
    assert "design.detected.faults.gz" not in names
    assert "design.edt.sa.a0.atpg.faults.fd.gz" in names


def test_a_directory_resolves_to_the_post_disposition_list(faultlist_dir):
    resolved, candidates, warnings = select_fault_list(str(faultlist_dir))
    assert os.path.basename(resolved) == "design.edt.sa.a0.atpg.faults.fd.gz"
    assert len(candidates) == 3
    assert any("Selected" in w for w in warnings)


def test_the_latest_phase_wins_when_no_final_list_exists(tmp_path):
    d = tmp_path / "faultlist"
    d.mkdir()
    for phase in (1, 2, 3):
        _write_faults(d / f"design.atpg.ph{phase}.faults", PRE_ROWS)
    resolved, _, _ = select_fault_list(str(d))
    assert os.path.basename(resolved) == "design.atpg.ph3.faults"


def test_an_explicit_file_is_honoured_and_the_better_candidate_named(
        faultlist_dir):
    """Silently analysing a file the caller did not name is worse than a
    wrong default -- it cannot be noticed."""
    path = faultlist_dir / "design.edt.sa.a0.atpg.ph1.faults.gz"
    resolved, candidates, _ = select_fault_list(str(path))
    assert resolved == str(path)

    state = classify_snapshot(str(path), _stats_for(path), candidates)
    assert state.resolved_path == str(path)
    assert state.better_candidate is not None
    assert os.path.basename(state.better_candidate) \
        == "design.edt.sa.a0.atpg.faults.fd.gz"
    assert any("sits beside the one analysed" in w for w in state.warnings)


def test_no_better_candidate_is_claimed_for_the_final_list(faultlist_dir):
    path = faultlist_dir / "design.edt.sa.a0.atpg.faults.fd.gz"
    _, candidates, _ = select_fault_list(str(path))
    state = classify_snapshot(str(path), _stats_for(path), candidates)
    assert state.better_candidate is None


def test_an_empty_directory_reports_rather_than_guesses(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    resolved, candidates, warnings = select_fault_list(str(d))
    assert candidates == []
    assert any("matched a fault-list pattern" in w for w in warnings)


# ---------------------------------------------------------------------------
# Fixture D: the ranking warning, and what the disposition actually moved
# ---------------------------------------------------------------------------
def test_reading_the_pre_disposition_list_emits_the_ranking_warning(
        faultlist_dir):
    path = faultlist_dir / "design.edt.sa.a0.atpg.ph1.faults.gz"
    _, candidates, _ = select_fault_list(str(path))
    state = classify_snapshot(str(path), _stats_for(path), candidates)
    assert PRE_DISPOSITION_WARNING in state.warnings
    assert "category ranking" in PRE_DISPOSITION_WARNING


def test_the_disposition_reorders_the_categories(faultlist_dir):
    """This is why the warning exists, asserted rather than asserted about."""
    pre = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.ph1.faults.gz")
    post = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.faults.fd.gz")
    relevant = exclude_subclass(post, "AU.DISPOSITION")

    def _top_loss(stats):
        return [s.subclass_id for s in stats.loss_stats][0]

    assert _top_loss(pre) == "AU.TC"
    assert _top_loss(relevant) == "AU.BB"


def test_the_subclass_delta_reports_what_moved(faultlist_dir):
    pre = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.ph1.faults.gz")
    post = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.faults.fd.gz")
    delta = subclass_delta(pre, post)
    assert delta["AU.DISPOSITION"] == POST_ROWS["AU.DISPOSITION"]
    assert delta["AU.TC"] == POST_ROWS["AU.TC"] - PRE_ROWS["AU.TC"]
    # Detected counts are unchanged; only the undetected population moves.
    assert "DS" not in delta
    assert "DI" not in delta


def test_both_populations_are_reported_for_a_post_disposition_list(
        faultlist_dir):
    post = _stats_for(faultlist_dir / "design.edt.sa.a0.atpg.faults.fd.gz")
    relevant = exclude_subclass(post, "AU.DISPOSITION")
    assert post.total_faults == sum(POST_ROWS.values())
    assert relevant.total_faults == (post.total_faults
                                     - POST_ROWS["AU.DISPOSITION"])
    assert post.census_balances and relevant.census_balances
    # The relevant column is the stricter denominator, so coverage reads higher.
    assert relevant.test_coverage > post.test_coverage


# ---------------------------------------------------------------------------
# Acceptance criterion 6: an unknown token fails loudly and is named
# ---------------------------------------------------------------------------
def test_an_invented_class_family_fails_loudly_and_names_the_token(tmp_path):
    path = _write_faults(tmp_path / "x.faults",
                         {"DS": 10, "ZQ.INVENTED": 40})
    with pytest.raises(ThresholdExceeded) as excinfo:
        parse_fault_list_file_ex(path, enforce=True)
    assert "ZQ.INVENTED" in str(excinfo.value)


def test_an_unknown_subclass_of_a_known_family_keeps_its_family_role(tmp_path):
    """New subclasses must not break the tool -- only new FAMILIES do."""
    config = AnalysisConfig()
    path = _write_faults(tmp_path / "x.faults",
                         {"DS": 100, "AU.BRAND_NEW_SUBTYPE": 20})
    stats = _stats_for(path, config)
    assert stats.census_balances
    assert stats.unrecognised_count == 0
    assert config.role_of("AU.BRAND_NEW_SUBTYPE").value == "AU"
