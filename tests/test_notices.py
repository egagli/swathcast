"""Agency notices fetched for the Satellites page (notices.py): each feed's
parser, matching a title to satellites, a failing source, the review list."""

from datetime import UTC, datetime

from swathcast import notices

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

CDSE = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title>Copernicus Sentinel-1D Data Unavailability on 28 October 2026</title>
<link>https://dataspace.copernicus.eu/news/2026-10-30-copernicus-sentinel-1d-data-unavailability-28-october-2026</link>
<pubDate>Fri, 30 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>Sentinel-2A Extension Campaign to Continue Until the End of 2027</title>
<link>https://dataspace.copernicus.eu/news/2026-10-9-sentinel-2a-extension-campaign-continue-until-end-2027</link>
<pubDate>Fri, 09 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>Delay in publication of Sentinel-1, Sentinel-2 and Sentinel-3 data on 01-11-2026</title>
<link>https://dataspace.copernicus.eu/news/2026-11-3-delay</link>
<pubDate>Tue, 03 Nov 2026 10:00:00 +0000</pubDate></item>
<item><title>What's new in openEO</title><link>https://dataspace.copernicus.eu/news/openeo</link>
<pubDate>Fri, 30 Oct 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

EARTHDATA = """<div><h3><a href="/data/alerts-outages/viirs-data-gaps-processing-delays-from-november-5-2026">VIIRS data gaps and processing delays from November 5, 2026</a></h3>
<dl><dt>Issued:</dt><dd>Nov. 9, 2026</dd><dt>Resolved:</dt><dd>TBD</dd></dl></div>
<div><h3><a href="/data/alerts-outages/terra-modis-placed-standby-mode">Terra MODIS Placed in Standby Mode</a></h3>
<dl><dt>Issued:</dt><dd>March 26, 2026</dd><dt>Resolved:</dt><dd>March 27, 2026</dd></dl></div>"""

OSPO = """<li><a href="../data/messages/2026/11/MSG_20261102_1800.html">Administrative: Update #4 - Cessation of Suomi National Polar-orbiting Partnership (S-NPP) data to users - Issued: November 02, 2026 1800Z</a></li>
<li><a href="../data/messages/2026/11/MSG_20261105_1209.html">Product Outage/Anomaly: NOAA-21 SMD Data Delay - Issued: November 05, 2026 1209Z</a></li>
<li><a href="../data/messages/2026/11/MSG_20261109_1819.html">Administrative: GOES-WEST MDS Meso-1 Scheduled</a></li>"""

USGS = """<a href="/centers/eros/2026-10-20-new-landsat-8-9-product-availability-interruption">Newly acquired Landsat 8 and 9 data are currently unavailable</a>
<a href="/centers/eros/about">About EROS</a>"""


def test_parsers():
    cdse = notices.parse_cdse(CDSE)
    assert cdse[0]["date"] == "2026-10-30" and "Sentinel-1D" in cdse[0]["title"]
    ed = notices.parse_earthdata(EARTHDATA)
    assert [(x["date"], x["resolved"]) for x in ed] == [
        ("2026-11-09", "TBD"),
        ("2026-03-26", "2026-03-27"),
    ]
    assert ed[0]["url"].startswith(
        "https://www.earthdata.nasa.gov/data/alerts-outages/viirs-data-gaps"
    )
    ospo = notices.parse_ospo(OSPO)
    assert ospo[0]["date"] == "2026-11-02"
    assert (
        ospo[0]["url"]
        == "https://www.ospo.noaa.gov/data/messages/2026/11/MSG_20261102_1800.html"
    )
    usgs = notices.parse_usgs(USGS)
    assert len(usgs) == 1 and usgs[0]["date"] == "2026-10-20"


def test_match_titles_to_satellites():
    assert notices.match("Copernicus Sentinel-1D Data Unavailability") == (
        ["S1D"],
        ["s1"],
    )
    assert notices.match("S1C/D Final Orbital Configuration Achieved") == (
        ["S1C", "S1D"],
        ["s1"],
    )
    assert notices.match(
        "Delay in publication of Sentinel-1, Sentinel-2 and Sentinel-3 data"
    ) == ([], ["s1", "s2", "s3"])
    assert notices.match(
        "Cessation of Suomi National Polar-orbiting Partnership (S-NPP) data"
    ) == (["SNPP"], ["viirs"])
    assert notices.match("Newly acquired Landsat 8 and 9 data") == (
        ["L8", "L9"],
        ["landsat"],
    )
    assert notices.match("GOES-WEST MDS Meso-1 Scheduled") == ([], [])
    # a mission is named only where none of its satellites is
    assert notices.match("VIIRS data gaps") == ([], ["viirs"])


def test_collect_keeps_matching_items_and_survives_a_failing_source():
    pages = {
        notices.CDSE_RSS: CDSE,
        notices.EARTHDATA_ALERTS: EARTHDATA,
        notices.OSPO_MESSAGES: OSPO,
    }

    def fetch(url):
        if url not in pages:
            raise OSError("timed out")
        return pages[url]

    cited = {
        "https://dataspace.copernicus.eu/news/2026-10-9-sentinel-2a-extension-campaign-continue-until-end-2027"
    }
    rep = notices.collect(fetch, now=NOW, cited=cited)
    assert (
        rep["sources"]["usgs"]["ok"] is False
        and "timed out" in rep["sources"]["usgs"]["error"]
    )
    assert rep["sources"]["cdse"]["ok"] and rep["sources"]["cdse"]["items"] == 4
    titles = [x["title"] for x in rep["items"]]
    assert not any(
        "openEO" in t or "GOES" in t for t in titles
    )  # names no tracked satellite
    assert [x["date"] for x in rep["items"]] == sorted(
        (x["date"] for x in rep["items"]), reverse=True
    )
    # the review list: lifecycle titles naming a satellite, not cited
    review = notices.review(rep, now=NOW)
    assert [x["platforms"] for x in review] == [["SNPP"]]
    marker, body = notices.issue_body(review)
    assert (
        marker.startswith("review: https://www.ospo.noaa.gov/") and "| SNPP |" in body
    )
    assert notices.issue_body([])[0] == "review: "


def test_old_items_are_dropped():
    old = CDSE.replace("Fri, 30 Oct 2026", "Fri, 30 Jan 2026")
    rep = notices.collect(lambda url: old if url == notices.CDSE_RSS else "", now=NOW)
    assert not any("Sentinel-1D" in x["title"] for x in rep["items"])


def test_a_failing_source_keeps_its_last_items():
    prev = notices.collect(
        lambda url: (
            EARTHDATA
            if url == notices.EARTHDATA_ALERTS
            else (_ for _ in ()).throw(OSError("x"))
        ),
        now=NOW,
    )
    assert prev["sources"]["earthdata"]["ok"]

    def fetch(url):
        if url == notices.EARTHDATA_ALERTS:
            raise OSError("403 Forbidden")
        return {notices.CDSE_RSS: CDSE, notices.OSPO_MESSAGES: OSPO}.get(url, "")

    rep = notices.collect(fetch, now=NOW, previous=prev)
    ed = rep["sources"]["earthdata"]
    assert (
        not ed["ok"]
        and ed["kept"] == 1
        and ed["last_ok"] == prev["sources"]["earthdata"]["fetched_at"]
    )
    assert any(x["source"] == "earthdata" for x in rep["items"])
    # failing again: still the same last good date
    again = notices.collect(fetch, now=NOW, previous=rep)
    assert again["sources"]["earthdata"]["last_ok"] == ed["last_ok"]
