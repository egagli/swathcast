"""survival_frame exclusions and the latency.json contract, on a synthetic table."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from swathcast.config import TrackerConfig
from swathcast.publish import latest_granules, sources_meta
from swathcast.stats import build_summary, survival_frame
from swathcast.storage import normalize

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def synthetic(n=200, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        sensed = NOW - timedelta(hours=float(rng.uniform(30, 400)))
        cell = f"137-{i % 10:03d}"
        base = dict(
            granule_id=f"G{i}",
            product_type="GRD",
            platform="S1C" if i % 2 else "S1D",
            mode="IW",
            polarization="DV",
            relative_orbit=137,
            coverage_cell=cell,
            sensing_start=sensed,
            sensing_stop=sensed + timedelta(seconds=25),
            tracking_started_utc=sensed + timedelta(hours=1),
            prompt=i % 50 != 0,
            timeout_hours=336.0,
            n_checks=1,
        )
        rows.append(
            dict(
                base,
                source="cdse_grd",
                status="seen",
                first_seen_utc=sensed + timedelta(hours=1),
                provider_timestamp_utc=sensed + timedelta(hours=0.8),
                closed_utc=sensed + timedelta(hours=1),
            )
        )
        lat = float(rng.exponential(20))
        seen_at = sensed + timedelta(hours=lat)
        if seen_at <= NOW and lat < 336:
            rows.append(
                dict(
                    base,
                    source="asf_grd",
                    status="seen",
                    first_seen_utc=seen_at,
                    last_not_seen_utc=seen_at - timedelta(hours=1),
                    last_checked_utc=seen_at,
                    provider_timestamp_utc=seen_at - timedelta(minutes=10),
                    provider_timestamp_latest_utc=seen_at,
                    provider_timestamp_field="ProviderDates[Insert]",
                    closed_utc=seen_at,
                )
            )
        else:
            rows.append(
                dict(
                    base,
                    source="asf_grd",
                    status="pending",
                    last_checked_utc=NOW,
                    last_not_seen_utc=NOW,
                )
            )
        # a partial-coverage source that only ever delivers in cells 000-004
        if int(cell[-3:]) < 5:
            rows.append(
                dict(
                    base,
                    source="pc_rtc",
                    status="seen",
                    first_seen_utc=sensed + timedelta(hours=5),
                    last_checked_utc=sensed + timedelta(hours=5),
                    closed_utc=sensed + timedelta(hours=5),
                )
            )
        else:
            rows.append(
                dict(base, source="pc_rtc", status="pending", last_checked_utc=NOW)
            )
    return normalize(pd.DataFrame(rows))


def test_survival_frame_exclusions():
    df = synthetic()
    out = survival_frame(df, learned_sources={"pc_rtc"})
    assert (
        out.loc[~out["prompt"], "excluded_reason"]
        == "tracking started late (not prompt)"
    ).all()
    pc = out[(out["source"] == "pc_rtc") & out["prompt"]]
    outside = pc["coverage_cell"].str[-3:].astype(int) >= 5
    assert (pc.loc[outside, "excluded_reason"] == "outside learned coverage").all()
    assert pc.loc[~outside, "included"].all()
    asf = out[(out["source"] == "asf_grd") & out["included"]]
    pending = asf["status"] == "pending"
    assert not asf.loc[pending, "observed"].any()
    assert (asf.loc[pending, "duration_h"] > 0).all()


def test_gap_sightings_are_excluded():
    df = synthetic()
    i = df.index[(df["source"] == "asf_grd") & (df["status"] == "seen") & df["prompt"]][
        0
    ]
    df.loc[i, "last_not_seen_utc"] = df.loc[i, "first_seen_utc"] - timedelta(hours=10)
    out = survival_frame(df, learned_sources=set(), config=TrackerConfig())
    assert out.loc[i, "excluded_reason"] == "sighting after a poller gap"


def test_build_summary_contract():
    df = synthetic(400)
    config = TrackerConfig()
    meta = sources_meta(config)
    summary = build_summary(df, meta, now=NOW, config=config, recent_days=30)
    assert summary["overall"]["asf_grd"]["measured"]
    med = summary["overall"]["asf_grd"]["median_h"]
    assert 20 * np.log(2) * 0.7 < med < 20 * np.log(2) * 1.3
    assert (
        summary["overall"]["pc_rtc"]["median_h"] == 4.0
        or summary["overall"]["pc_rtc"]["median_h"] == 5.0
    )
    # sources with no rows are "not measured", never estimated
    assert summary["overall"]["opera_cslc"]["measured"] is False
    assert summary["overall"]["opera_cslc"]["median_h"] is None
    assert summary["expected"]["asf_grd"]["all"]["measured"]
    assert {"S1C", "S1D"} <= set(summary["expected"]["asf_grd"])
    comp = {r["source"]: r for r in summary["provider_vs_first_seen"]}
    assert (
        comp["asf_grd"]["median_h"] == 10 / 60
        or abs(comp["asf_grd"]["median_h"] - 1 / 6) < 1e-3
    )
    assert comp["asf_grd"]["frac_provider_timestamp_changed"] > 0.9
    assert all(c["F"][0] is not None for c in summary["curves"])


def test_latest_granules():
    df = synthetic(100)
    out = latest_granules(df, NOW, hours=400)
    assert out["granules"]
    g = out["granules"][0]
    assert set(g) >= {"id", "platform", "track", "sensing", "h"}
