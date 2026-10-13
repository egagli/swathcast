"""Kaplan-Meier maths on synthetic data with known answers."""

import math

import numpy as np
import pytest

from swathcast.stats import kaplan_meier, summarize_curve


def test_no_censoring_equals_empirical_cdf():
    d = [1, 2, 3, 4]
    c = kaplan_meier(d, [True] * 4)
    assert np.allclose(c.times, [1, 2, 3, 4])
    assert np.allclose(c.F, [0.25, 0.5, 0.75, 1.0])
    assert c.quantile(0.5) == 2
    assert c.quantile(0.9) == 4


def test_textbook_example_with_censoring():
    # durations 1,2,2+,3,4+,5 (+ = censored)
    # t=1: 6 at risk, 1 event -> S=5/6
    # t=2: 5 at risk (censored-at-2 still at risk), 1 event -> S=5/6*4/5=2/3
    # t=3: 3 at risk, 1 event -> S=2/3*2/3=4/9
    # t=5: 1 at risk, 1 event -> S=0
    c = kaplan_meier([1, 2, 2, 3, 4, 5], [1, 1, 0, 1, 0, 1])
    assert np.allclose(c.times, [1, 2, 3, 5])
    assert np.allclose(1 - c.F, [5 / 6, 2 / 3, 4 / 9, 0.0])
    assert np.allclose(c.at_risk, [6, 5, 3, 1])
    assert c.quantile(0.5) == 3  # F(3) = 5/9 >= .5, F(2) = 1/3


def test_quantile_not_reached_is_none_not_extrapolated():
    c = kaplan_meier([1, 2, 10, 10, 10], [1, 1, 0, 0, 0])
    assert c.quantile(0.9) is None
    assert math.isnan(float(c.cdf(11)))
    assert float(c.cdf(9.5)) == pytest.approx(0.4)
    assert float(c.cdf(0.5)) == 0.0


def test_exponential_with_random_censoring_recovers_median():
    rng = np.random.default_rng(42)
    lam = 1 / 10.0  # true median ln2/lam = 6.93 h
    t = rng.exponential(1 / lam, 20000)
    c_time = rng.uniform(0, 40, 20000)
    obs = t <= c_time
    curve = kaplan_meier(np.minimum(t, c_time), obs)
    assert curve.quantile(0.5) == pytest.approx(math.log(2) / lam, rel=0.03)
    # Dropping censored rows instead is biased low.
    naive = np.median(t[obs])
    assert naive < 0.85 * math.log(2) / lam


def test_never_delivered_fraction():
    # 100 granules, 70 arrive by 5 h, 30 time out at 336 h
    d = [5] * 70 + [336] * 30
    e = [True] * 70 + [False] * 30
    s = summarize_curve(kaplan_meier(d, e), timeout_hours=336)
    assert s["measured"]
    assert s["median_h"] == 5
    assert s["p90_h"] is None
    assert s["not_delivered_frac"] == pytest.approx(0.30)


def test_not_delivered_needs_followup_to_timeout():
    s = summarize_curve(
        kaplan_meier([5] * 30 + [20] * 5, [True] * 30 + [False] * 5), timeout_hours=336
    )
    assert s["not_delivered_frac"] is None


def test_small_sample_is_not_measured():
    s = summarize_curve(kaplan_meier([1, 2, 3], [1, 1, 1]), timeout_hours=10)
    assert not s["measured"]
    assert s["median_h"] is None and s["p90_h"] is None


def test_empty_and_shape_errors():
    c = kaplan_meier([], [])
    assert c.n == 0 and c.quantile(0.5) is None
    with pytest.raises(ValueError):
        kaplan_meier([1, 2], [True])
