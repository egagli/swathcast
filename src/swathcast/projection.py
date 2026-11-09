"""Projected acquisitions: a plan carried forward past its end by its repeat cycle.

An agency's plan says what *will* be acquired, as far as it is published
(:mod:`swathcast.plans`). Past its end nothing is known, but each mission flies
a repeat-ground-track orbit and images on a cycle: Landsat 8 and 9 each repeat
every 16 days, Sentinel-1 every 12, Sentinel-2 every 10, NISAR every 12. A
*projected* acquisition is a planned one shifted forward by whole repeat
cycles: the last cycle of the plan, repeated.

Projected rows are never mixed with planned ones without a label: each carries
``basis = "projected"`` and ``cycles`` (how many repeats it was shifted). How
far a projection can be trusted is measured per mission by
:mod:`swathcast.hindcast` (an old plan projected forward against what was
later planned or acquired), and the horizon in :data:`PROJECTION_DAYS` comes
from that measurement (docs/methodology.md, "Projection").

Time: shifting by whole repeat periods keeps the time of day, because each
orbit is held on its reference ground track. This is used instead of
propagating orbital elements months ahead (SGP4 drifts and does not know
about manoeuvres); the hindcast compares the two.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Nominal repeat cycle of each platform's ground track (one satellite).
REPEAT_DAYS = {
    "landsat": 16,  # 233 orbits
    "sentinel-1": 12,  # 175 orbits
    "sentinel-2": 10,  # 143 orbits
    "nisar": 12,  # 173 orbits
}

#: Platforms whose *acquisition plan* repeats on a longer period than their
#: orbit: since S2C took over from S2A (2025), S2A images on a 20-day pattern
#: (hindcast 2026-11-09: one 10-day cycle ahead, 46 % of S2A's projected time
#: was later planned; on a 20-day repeat, 90 %).
PLATFORM_REPEAT_DAYS = {"S2A": 20}


def repeat_of(sensor: str, platform: str) -> pd.Timedelta:
    """The period a platform's plan is carried forward by."""
    return pd.Timedelta(days=PLATFORM_REPEAT_DAYS.get(platform, REPEAT_DAYS[sensor]))


#: How far ahead each mission is projected (days after the start of the
#: schedule's window, like ``schedule --days``). Only missions listed here are
#: projected; set from the hindcast (docs/methodology.md, "Projection").
PROJECTION_DAYS = {
    "landsat": 365,
}


def base_cycle(df: pd.DataFrame, plan_end, repeat: pd.Timedelta, time_col="begin"):
    """The rows of the plan's last full cycle: ``[plan_end - repeat, plan_end)``."""
    t = df[time_col]
    return df[(t >= plan_end - repeat) & (t < plan_end)]


def project_repeat(
    df: pd.DataFrame,
    plan_end: dict,
    repeat: pd.Timedelta,
    until,
    *,
    platform_col: str = "platform",
    time_cols: tuple[str, ...] = ("begin",),
) -> pd.DataFrame:
    """Each platform's last cycle of ``df``, repeated past ``plan_end[platform]``
    up to (not including) ``until``.

    Returns the projected rows with every column of ``df``, the time columns
    shifted, ``cycles`` (number of repeats, >= 1) and ``basis`` ("projected").
    The first time column decides the base cycle and the cut-off. A platform
    missing from ``plan_end`` is not projected."""
    until = pd.Timestamp(until)
    first = time_cols[0]
    out = []
    for platform, end in plan_end.items():
        end = pd.Timestamp(end)
        base = base_cycle(df[df[platform_col] == platform], end, repeat, first)
        if base.empty or end >= until:
            continue
        n = int(np.ceil((until - (end - repeat)) / repeat))
        for k in range(1, n + 1):
            shifted = base.copy()
            for c in time_cols:
                shifted[c] = shifted[c] + k * repeat
            shifted = shifted[(shifted[first] >= end) & (shifted[first] < until)]
            if shifted.empty:
                continue
            shifted["cycles"] = k
            out.append(shifted)
    if not out:
        empty = df.iloc[0:0].copy()
        empty["cycles"] = pd.Series(dtype="int16")
        empty["basis"] = pd.Series(dtype="object")
        return empty
    res = pd.concat(out, ignore_index=True)
    res["cycles"] = res["cycles"].astype("int16")
    res["basis"] = "projected"
    return res


# ------------------------------------------------------------- interval maths
def merge_intervals(begin, end) -> tuple[np.ndarray, np.ndarray]:
    """Union of [begin, end) intervals as sorted, disjoint arrays (int ns)."""
    b = np.asarray(begin, dtype="datetime64[ns]").astype("int64")
    e = np.asarray(end, dtype="datetime64[ns]").astype("int64")
    if not len(b):
        return b, e
    order = np.argsort(b)
    b, e = b[order], e[order]
    mb, me = [b[0]], [e[0]]
    for x, y in zip(b[1:], e[1:], strict=True):
        if x <= me[-1]:
            me[-1] = max(me[-1], y)
        else:
            mb.append(x)
            me.append(y)
    return np.array(mb), np.array(me)


def overlap_ns(begin, end, union: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """For each [begin, end), how much of it the union of intervals covers (ns)."""
    ub, ue = union
    b = np.asarray(begin, dtype="datetime64[ns]").astype("int64")
    e = np.asarray(end, dtype="datetime64[ns]").astype("int64")
    out = np.zeros(len(b), dtype="int64")
    if not len(ub):
        return out
    # cumulative covered length up to each union boundary
    for i, (x, y) in enumerate(zip(b, e, strict=True)):
        j0 = max(np.searchsorted(ue, x, side="right"), 0)
        j1 = np.searchsorted(ub, y, side="left")
        if j1 <= j0:
            continue
        lo = np.maximum(ub[j0:j1], x)
        hi = np.minimum(ue[j0:j1], y)
        out[i] = int(np.clip(hi - lo, 0, None).sum())
    return out
