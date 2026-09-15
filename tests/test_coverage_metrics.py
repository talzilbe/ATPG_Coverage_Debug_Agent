"""The coverage metrics must reproduce what the ATPG tool reports.

The fixtures below are the three columns of one real ``report_statistics``
listing. They exist because the tool's arithmetic is not obvious: it gives a
possibly-detected fault no credit in test or fault coverage, credits only
``PU`` (never ``PT``) in ATPG effectiveness, and -- the part that is easiest to
get wrong -- reports effectiveness over the TOTAL population in both columns
rather than re-basing it on the relevant one.

No identifier or count from that run appears in product code; they are pinned
here and nowhere else.
"""

import pytest

from atpg_coverage_debug_agent.analysis.statistics import (
    DerivedStatistics,
    SubclassStat,
    exclude_subclass,
)
from atpg_coverage_debug_agent.config.analysis_config import (
    AnalysisConfig,
    CoverageRole,
)

# --- Fixture A: a pre-disposition snapshot of one run ----------------------
# DT=767594 FU=860480 UD=10237 AU=80029 PU=961 PT=227
FIXTURE_A = [
    ("DS", "DS", 691617),
    ("DI", "DI", 75977),
    ("PU", "PU", 961),
    ("PT", "PT", 227),
    ("UU", "UU", 5820),
    ("TI", "TI", 2213),
    ("BL", "BL", 236),
    ("RE", "RE", 1968),
    ("AU.BB", "AU", 32001),
    ("AU.TC", "AU", 17629),
    ("AU.SEQ", "AU", 16091),
    ("AU.PC", "AU", 6219),
    ("AU.MPO", "AU", 4789),
    ("AU", "AU", 3300),
    ("UC", "UC", 321),
    ("UO", "UO", 1111),
]
EXPECT_A = {"FU": 860480, "tc": 90.28, "fc": 89.21, "eff": 99.81}

# --- Fixtures B and C: the same run AFTER the disposition step -------------
# The tool prints two columns from this one population:
#   C = "total"           FU=860480, tc 89.87%, fc 89.21%
#   B = "total relevant"  FU=818619, tc 94.51%, fc 93.77%  (waiver excluded)
# Effectiveness reads 99.88% in BOTH columns.
WAIVER_SUBCLASS = "AU.DISPOSITION"
FIXTURE_POST = [
    ("DS", "DS", 691617),
    ("DI", "DI", 75977),
    ("PU", "PU", 752),
    ("PT", "PT", 110),
    ("UU", "UU", 3370),
    ("TI", "TI", 1738),
    ("BL", "BL", 98),
    ("RE", "RE", 1202),
    ("AU.BB", "AU", 21513),
    ("AU.PC", "AU", 4769),
    ("AU.TC", "AU", 5564),
    ("AU.MPO", "AU", 4476),
    ("AU.SEQ", "AU", 4111),
    (WAIVER_SUBCLASS, "AU", 41861),
    ("AU", "AU", 2358),
    ("UC", "UC", 129),
    ("UO", "UO", 835),
]
EXPECT_C = {"FU": 860480, "tc": 89.87, "fc": 89.21, "eff": 99.88}
EXPECT_B = {"FU": 818619, "tc": 94.51, "fc": 93.77, "eff": 99.88}


def _stats(rows, config=None):
    """Build a population from ``(subclass, family, count)`` rows."""
    config = config or AnalysisConfig()
    stats, counter = [], {}
    for subclass, family, count in rows:
        role = config.role_of(subclass).value
        stats.append(SubclassStat(subclass_id=subclass, family=family,
                                  count=count, role=role))
        counter[role] = counter.get(role, 0) + count
    total = sum(c for _, _, c in rows)
    result = DerivedStatistics(
        total_faults=total,
        detected_count=counter.get(CoverageRole.DT.value, 0),
        subclass_stats=stats,
        role_counts=counter,
        posdet_credit=config.posdet_credit,
        effectiveness_posdet_families=list(config.effectiveness_posdet_families),
        effectiveness_basis=config.effectiveness_basis,
    )
    for stat in stats:
        stat.pct = 100.0 * stat.count / total if total else 0.0
    return result


def _check(population, expected, total=None):
    m = population.metrics(total)
    assert m["total_faults"] == expected["FU"]
    assert round(m["test_coverage"], 2) == expected["tc"]
    assert round(m["fault_coverage"], 2) == expected["fc"]
    assert round(m["atpg_effectiveness"], 2) == expected["eff"]
    return m


# ---------------------------------------------------------------------------
# Acceptance criterion 1: the fixtures reproduce
# ---------------------------------------------------------------------------
def test_fixture_a_pre_disposition_column():
    _check(_stats(FIXTURE_A), EXPECT_A)


def test_fixture_c_post_disposition_total_column():
    _check(_stats(FIXTURE_POST), EXPECT_C)


def test_fixture_b_post_disposition_relevant_column():
    total = _stats(FIXTURE_POST)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    _check(relevant, EXPECT_B, total)


def test_the_relevant_population_differs_by_exactly_the_waiver_block():
    total = _stats(FIXTURE_POST)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    waived = total.get(WAIVER_SUBCLASS).count
    assert total.total_faults - relevant.total_faults == waived
    assert (total.role(CoverageRole.AU)
            - relevant.role(CoverageRole.AU)) == waived
    assert relevant.get(WAIVER_SUBCLASS) is None
    # Nothing but the waived subclass moved.
    for role in (CoverageRole.DT, CoverageRole.PD, CoverageRole.UD,
                 CoverageRole.ND):
        assert total.role(role) == relevant.role(role)


