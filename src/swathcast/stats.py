"""Kaplan-Meier cumulative availability, quantiles, and the site summary.

Latency is a time-to-event: *hours from sensing start until our poller first
found the granule at a source*. Granules not yet found are right-censored at
their last check; granules not found within the source's timeout are
censored at the timeout. The Kaplan-Meier estimator gives

    F(t) = 1 - S(t) = fraction available by t hours,

from which the median / p90 / p99 are read, and 1 - F(timeout) is the
fraction never delivered within the timeout. Nothing here extrapolates:
a quantile the curve never reaches is reported as ``None``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from swathcast.config import TrackerConfig
from swathcast.timeutil import to_iso

QUANTILES = {"median": 0.5, "p90": 0.9, "p99": 0.99}
#: fewer events than this and a stratum is "not measured"
MIN_EVENTS = 20
#: the log-spaced grid (hours) curves are sampled on for the site
CURVE_GRID = np.unique(np.round(np.geomspace(0.1, 24 * 21, 90), 3))


@dataclass
class KMCurve:
    """Step function: F jumps to ``F[i]`` at ``times[i]``."""

    times: np.ndarray
    F: np.ndarray
    at_risk: np.ndarray
    events: np.ndarray
    n: int
    n_events: int
    max_followup: float

    def cdf(self, t: float | np.ndarray) -> np.ndarray:
        """F(t): fraction available by ``t`` hours (right-continuous)."""
        t = np.asarray(t, dtype=float)
        idx = np.searchsorted(self.times, t, side="right") - 1
        out = np.where(
            idx >= 0, self.F[np.clip(idx, 0, None)] if len(self.F) else 0.0, 0.0
        )
        # Beyond the longest follow-up F is unknown (do not extrapolate),
        # unless every granule has already been seen (F = 1 exactly).
        complete = len(self.F) and self.F[-1] >= 1.0 - 1e-12
        return out if complete else np.where(t > self.max_followup, np.nan, out)

    def quantile(self, q: float) -> float | None:
        """Smallest t with F(t) >= q, or None if the curve never gets there."""
        hits = np.nonzero(self.F >= q - 1e-12)[0]
        return float(self.times[hits[0]]) if len(hits) else None


def kaplan_meier(durations, observed) -> KMCurve:
    """Kaplan-Meier estimate of the cumulative distribution F = 1 - S.

    ``durations``: hours (event or censoring time). ``observed``: True where
    the event (granule seen) happened, False where right-censored. Ties
    follow the usual convention: rows censored at t are still at risk at t.
    """
    d = np.asarray(durations, dtype=float)
    e = np.asarray(observed, dtype=bool)
    if d.shape != e.shape:
        raise ValueError("durations and observed must have the same shape")
    keep = np.isfinite(d)
    d, e = d[keep], e[keep]
    n = len(d)
    if n == 0:
        return KMCurve(
            np.array([]), np.array([]), np.array([]), np.array([]), 0, 0, 0.0
        )
    times, events = np.unique(d[e], return_counts=True)
    events = events.astype(float)
    # at risk at t = rows with duration >= t (censored-at-t included)
    at_risk = (n - np.searchsorted(np.sort(d), times, side="left")).astype(float)
    survival = np.cumprod(1.0 - events / at_risk) if len(times) else np.array([])
    return KMCurve(
        times=times,
        F=1.0 - survival,
        at_risk=at_risk,
        events=events,
        n=n,
        n_events=int(e.sum()),
        max_followup=float(d.max()),
    )


# ---------------------------------------------------------------------------
# From the tracking table to (duration, observed) pairs
# ---------------------------------------------------------------------------


def eligible_cells(df: pd.DataFrame) -> dict[str, set]:
    """Coverage cells in which each source has delivered at least once."""
    seen = df[df["status"] == "seen"]
    return {k: set(g["coverage_cell"].dropna()) for k, g in seen.groupby("source")}


def survival_frame(
    df: pd.DataFrame,
    *,
    learned_sources: set[str],
    config: TrackerConfig | None = None,
    metric: str = "first",
) -> pd.DataFrame:
    """Add ``duration_h``, ``observed`` and ``included``/``excluded_reason``.

    ``metric="first"``: first sighting (for burst products: first burst).
    ``metric="last"``: burst products only: arrival of the last new burst,
    counted as an event only once the row has settled (closed while seen).
    """
    config = config or TrackerConfig()
    out = df.copy()
    sensing = out["sensing_start"]
    hours = lambda col: (out[col] - sensing).dt.total_seconds() / 3600.0  # noqa: E731

    seen = out["status"] == "seen"
    timed_out = out["status"] == "timed_out"
    censor_at = hours("last_checked_utc").fillna(hours("tracking_started_utc"))
    if metric == "first":
        event_at = hours("first_seen_utc")
        observed = seen
    elif metric == "last":
        event_at = hours("last_item_seen_utc").fillna(hours("first_seen_utc"))
        observed = seen & out["closed_utc"].notna()
    else:
        raise ValueError(metric)
    duration = np.where(
        observed, event_at, np.where(timed_out, out["timeout_hours"], censor_at)
    )
    out["duration_h"] = duration.astype(float)
    out["observed"] = observed.astype(bool)

    reason = pd.Series("", index=out.index, dtype=object)
    reason[~out["prompt"].fillna(False).astype(bool)] = (
        "tracking started late (not prompt)"
    )
    gap = (out["first_seen_utc"] - out["last_not_seen_utc"]).dt.total_seconds() / 3600.0
    reason[(reason == "") & seen & (gap > config.max_resolution_hours)] = (
        "sighting after a poller gap"
    )
    cells = eligible_cells(out)
    for key in learned_sources:
        mask = (out["source"] == key) & (reason == "")
        ok = out.loc[mask, "coverage_cell"].isin(cells.get(key, set()))
        reason[ok.index[~ok]] = "outside learned coverage"
    out["excluded_reason"] = reason
    out["included"] = reason == ""
    return out


def summarize_curve(curve: KMCurve, timeout_hours: float | None) -> dict:
    measured = curve.n_events >= MIN_EVENTS
    row = {
        "n": curve.n,
        "n_events": curve.n_events,
        "max_followup_h": round(curve.max_followup, 2),
        "measured": measured,
    }
    for name, q in QUANTILES.items():
        value = curve.quantile(q) if measured else None
        row[f"{name}_h"] = None if value is None else round(value, 2)
    not_delivered = None
    if measured and timeout_hours and curve.max_followup >= timeout_hours:
        not_delivered = round(float(1.0 - curve.cdf(timeout_hours)), 4)
    row["not_delivered_frac"] = not_delivered
    return row


def _curve_points(curve: KMCurve) -> dict:
    values = curve.cdf(CURVE_GRID)
    return {
        "t_h": [float(t) for t in CURVE_GRID],
        "F": [None if math.isnan(v) else round(float(v), 4) for v in values],
    }


def build_summary(
    df: pd.DataFrame,
    sources_meta: dict[str, dict],
    *,
    now: datetime,
    config: TrackerConfig | None = None,
    recent_days: int = 30,
    health: dict | None = None,
    reference: str = "cdse",
) -> dict:
    """Everything the site's latency layer and dashboard need, as one dict."""
    config = config or TrackerConfig()
    learned = {k for k, m in sources_meta.items() if m.get("coverage") == "learned"}
    frames = {"first": survival_frame(df, learned_sources=learned, config=config)}
    burst = {k for k, m in sources_meta.items() if m.get("burst_level")}
    if burst:
        frames["last"] = survival_frame(
            df[df["source"].isin(burst)],
            learned_sources=learned,
            config=config,
            metric="last",
        )

    first = frames["first"]
    first = first.assign(month=first["sensing_start"].dt.strftime("%Y-%m"))
    recent_cut = now - timedelta(days=recent_days)
    recent = first[first["sensing_start"] >= recent_cut]

    overall, curves, expected = {}, [], {}
    for key, meta in sources_meta.items():
        timeout = meta.get("timeout_hours")
        sub = recent[(recent["source"] == key) & recent["included"]]
        curve = kaplan_meier(sub["duration_h"], sub["observed"])
        stats = summarize_curve(curve, timeout)
        excluded = recent[(recent["source"] == key) & ~recent["included"]][
            "excluded_reason"
        ].value_counts()
        stats["excluded"] = {str(k): int(v) for k, v in excluded.items()}
        stats["n_pending"] = int((sub["status"] == "pending").sum())
        stats["n_timed_out"] = int((sub["status"] == "timed_out").sum())
        overall[key] = stats
        if curve.n:
            curves.append(
                {
                    "source": key,
                    "platform": "all",
                    **_curve_points(curve),
                    "n": curve.n,
                    "n_events": curve.n_events,
                }
            )
        expected[key] = {"all": _expected_entry(stats, "all")}
        for platform, grp in sub.groupby("platform"):
            pc = kaplan_meier(grp["duration_h"], grp["observed"])
            ps = summarize_curve(pc, timeout)
            expected[key][str(platform)] = _expected_entry(ps, str(platform))
            if pc.n:
                curves.append(
                    {
                        "source": key,
                        "platform": str(platform),
                        **_curve_points(pc),
                        "n": pc.n,
                        "n_events": pc.n_events,
                    }
                )

    table = []
    included = first[first["included"]]
    for (key, platform, month), grp in included.groupby(
        ["source", "platform", "month"]
    ):
        curve = kaplan_meier(grp["duration_h"], grp["observed"])
        row = summarize_curve(curve, sources_meta.get(key, {}).get("timeout_hours"))
        row.update(
            source=key,
            product=sources_meta.get(key, {}).get("product"),
            platform=platform,
            month=month,
        )
        table.append(row)

    trend = []
    for (key, month), grp in included.groupby(["source", "month"]):
        curve = kaplan_meier(grp["duration_h"], grp["observed"])
        med = curve.quantile(0.5) if curve.n_events >= MIN_EVENTS else None
        trend.append(
            {
                "source": key,
                "month": month,
                "median_h": None if med is None else round(med, 2),
                "n": curve.n,
                "n_events": curve.n_events,
            }
        )

    last_burst = {}
    if "last" in frames:
        lb = frames["last"]
        lb = lb[(lb["sensing_start"] >= recent_cut) & lb["included"]]
        for key, grp in lb.groupby("source"):
            curve = kaplan_meier(grp["duration_h"], grp["observed"])
            last_burst[key] = summarize_curve(
                curve, sources_meta.get(key, {}).get("timeout_hours")
            )

    return {
        "schema_version": 1,
        "generated_at": to_iso(now),
        "data_as_of": _data_as_of(health, reference) or to_iso(now),
        "poll_interval_hours": config.poll_interval_hours,
        "recent_days": recent_days,
        "min_events": MIN_EVENTS,
        "definition": "hours from sensing start to first sighting by this repo's hourly poller",
        "sources": sources_meta,
        "overall": overall,
        "expected": expected,
        "curves": curves,
        "table": table,
        "trend": trend,
        "last_burst": last_burst,
        "provider_vs_first_seen": provider_comparison(first),
        "audit": audit(df, sources_meta),
    }


