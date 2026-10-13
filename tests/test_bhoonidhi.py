"""Bhoonidhi (ISRO) NISAR S-band source, against a fake server.

The fake follows the published API specification
(https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/, read 2026-10-01). Item names and
properties are copied from a live item
(2026-10-01): ``updated`` is India time, ``GenerationTime`` UTC.
"""

from datetime import UTC, datetime, timedelta

import pytest

from swathcast import missions
from swathcast.sources import bhoonidhi
from swathcast.storage import Store
from swathcast.tracker import Tracker

NOW = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
RSLC = "NISAR_S1_PR_RSLC_032_019_D_079_2500_CRNA_A_20261001T124419_20261001T124424_P00500_N_F_I_001"
GCOV = "NISAR_S2_PR_GCOV_032_019_D_079_2500_CRNA_A_20261001T124419_20261001T124424_P00500_N_F_I_001"


def item(gid, when, updated="2026-10-01 22:56:03", **extra):
    """A live item's shape (trimmed); ``updated`` = 17:26:03 UTC."""
    props = {
        "datetime": when,
        "Track": 19,
        "Frame": 79,
        "Node": "Descending",
        "Polarization": "CP",
        "CycleNumber": 32,
        "Imaging_Orbit": 6173,
        "SceneCenterLon": 87.91846049,
        "SceneCenterLat": 19.7617044,
        "GenerationTime": "2026-10-01T17:04:17",
        "Coverage": "F",
        **extra,
    }
    if updated:
        props["updated"] = updated
    return {
        "type": "Feature",
        "id": gid,
        "bbox": [86.64219282, 20.20409388, 89.03512454, 19.35179576],
        "properties": props,
    }


#: the live error body behind "Bhoonidhi search HTTP 404: 404: NO RESULTS FOUND"
#: (2026-10-12)
NO_RESULTS = {"ErrorCode": 404, "Description": "NO RESULTS FOUND"}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, str):  # as requests does for a non-JSON body
            raise ValueError(self._body)
        return self._body


class FakeSession:
    """POST /auth/token, /data/search (one page + a rel=next page), /auth/logout."""

    def __init__(self, pages=None, gcov=()):
        self.calls = []
        self.pages = pages or [[item(RSLC, "2026-10-01T12:44:19Z")]]
        self.gcov = set(gcov)
        self.searches = []
        #: the one-day window that holds the items (NOW's day: the newest)
        self.day = "2026-09-30T18:00:00Z/2026-10-01T17:59:59Z"

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        if url.endswith("/auth/token"):
            assert json["grant_type"] == "password" and json["userId"] == "u"
            return Resp(
                200,
                {
                    "userId": "u",
                    "access_token": "A",
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "refresh_token": "R",
                },
            )
        if url.endswith("/auth/logout"):
            assert headers["Authorization"] == "Bearer R"
            return Resp(200, {})
        assert headers["Authorization"] == "Bearer A"
        if "ids" in json:  # lookup by id
            col = json["collections"][0]
            hits = [
                i
                for i in json["ids"]
                if (col.endswith("GCOV") and i in self.gcov) or col.endswith("RSLC")
            ]
            if not hits:  # the live API's answer to a search matching nothing
                return Resp(404, NO_RESULTS)
            return Resp(
                200,
                {
                    "features": [item(i, "2026-10-01T12:44:19Z") for i in hits],
                    "links": [],
                },
            )
        self.searches.append(json["datetime"])
        if json["datetime"] != self.day:
            return Resp(200, {"features": [], "links": []})
        page = self.pages[0]
        return Resp(
            200,
            {
                "features": page,
                "links": [
                    {
                        "rel": "next",
                        "href": "https://bhoonidhi-api.nrsc.gov.in/data/search?page=2",
                        "method": "GET",
                    }
                ]
                if len(self.pages) > 1
                else [],
            },
        )

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None, headers))
        return Resp(200, {"features": self.pages[1], "links": []})


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(bhoonidhi.ENV_USER, "u")
    monkeypatch.setenv(bhoonidhi.ENV_PASSWORD, "p")


def test_item_parses_and_gcov_name():
    a = bhoonidhi.acquisition(item(RSLC, "2026-10-01T12:44:19Z"), "RSLC", True)
    assert (a.relative_orbit, a.datatake_id, a.orbit_direction) == (
        19,
        "079",
        "descending",
    )
    assert a.coverage_key == "019-079" and a.polarization == "CP"
    assert a.timeliness == "cycle 32" and a.absolute_orbit == 6173
    # "updated" is India time: 22:56:03 IST = 17:26:03 UTC
    assert a.provider_timestamp == datetime(2026, 10, 1, 17, 26, 3, tzinfo=UTC)
    assert a.provider_timestamp_field == "updated (IST)"
    # with a publication time, promptness is the tracker's call
    assert a.listed_promptly is None
    assert bhoonidhi.gcov_name(RSLC) == GCOV


