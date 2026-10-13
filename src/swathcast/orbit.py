"""Sentinel-1 orbit timing: when is a given burst imaged, and is it in the plan?

Each Sentinel-1 unit flies a 12-day, 175-orbit exact repeat, so its
ascending-node (ANX) crossings form a lattice

    ANX_k = epoch + k * period,        k = ..., -1, 0, 1, ...

and orbit k has relative orbit (track) ``((k + offset) mod 175) + 1``. Both
``epoch``/``period`` and ``offset`` are fitted per platform from CDSE products
the poller has already seen (sensing start minus ``startTimeFromAscendingNode``).
Fitted on 2026-09-16..29: residuals <= 0.3 s for S1C and S1D.

A burst of the ESA burst grid has a fixed track and ``TIME_FROM_ANX_SEC``
(ASF's ``Sentinel-1_Burst_Map`` CMR collection), so it is imaged at
``ANX_k + time_from_anx`` for each orbit k on its track, and it is *acquired*
if that instant falls inside a planned IW datatake of the same platform.
``site/common.js`` mirrors :func:`burst_times` and :func:`acquisitions`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from swathcast.timeutil import parse_utc, to_iso

N_TRACKS = 175
REPEAT_DAYS = 12
NOMINAL_PERIOD_S = REPEAT_DAYS * 86400 / N_TRACKS
#: slack when testing whether a burst instant is inside a datatake window
WINDOW_SLACK_S = 3.0

# ESA burst ID definition (Sentinel-1 burst ID map): the relative burst ID
# counts burst cycles from the ANX of track 1, so the burst's time after its
# own track's ANX follows from the ID. Checked against ASF's
# TIME_FROM_ANX_SEC for 2,500 bursts on 2026-09-29: exact to 0.1 ms once a
# constant per-subswath offset is added.
T_BEAM_S = 2.758273
T_PRE_S = 2.299849
SUBSWATH_OFFSET_S = {"IW1": 0.0008, "IW2": 0.8333, "IW3": 1.9113}


def burst_time_from_anx(track: int, burst_id: int, subswath: str) -> float:
    """Seconds after the track's ANX at which burst ``T{track}-{burst_id}-{subswath}`` starts."""
    return (
        (int(burst_id) - 1) * T_BEAM_S
        + T_PRE_S
        - NOMINAL_PERIOD_S * (int(track) - 1)
        + SUBSWATH_OFFSET_S[subswath]
    )


@dataclass(frozen=True)
class OrbitModel:
    platform: str
    epoch: datetime  # an ANX crossing (k = 0)
    period_s: float
    offset: int  # track of orbit k is ((k + offset) % 175) + 1
    n: int
    resid_std_s: float
    resid_max_s: float

    def track(self, k: int | np.ndarray):
        return (np.asarray(k) + self.offset) % N_TRACKS + 1

    def anx(self, k: int) -> datetime:
        return self.epoch + timedelta(seconds=float(k) * self.period_s)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "epoch": self.epoch.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "period_s": round(self.period_s, 6),
            "offset": int(self.offset),
            "n": int(self.n),
            "resid_std_s": round(self.resid_std_s, 3),
            "resid_max_s": round(self.resid_max_s, 3),
        }

    @classmethod
    def from_dict(cls, d: dict) -> OrbitModel:
        return cls(
            platform=d["platform"],
            epoch=parse_utc(d["epoch"]),
            period_s=float(d["period_s"]),
            offset=int(d["offset"]),
            n=int(d.get("n", 0)),
            resid_std_s=float(d.get("resid_std_s", 0)),
            resid_max_s=float(d.get("resid_max_s", 0)),
        )