def audit(df: pd.DataFrame, sources_meta: dict[str, dict]) -> dict:
    """Integrity check: every reference granule has exactly one row per source
    for its product type, and every row is seen, pending (censored) or timed out.

    ``missing_rows`` and ``bad_status`` must be zero: that is what "no granule
    silently missing" means.
    """
    expected = {
        ptype: {
            k for k, m in sources_meta.items() if m.get("reference_product") == ptype
        }
        for ptype in {m.get("reference_product") for m in sources_meta.values()}
    }
    missing = duplicates = 0
    for _, grp in df.groupby("granule_id"):
        want = expected.get(grp["product_type"].iat[0], set())
        have = list(grp["source"])
        missing += len(want - set(have))
        duplicates += len(have) - len(set(have))
    status = df["status"].value_counts().to_dict()
    per_source = {
        key: {str(k): int(v) for k, v in grp["status"].value_counts().items()}
        for key, grp in df.groupby("source")
    }
    return {
        "granules": int(df["granule_id"].nunique()) if len(df) else 0,
        "rows": int(len(df)),
        "missing_rows": int(missing),
        "duplicate_rows": int(duplicates),
        "bad_status": int(
            sum(
                v
                for k, v in status.items()
                if k not in ("seen", "pending", "timed_out")
            )
        ),
        "status_by_source": per_source,
    }


