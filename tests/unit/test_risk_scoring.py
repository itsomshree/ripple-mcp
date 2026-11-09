import pytest

from ripple.analysis.risk import (
    RiskConfig,
    RiskLevel,
    assemble,
    build_factors,
    coverage_gap,
    level_for,
    saturate,
)


def factors(**kw):
    base = {"endpoints": 0, "tables": 0, "covered": 0, "considered": 0, "fan_in": 0}
    return build_factors(**{**base, **kw})


# saturate
def test_saturate_endpoints():
    assert saturate(0, 8) == 0.0
    assert saturate(-3, 8) == 0.0
    assert saturate(8, 8) == pytest.approx(1.0)
    assert saturate(50, 8) == 1.0


def test_saturate_has_diminishing_returns():
    steps = [saturate(n, 10) for n in range(11)]
    assert steps == sorted(steps)
    assert steps[1] - steps[0] > steps[10] - steps[9]
    assert saturate(1, 8) == pytest.approx(0.3155, abs=1e-3)


# coverage gap
@pytest.mark.parametrize(
    "covered, considered, expected",
    [(0, 0, None), (3, 3, 0.0), (0, 3, 1.0), (1, 4, 0.75)],
)
def test_coverage_gap(covered, considered, expected):
    assert coverage_gap(covered, considered) == expected


# levels
@pytest.mark.parametrize(
    "score, level",
    [
        (0, RiskLevel.LOW),
        (24.9, RiskLevel.LOW),
        (25, RiskLevel.MEDIUM),
        (49.9, RiskLevel.MEDIUM),
        (50, RiskLevel.HIGH),
        (74.9, RiskLevel.HIGH),
        (75, RiskLevel.CRITICAL),
        (100, RiskLevel.CRITICAL),
    ],
)
def test_level_boundaries(score, level):
    assert level_for(score) is level


# config validation
def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        RiskConfig(w_endpoints=0.5)


def test_negative_weight_is_rejected():
    with pytest.raises(ValueError):
        RiskConfig(w_endpoints=-0.1, w_tables=0.7)


def test_caps_must_be_positive():
    with pytest.raises(ValueError):
        RiskConfig(cap_tables=0)


def test_thresholds_must_ascend():
    with pytest.raises(ValueError):
        RiskConfig(medium_at=60, high_at=50)


# factors
def test_factor_names_and_order_are_stable():
    assert [f.name for f in factors()] == [
        "endpoints",
        "tables",
        "coverage_gap",
        "fan_in",
    ]


def test_factor_at_cap_contributes_its_full_weight():
    by = {f.name: f for f in factors(endpoints=8, tables=4, fan_in=10)}
    assert by["endpoints"].contribution == pytest.approx(35.0)
    assert by["tables"].contribution == pytest.approx(25.0)
    assert by["fan_in"].contribution == pytest.approx(15.0)


def test_nothing_affected_contributes_nothing():
    assert all(f.contribution == 0 for f in factors())


def test_coverage_not_applicable_is_marked_and_neutral():
    cov = next(f for f in factors() if f.name == "coverage_gap")
    assert cov.raw is None and cov.contribution == 0
    assert "no endpoints" in cov.explanation


def test_uncovered_surface_scores_the_full_coverage_weight():
    cov = next(f for f in factors(covered=0, considered=4) if f.name == "coverage_gap")
    assert cov.raw == 1.0
    assert cov.contribution == pytest.approx(25.0)
    assert cov.explanation == "0 of 4 covered by tests"


def test_explanations_pluralize():
    by = {f.name: f for f in factors(endpoints=1, tables=2, fan_in=1)}
    assert by["endpoints"].explanation == "1 endpoint affected"
    assert by["tables"].explanation.startswith("2 tables")
    assert by["fan_in"].explanation == "1 direct dependent"


# assemble
def test_everything_maxed_is_critical_100():
    a = assemble(factors(endpoints=8, tables=4, covered=0, considered=2, fan_in=10))
    assert a.score == 100.0 and a.level is RiskLevel.CRITICAL


def test_nothing_is_low_0():
    a = assemble(factors())
    assert a.score == 0.0 and a.level is RiskLevel.LOW


def test_score_is_the_sum_of_contributions_rounded():
    fs = factors(endpoints=2, tables=1, covered=1, considered=2, fan_in=3)
    a = assemble(fs)
    assert a.score == round(sum(f.contribution for f in fs), 1)
    assert a.factors == fs


def test_caveats_are_carried_through_as_a_copy():
    caveats = ["truncated"]
    a = assemble(factors(), caveats)
    caveats.append("later")
    assert a.caveats == ["truncated"]


def test_custom_config_changes_level_cutoffs():
    cfg = RiskConfig(medium_at=5, high_at=10, critical_at=15)
    a = assemble(factors(endpoints=1), config=cfg)
    assert a.level is RiskLevel.HIGH
