"""Tracker state machine: universe, sightings, censoring, failures, idempotency."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from swathcast.config import TrackerConfig
from swathcast.sources.base import Acquisition, Found, ReferenceCatalog, Source
from swathcast.storage import Store
from swathcast.tracker import Tracker

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def acq(
    i: int, sensed: datetime, published: datetime, kind: str = "GRD"
) -> Acquisition:
    stamp = sensed.strftime("%Y%m%dT%H%M%S")
    t = "GRDH" if kind == "GRD" else "SLC_"
    return Acquisition(
        granule_id=f"S1D_IW_{t}_1SDV_{stamp}_{stamp}_00{i:04d}_00ABCD_{i:04X}",
        product_type=kind,
        platform="S1D",
        mode="IW",
        polarization="DV",
        absolute_orbit=1000 + i,
        relative_orbit=137,
        orbit_direction="descending",
        datatake_id="00ABCD",
        anx_start_s=100.0 * i,
        sensing_start=sensed,
        sensing_stop=sensed + timedelta(seconds=25),
        centroid_lon=-123.5,
        centroid_lat=47.8,
        timeliness="NRT-3h",
        provider_timestamp=published,
        provider_timestamp_field="PublicationDate",
    )


class FakeCatalog(ReferenceCatalog):
    key = "cdse"
    label = distributor = product = "fake"

    def __init__(self, batches):
        super().__init__(session=object())
        self.batches = batches  # {now: [Acquisition]}
        self.windows = []

    def list_new(self, window):
        self.windows.append(window)
        return list(self.batches.get(window[1], []))

    def lookup(self, ids):
        return {}


class RefGRD(Source):
    key = "cdse_grd"
    label = distributor = product = "ref"
    is_reference = True

    def lookup(self, ids):
        return {}


class FakeSource(Source):
    key = "fake_grd"
    label = distributor = product = "fake"
    timeout_hours = 48.0

    def __init__(self, available=None, fail_at=()):
        super().__init__(session=object())
        self.available = available or {}  # id -> time it becomes available
        self.fail_at = set(fail_at)
        self.now = None
        self.asked = []

    def lookup(self, ids):
        self.asked.append(list(ids))
        if self.now in self.fail_at:
            raise ConnectionError("provider down")
        return {
            i: Found(
                provider_timestamp=self.available[i] - timedelta(minutes=5),
                provider_timestamp_field="ts",
            )
            for i in ids
            if i in self.available and self.available[i] <= self.now
        }


def run(tmp_path, now, catalog, fake, config=None):
    fake.now = now
    tracker = Tracker(
        Store(tmp_path),
        config or TrackerConfig(source_options={"cdse_grd": {}, "fake_grd": {}}),
        now=now,
        catalog=catalog,
        sources={"cdse_grd": RefGRD(session=object()), "fake_grd": fake},
    )
    return tracker.run()


def table(tmp_path):
    return Store(tmp_path).read_all().set_index(["granule_id", "source"])


def test_lifecycle_seen_censored_timed_out(tmp_path):
    a = acq(1, T0 - timedelta(hours=2), T0 - timedelta(minutes=30))
    b = acq(2, T0 - timedelta(hours=2), T0 - timedelta(minutes=20))
    catalog = FakeCatalog({T0: [a, b]})
    fake = FakeSource(available={a.granule_id: T0 + timedelta(hours=3)})

    r = run(tmp_path, T0, catalog, fake)
    assert (r.new_acquisitions, r.new_rows, r.exit_code) == (2, 4, 0)
    df = table(tmp_path)
    ref = df.loc[(a.granule_id, "cdse_grd")]
    assert (
        ref["status"] == "seen" and ref["first_seen_utc"] == T0 and bool(ref["prompt"])
    )
    assert ref["provider_timestamp_utc"] == a.provider_timestamp
    assert df.loc[(a.granule_id, "fake_grd"), "status"] == "pending"
    assert df.loc[(a.granule_id, "fake_grd"), "last_not_seen_utc"] == T0

    for h in (1, 2, 3):
        run(tmp_path, T0 + timedelta(hours=h), catalog, fake)
    df = table(tmp_path)
    row = df.loc[(a.granule_id, "fake_grd")]
    assert row["status"] == "seen"
    assert row["first_seen_utc"] == T0 + timedelta(hours=3)
    assert row["last_not_seen_utc"] == T0 + timedelta(hours=2)
    assert row["n_checks"] == 4
    assert pd.notna(row["closed_utc"])
    # b is still open: right-censored, not dropped
    assert df.loc[(b.granule_id, "fake_grd"), "status"] == "pending"
    # closed rows are not looked up again
    run(tmp_path, T0 + timedelta(hours=4), catalog, fake)
    assert fake.asked[-1] == [b.granule_id]

    # timeout = sensing + 48 h
    r = run(tmp_path, T0 + timedelta(hours=46), catalog, fake)
    assert r.timed_out == {"fake_grd": 1}
    assert table(tmp_path).loc[(b.granule_id, "fake_grd"), "status"] == "timed_out"


def test_rerun_is_idempotent_and_cursor_overlaps(tmp_path):
    a = acq(1, T0 - timedelta(hours=2), T0 - timedelta(minutes=30))
    catalog = FakeCatalog({T0: [a], T0 + timedelta(hours=1): [a]})  # overlap re-lists a
    fake = FakeSource()
    run(tmp_path, T0, catalog, fake)
    r = run(tmp_path, T0 + timedelta(hours=1), catalog, fake)
    assert r.new_acquisitions == 0
    assert len(Store(tmp_path).read_all()) == 2
    start, end = catalog.windows[-1]
    assert end - start == timedelta(hours=4)  # 1 h since cursor + 3 h overlap
    run(tmp_path, T0 + timedelta(hours=1), catalog, fake)
    assert len(Store(tmp_path).read_all()) == 2


def test_bootstrap_rows_are_not_prompt(tmp_path):
    old = acq(1, T0 - timedelta(hours=20), T0 - timedelta(hours=18))
    catalog = FakeCatalog({T0: [old]})
    run(tmp_path, T0, catalog, FakeSource())
    assert not table(tmp_path).loc[(old.granule_id, "fake_grd"), "prompt"]
    start, end = catalog.windows[0]
    assert end - start == timedelta(hours=24)


def test_products_older_than_max_age_are_ignored(tmp_path):
    ancient = acq(1, T0 - timedelta(days=20), T0 - timedelta(minutes=10))
    r = run(tmp_path, T0, FakeCatalog({T0: [ancient]}), FakeSource())
    assert r.new_acquisitions == 0


def test_failing_source_is_isolated_and_rows_stay_open(tmp_path):
    a = acq(1, T0 - timedelta(hours=2), T0 - timedelta(minutes=30))
    catalog = FakeCatalog({T0: [a]})
    fake = FakeSource(
        available={a.granule_id: T0 + timedelta(hours=1)},
        fail_at={T0 + timedelta(hours=1)},
    )
    run(tmp_path, T0, catalog, fake)
    r = run(tmp_path, T0 + timedelta(hours=1), catalog, fake)
    assert r.exit_code == 0  # CDSE listing succeeded, so not "all failed"
    health = Store(tmp_path).read_json("health.json")
    entry = health["sources"]["fake_grd"]
    assert entry["last_error"].startswith("ConnectionError")
    assert entry["failing_since_utc"] == "2026-10-01T13:00:00Z"
    row = table(tmp_path).loc[(a.granule_id, "fake_grd")]
    assert (
        row["status"] == "pending" and row["n_checks"] == 1
    )  # failed check not counted
    run(tmp_path, T0 + timedelta(hours=2), catalog, fake)
    health = Store(tmp_path).read_json("health.json")
    assert health["sources"]["fake_grd"]["failing_since_utc"] is None
    assert table(tmp_path).loc[
        (a.granule_id, "fake_grd"), "first_seen_utc"
    ] == T0 + timedelta(hours=2)


def test_exit_code_when_everything_fails(tmp_path):
    class Broken(FakeCatalog):
        def list_new(self, window):
            raise ConnectionError("CDSE down")

    fake = FakeSource(fail_at={T0})
    # give the fake something to poll so it is attempted
    run(
        tmp_path,
        T0 - timedelta(hours=1),
        FakeCatalog(
            {
                T0 - timedelta(hours=1): [
                    acq(1, T0 - timedelta(hours=3), T0 - timedelta(hours=1, minutes=10))
                ]
            }
        ),
        FakeSource(),
    )
    r = run(tmp_path, T0, Broken({}), fake)
    assert r.exit_code == 2


def test_exit_code_when_a_source_fails_for_over_24h(tmp_path):
    a = acq(1, T0 - timedelta(hours=2), T0 - timedelta(minutes=30))
    hours = [T0 + timedelta(hours=h) for h in range(0, 27)]
    fake = FakeSource(fail_at=set(hours[1:]))
    catalog = FakeCatalog({T0: [a]})
    codes = [run(tmp_path, t, catalog, fake).exit_code for t in hours]
    assert codes[:25] == [0] * 25
    assert codes[-1] == 3


@pytest.mark.parametrize("month_edge", [True])
def test_partitions_by_sensing_month(tmp_path, month_edge):
    now = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
    a = acq(1, datetime(2026, 10, 31, 23, 50, tzinfo=UTC), now - timedelta(minutes=20))
    b = acq(2, datetime(2026, 11, 1, 0, 10, tzinfo=UTC), now - timedelta(minutes=10))
    run(tmp_path, now, FakeCatalog({now: [a, b]}), FakeSource())
    assert Store(tmp_path).partitions() == [(2026, 10), (2026, 11)]


def test_audit_every_reference_granule_is_tracked_everywhere(tmp_path):
    from swathcast.stats import audit

    acqs = [
        acq(i, T0 - timedelta(hours=2), T0 - timedelta(minutes=30)) for i in range(1, 6)
    ]
    fake = FakeSource(available={acqs[0].granule_id: T0 + timedelta(hours=1)})
    catalog = FakeCatalog({T0: acqs})
    for h in range(3):
        run(tmp_path, T0 + timedelta(hours=h), catalog, fake)
    meta = {
        "cdse_grd": {"reference_product": "GRD"},
        "fake_grd": {"reference_product": "GRD"},
    }
    df = Store(tmp_path).read_all()
    report = audit(df, meta)
    assert (
        report["missing_rows"] == 0
        and report["duplicate_rows"] == 0
        and report["bad_status"] == 0
    )
    assert report["status_by_source"]["fake_grd"] == {"pending": 4, "seen": 1}
    # drop a row: the audit must notice
    report = audit(df.drop(df.index[(df["source"] == "fake_grd")][:1]), meta)
    assert report["missing_rows"] == 1


class FakeBurstSource(FakeSource):
    key = "fake_bursts"
    burst_level = True

    def lookup(self, ids):
        self.asked.append(list(ids))
        return {
            i: Found(n_items=2, notes={"items": {"B1", "B2"}})
            for i in ids
            if i in self.available and self.available[i] <= self.now
        }


def run_with(tmp_path, now, catalog, sources):
    for s in sources.values():
        s.now = now
    config = TrackerConfig(source_options={k: {} for k in sources})
    tracker = Tracker(
        Store(tmp_path), config, now=now, catalog=catalog, sources=sources
    )
    return tracker.run()


def test_burst_first_sighting_after_new_rows_regression(tmp_path):
    """2026-09-29: a burst match on a row whose `items` was NaN crashed every run."""
    a = acq(1, T0 - timedelta(hours=2), T0 - timedelta(minutes=30), kind="SLC")
    b = acq(2, T0 + timedelta(minutes=30), T0 + timedelta(minutes=50), kind="SLC")
    catalog = FakeCatalog({T0: [a], T0 + timedelta(hours=1): [b]})
    ref = type("RefSLC", (RefGRD,), {"key": "cdse_slc", "reference_product": "SLC"})(
        session=object()
    )
    bursts = FakeBurstSource(available={a.granule_id: T0 + timedelta(hours=1)})
    bursts.reference_product = "SLC"
    sources = {"cdse_slc": ref, "fake_bursts": bursts}
    run_with(tmp_path, T0, catalog, sources)
    r = run_with(
        tmp_path, T0 + timedelta(hours=1), catalog, sources
    )  # new rows + first sighting
    assert r.exit_code == 0 and r.sightings["fake_bursts"] == 1
    row = table(tmp_path).loc[(a.granule_id, "fake_bursts")]
    assert row["items"] == "B1,B2" and row["n_items"] == 2
    assert row["last_item_seen_utc"] == T0 + timedelta(hours=1)
    assert pd.isna(row["closed_utc"])  # still collecting bursts


def test_error_applying_results_is_isolated_and_rolled_back(tmp_path):
    a = acq(1, T0 - timedelta(hours=2), T0 - timedelta(minutes=30))

    class Garbage(FakeSource):
        key = "garbage"

        def lookup(self, ids):
            # a result that makes the tracker itself fail while applying it
            return {i: Found(provider_timestamp="not a datetime") for i in ids}

    good = FakeSource(available={a.granule_id: T0})
    sources = {
        "cdse_grd": RefGRD(session=object()),
        "garbage": Garbage(),
        "fake_grd": good,
    }
    r = run_with(tmp_path, T0, FakeCatalog({T0: [a]}), sources)
    assert r.exit_code == 0
    df = table(tmp_path)
    assert df.loc[(a.granule_id, "fake_grd"), "status"] == "seen"  # other work kept
    assert df.loc[(a.granule_id, "garbage"), "status"] == "pending"  # rolled back
    assert df.loc[(a.granule_id, "garbage"), "n_checks"] == 0
    health = Store(tmp_path).read_json("health.json")
    assert health["sources"]["garbage"]["last_error"]