def fit(
    sensing_start: pd.Series,
    anx_start_s: pd.Series,
    relative_orbit: pd.Series,
    platform: str,
) -> OrbitModel:
    """Fit the ANX lattice and track offset for one platform."""
    ok = sensing_start.notna() & anx_start_s.notna() & relative_orbit.notna()
    if ok.sum() < 10:
        raise ValueError(
            f"{platform}: need at least 10 products to fit, have {ok.sum()}"
        )
    anx = anx_start_s[ok].astype(float).to_numpy()
    epoch0 = pd.Timestamp("2000-01-01", tz="UTC")
    seconds = (pd.to_datetime(sensing_start[ok], utc=True) - epoch0).dt.total_seconds()
    t = seconds.to_numpy() - anx
    rel = relative_orbit[ok].astype(int).to_numpy()
    # Anchor on an actual product's ANX time (the one nearest the median):
    # the median itself can fall half-way between lattice points, making the
    # orbit index round inconsistently (seen for S1D on 2026-09-29).
    t0 = float(t[np.argmin(np.abs(t - np.median(t)))])
    k = np.round((t - t0) / NOMINAL_PERIOD_S)
    design = np.vstack([np.ones_like(k), k]).T
    (intercept, period), *_ = np.linalg.lstsq(design, t - t0, rcond=None)
    resid = (t - t0) - design @ np.array([intercept, period])
    # startTimeFromAscendingNode is measured from the ANX of the orbit in which
    # the *datatake* began, so it can exceed one period for slices after the
    # next ANX; the relative orbit refers to the slice's own orbit.
    k_slice = k.astype(int) + np.floor(anx / NOMINAL_PERIOD_S).astype(int)
    offsets = (rel - 1 - k_slice) % N_TRACKS
    values, counts = np.unique(offsets, return_counts=True)
    offset = int(values[np.argmax(counts)])
    agree = counts.max() / counts.sum()
    if agree < 0.99:
        raise ValueError(f"{platform}: track offset inconsistent ({agree:.1%} agree)")
    epoch = (
        epoch0 + pd.Timedelta(microseconds=round((t0 + intercept) * 1e6))
    ).to_pydatetime()
    return OrbitModel(
        platform=platform,
        epoch=epoch,
        period_s=float(period),
        offset=offset,
        n=int(ok.sum()),
        resid_std_s=float(resid.std()),
        resid_max_s=float(np.abs(resid).max()),
    )


def fit_from_table(df: pd.DataFrame) -> dict[str, OrbitModel]:
    """Fit every platform from the tracking table's CDSE GRD rows."""
    ref = df[(df["source"] == "cdse_grd") & df["anx_start_s"].notna()]
    models = {}
    for platform, grp in ref.groupby("platform"):
        try:
            models[str(platform)] = fit(
                grp["sensing_start"],
                grp["anx_start_s"],
                grp["relative_orbit"],
                str(platform),
            )
        except ValueError:
            continue
    return models


def burst_times(
    model: OrbitModel,
    track: int,
    time_from_anx_s: float,
    start: datetime,
    end: datetime,
) -> list[datetime]:
    """Every instant in [start, end] at which ``model``'s platform images the burst."""
    first_k = int(np.floor((start - model.epoch).total_seconds() / model.period_s)) - 1
    last_k = int(np.ceil((end - model.epoch).total_seconds() / model.period_s)) + 1
    # smallest k >= first_k on the right track
    k = first_k + ((track - 1 - model.offset - first_k) % N_TRACKS)
    out = []
    while k <= last_k:
        when = model.anx(k) + timedelta(seconds=time_from_anx_s)
        if start <= when <= end:
            out.append(when)
        k += N_TRACKS
    return out


def acquisitions(
    models: dict[str, OrbitModel],
    datatakes: pd.DataFrame,
    track: int,
    time_from_anx_s: float,
    start: datetime,
    end: datetime,
    slack_s: float = WINDOW_SLACK_S,
) -> list[dict]:
    """Planned acquisitions of one burst: instants inside an IW datatake window.

    ``datatakes`` has columns platform, mode, begin_time, end_time, track.
    """
    found = []
    iw = datatakes[datatakes["mode"] == "IW"]
    slack = pd.Timedelta(seconds=slack_s)
    for platform, model in models.items():
        plan = iw[iw["platform"] == platform]
        if plan.empty:
            continue
        for when in burst_times(model, track, time_from_anx_s, start, end):
            ts = pd.Timestamp(when)
            hit = plan[
                (plan["begin_time"] - slack <= ts) & (plan["end_time"] + slack >= ts)
            ]
            if len(hit):
                found.append(
                    {
                        "platform": platform,
                        "time": when,
                        "datatake_track": int(hit["track"].iat[0]),
                    }
                )
    return sorted(found, key=lambda a: a["time"])


def models_to_json(models: dict[str, OrbitModel], now: datetime) -> dict:
    return {
        "generated_at": to_iso(now),
        "n_tracks": N_TRACKS,
        "platforms": {k: m.to_dict() for k, m in sorted(models.items())},
    }