def test_without_updated_falls_back():
    a = bhoonidhi.acquisition(
        item(RSLC, "2026-10-01T12:44:19Z", updated=None), "RSLC", True
    )
    assert a.provider_timestamp == datetime(2026, 10, 1, 17, 4, 17, tzinfo=UTC)
    assert a.provider_timestamp_field == "GenerationTime"
    b = item(RSLC, "2026-10-01T12:44:19Z", updated=None)
    del b["properties"]["GenerationTime"]
    assert bhoonidhi.acquisition(b, "RSLC", False).listed_promptly is False


def test_lists_one_day_at_a_time_pages_and_logs_in_once(creds):
    s = FakeSession(
        pages=[
            [item(RSLC, "2026-10-01T12:44:19Z")],
            [item(RSLC.replace("_079_", "_080_"), "2026-10-01T12:44:30Z")],
        ]
    )
    cat = bhoonidhi.BhoonidhiCatalog(session=s)
    got = cat.list_new((NOW - timedelta(hours=4), NOW))
    assert len(got) == 2
    assert len(s.searches) == bhoonidhi.LOOKBACK_DAYS
    assert s.searches[-1] == "2026-09-21T18:00:00Z/2026-09-22T17:59:59Z"
    assert [c[1].rsplit("/", 1)[-1] for c in s.calls].count("token") == 1
    bhoonidhi.close(s)
    assert s.calls[-1][1].endswith("/auth/logout")


def test_search_errors_become_source_errors(creds):
    import requests

    class Broken(FakeSession):
        def post(self, url, json=None, headers=None, timeout=None):
            if url.endswith("/data/search"):
                raise requests.exceptions.RetryError("too many 500 error responses")
            return super().post(url, json=json, headers=headers, timeout=timeout)

    with pytest.raises(bhoonidhi.SourceError):
        bhoonidhi.BhoonidhiCatalog(session=Broken()).list_new((NOW, NOW))


class Answers(FakeSession):
    """Answers every /data/search with one fixed response."""

    def __init__(self, resp):
        super().__init__()
        self.resp = resp

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/data/search"):
            return self.resp
        return super().post(url, json=json, headers=headers, timeout=timeout)


def test_no_results_404_is_an_empty_result(creds):
    s = Answers(Resp(404, NO_RESULTS))
    assert bhoonidhi.search(s, {"collections": ["NISAR_SSAR_GCOV"]}) == []
    assert bhoonidhi.BhoonidhiGCOV(session=s).lookup([RSLC]) == {}


@pytest.mark.parametrize(
    "resp",
    [
        Resp(404, {"ErrorCode": 404, "Description": "NOT FOUND"}),
        Resp(404, "<html>Not Found</html>"),
        Resp(500, NO_RESULTS),
        Resp(503, {"ErrorCode": 503, "Description": "SERVICE UNAVAILABLE"}),
    ],
)
def test_other_errors_still_raise(creds, resp):
    with pytest.raises(bhoonidhi.SourceError):
        bhoonidhi.search(Answers(resp), {"collections": ["NISAR_SSAR_GCOV"]})
    with pytest.raises(bhoonidhi.SourceError):
        bhoonidhi.BhoonidhiGCOV(session=Answers(resp)).lookup([RSLC])


def test_mission_is_skipped_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv(bhoonidhi.ENV_USER, raising=False)
    monkeypatch.delenv(bhoonidhi.ENV_PASSWORD, raising=False)
    assert missions.run_mission("nisar_s", tmp_path) is None
    assert not (tmp_path / "missions" / "nisar_s").exists()


def test_tracker_run_first_run_is_backlog(creds, tmp_path):
    s = FakeSession(gcov={GCOV})
    m = missions.MISSIONS["nisar_s"]
    store = Store(tmp_path)
    r = Tracker(
        store,
        m.config(),
        now=NOW,
        catalog=m.catalog(session=s),
        sources=m.build_sources(s),
    ).run()
    assert r.new_acquisitions == 1 and r.sightings == {"bhoonidhi_nisar_s_gcov": 1}
    df = store.read_all()
    # even on the first run (a 24 h backlog), an item listed 34 min after its
    # "updated" time (17:26 UTC) is a prompt sighting
    assert df["prompt"].all()
    assert set(df["provider_timestamp_field"].dropna()) == {"updated (IST)"}
    assert set(df["source"]) == {"bhoonidhi_nisar_s_rslc", "bhoonidhi_nisar_s_gcov"}
