"""Orbital elements for the map: fetch, validate, keep the last good set."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import load_fixture
from swathcast import tle

NOW = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    """Serves the fixture for S1C/NISAR and fails every other satellite."""

    def __init__(self):
        self.fx = load_fixture("celestrak_omm.json")
        self.by_norad = {
            self.fx[k]["NORAD_CAT_ID"]: self.fx[k] for k in ("S1C", "NISAR")
        }
        self.calls = 0

    def get(self, url, params, timeout):
        self.calls += 1
        omm = self.by_norad.get(params["CATNR"])
        if omm is None:
            raise ConnectionResetError("reset by peer")
        return FakeResponse([omm])


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tle.time, "sleep", lambda s: None)


def test_check_omm_rejects_wrong_satellite_and_missing_fields():
    omm = load_fixture("celestrak_omm.json")["S1C"]
    assert tle.check_omm(omm, 62261)["OBJECT_NAME"] == "SENTINEL-1C"
    with pytest.raises(ValueError, match="asked for"):
        tle.check_omm(omm, 66315)
    with pytest.raises(ValueError, match="missing"):
        tle.check_omm({k: v for k, v in omm.items() if k != "BSTAR"}, 62261)


def test_failures_keep_previous_elements(tmp_path):
    path = tmp_path / "tle.json"
    old = {
        "norad": 66315,
        "omm": {"OBJECT_NAME": "SENTINEL-1D"},
        "fetched_at": "2026-09-28T00:00:00+00:00",
    }
    path.write_text(
        json.dumps(
            {"generated_at": "2026-09-28T00:00:00+00:00", "satellites": {"S1D": old}}
        )
    )
    out = tle.update(path, session=FakeSession(), now=NOW)
    assert set(out["satellites"]) == {"S1C", "NISAR", "S1D"}
    assert out["satellites"]["S1D"] == old  # kept, not dropped
    assert out["satellites"]["S1C"]["omm"]["NORAD_CAT_ID"] == 62261
    assert "S1D" in out["failed"] and "S1C" not in out["failed"]
    assert json.loads(path.read_text()) == out


def test_recent_complete_file_is_not_refetched(tmp_path):
    path = tmp_path / "tle.json"
    recent = {
        "generated_at": (NOW - timedelta(hours=2)).isoformat(),
        "failed": [],
        "satellites": {},
    }
    path.write_text(json.dumps(recent))
    session = FakeSession()
    assert tle.update(path, session=session, now=NOW, max_age_h=6) == recent
    assert session.calls == 0
    tle.update(path, session=session, now=NOW + timedelta(hours=5), max_age_h=6)
    assert session.calls > 0  # 7 h old now


def test_norad_ids_cover_every_schedule_platform():
    assert {"S1C", "S1D", "S2A", "S2B", "S2C", "L8", "L9", "NISAR"} <= set(tle.NORAD)