# ---------------------------------------------------------------------------
# Acceptance criterion 2: the census still reconciles, delta 0
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rows", [FIXTURE_A, FIXTURE_POST],
                         ids=["pre", "post"])
def test_every_fixture_census_reconciles_exactly(rows):
    population = _stats(rows)
    assert population.census_balances
    assert population.census_total == population.total_faults
    assert sum(s.count for s in population.subclass_stats) \
        == population.total_faults
    population.validate_census()


def test_the_relevant_population_census_also_reconciles():
    relevant = exclude_subclass(_stats(FIXTURE_POST), WAIVER_SUBCLASS)
    assert relevant.census_balances
    relevant.validate_census()


# ---------------------------------------------------------------------------
# The formula rules themselves
# ---------------------------------------------------------------------------
def test_possibly_detected_faults_get_no_coverage_credit_by_default():
    """The defect: crediting PD at 0.5 overstated both coverage figures."""
    population = _stats(FIXTURE_POST)
    assert population.posdet_credit == 0.0
    assert population.detected_credit == population.role(CoverageRole.DT)
    assert population.role(CoverageRole.PD) > 0  # the credit would have shown


def test_effectiveness_credits_pu_and_not_pt():
    population = _stats(FIXTURE_POST)
    assert population.credited_posdet_families == ["PU"]
    assert population.effectiveness_posdet_count == population.family_count("PU")
    assert population.family_count("PT") > 0
    assert population.effectiveness_posdet_count \
        != population.role(CoverageRole.PD)


def test_effectiveness_is_reported_over_the_total_population():
    """Re-basing it on the relevant population contradicts the tool.

    Re-basing gives 99.8688% -> 99.87%, but the tool prints 99.88% in both
    columns. The waived faults were resolved by ATPG, so removing them from
    the denominator understates how much it settled.
    """
    total = _stats(FIXTURE_POST)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    assert relevant.atpg_effectiveness_on(total) == total.atpg_effectiveness
    # The re-based figure is a different number, and is NOT what we report.
    assert round(relevant.atpg_effectiveness, 2) == 99.87
    assert round(relevant.atpg_effectiveness_on(total), 2) == 99.88


def test_the_effectiveness_basis_is_configurable():
    config = AnalysisConfig.from_dict({"effectiveness_basis": "population"})
    total = _stats(FIXTURE_POST, config)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    assert round(relevant.atpg_effectiveness_on(total), 2) == 99.87


def test_posdet_credit_remains_configurable():
    config = AnalysisConfig.from_dict({"posdet_credit": 0.5})
    population = _stats(FIXTURE_POST, config)
    assert population.posdet_credit == 0.5
    expected = (population.role(CoverageRole.DT)
                + 0.5 * population.role(CoverageRole.PD))
    assert population.detected_credit == expected


def test_the_credited_posdet_families_are_configurable():
    config = AnalysisConfig.from_dict(
        {"effectiveness_posdet_families": ["PT"], "replace_defaults": True})
    population = _stats(FIXTURE_POST, config)
    assert population.credited_posdet_families == ["PT"]
    assert population.effectiveness_posdet_count == population.family_count("PT")


# ---------------------------------------------------------------------------
# Acceptance criterion 3: every metric carries formula AND substitution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["test_coverage", "fault_coverage",
                                 "atpg_effectiveness"])
def test_each_metric_prints_its_formula_and_substitution(key):
    total = _stats(FIXTURE_POST)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    for population in (total, relevant):
        spec = population.metrics(total)["formulas"][key]
        assert spec["formula"]
        assert spec["substitution"]
        # A substitution must contain the arithmetic, not just the answer.
        assert "/" in spec["substitution"]
        assert "=" in spec["substitution"]


def test_the_effectiveness_substitution_names_the_credited_families():
    m = _stats(FIXTURE_POST).metrics()
    spec = m["formulas"]["atpg_effectiveness"]
    assert "PU" in spec["formula"]
    assert "PT" not in spec["formula"]


def test_the_relevant_effectiveness_substitution_says_which_population():
    total = _stats(FIXTURE_POST)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    spec = relevant.metrics(total)["formulas"]["atpg_effectiveness"]
    assert "total population" in spec["substitution"]


def test_metrics_label_the_population_they_describe():
    total = _stats(FIXTURE_POST)
    relevant = exclude_subclass(total, WAIVER_SUBCLASS)
    assert total.metrics()["population"] == "total"
    assert relevant.metrics(total)["population"] == "relevant"
    assert relevant.metrics(total)["excluded_subclass"] == WAIVER_SUBCLASS


# ---------------------------------------------------------------------------
# Degenerate populations still refuse to invent a number
# ---------------------------------------------------------------------------
def test_an_absent_waiver_subclass_leaves_the_population_untouched():
    total = _stats(FIXTURE_A)
    assert exclude_subclass(total, WAIVER_SUBCLASS) is total
    assert exclude_subclass(total, "") is total


def test_an_empty_population_reports_no_coverage():
    empty = _stats([])
    assert empty.test_coverage is None
    assert empty.fault_coverage is None
    assert empty.atpg_effectiveness is None