def _expected_entry(stats: dict, stratum: str) -> dict:
    return {
        "stratum": stratum,
        "measured": stats["measured"],
        "median_h": stats["median_h"],
        "p90_h": stats["p90_h"],
        "n_events": stats["n_events"],
    }


def _data_as_of(health: dict | None, reference: str = "cdse") -> str | None:
    if not health:
        return None
    return (health.get("sources", {}).get(reference) or {}).get("last_success_utc")


def provider_comparison(first: pd.DataFrame) -> list[dict]:
    """first_seen minus the provider's own timestamp, per source (hours)."""
    seen = first[
        (first["status"] == "seen")
        & first["included"]
        & first["provider_timestamp_utc"].notna()
    ]
    rows = []
    for key, grp in seen.groupby("source"):
        diff = (
            grp["first_seen_utc"] - grp["provider_timestamp_utc"]
        ).dt.total_seconds() / 3600.0
        drift = (
            grp["provider_timestamp_latest_utc"] - grp["provider_timestamp_utc"]
        ).dt.total_seconds() / 3600.0
        rows.append(
            {
                "source": key,
                "field": grp["provider_timestamp_field"].dropna().mode().iat[0]
                if grp["provider_timestamp_field"].notna().any()
                else None,
                "n": int(len(grp)),
                "median_h": round(float(diff.median()), 3),
                "p10_h": round(float(diff.quantile(0.1)), 3),
                "p90_h": round(float(diff.quantile(0.9)), 3),
                "frac_provider_after_first_seen": round(float((diff < 0).mean()), 4),
                "frac_provider_timestamp_changed": round(
                    float((drift.abs() > 1 / 60).mean()), 4
                ),
            }
        )
    return rows


def expected_availability(
    pass_begin: datetime, platform: str, source_expected: dict
) -> dict | None:
    """``pass.begin_time + latency quantile`` for one source.

    Prefers the platform-specific stratum; falls back to all platforms;
    returns ``None`` (render as "not measured") if neither is measured.
    """
    entry = source_expected.get(platform)
    if not entry or not entry.get("measured"):
        entry = source_expected.get("all")
    if not entry or not entry.get("measured") or entry.get("median_h") is None:
        return None
    result = {"stratum": entry["stratum"], "n_events": entry["n_events"]}
    for name in ("median_h", "p90_h"):
        hours = entry.get(name)
        result[name.replace("_h", "_at")] = (
            None if hours is None else pass_begin + timedelta(hours=hours)
        )
    return result
