"""The map in a real browser (Playwright + Chromium), on a small synthetic site:
one planned Sentinel-1 EW pass over Seattle a day from now, every catalogue
and tile server stubbed (empty answers), the JavaScript libraries from their
CDNs. Skipped where Playwright, Chromium or the CDNs are not available.

Each test replays a scenario from the map review (docs: the one list and its
"Now" line, one selection, AOI checks and the antimeridian, dates, the
calendar's escaping, the phone layout)."""

import functools
import http.server
import json
import os
import shutil
import socket
import struct
import threading
import time
import urllib.request
import zlib
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from swathcast.publish import split_schedule

# CI sets this: there a missing browser or CDN is a failure, not a skip
REQUIRED = os.environ.get("SWATHCAST_REQUIRE_BROWSER") == "1"
if REQUIRED:
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")


def unavailable(why):
    if REQUIRED:
        pytest.fail(why)
    pytest.skip(why)


CDN = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.min.js"
STUBS = {  # catalogue hosts -> an empty answer of the shape the page expects
    "api.daac.asf.alaska.edu": {"type": "FeatureCollection", "features": []},
    "cmr.earthdata.nasa.gov": {"items": [], "feed": {"entry": []}},
    "earth-search.aws.element84.com": {"type": "FeatureCollection", "features": []},
    "planetarycomputer.microsoft.com": {"type": "FeatureCollection", "features": []},
    "catalogue.dataspace.copernicus.eu": {"value": []},
    "api.eumetsat.int": {
        "type": "FeatureCollection",
        "features": [],
        "totalResults": 0,
    },
}


def png_1x1():
    """A transparent 1x1 PNG, built with its CRCs (a hand-typed one that
    did not decode left MapLibre failing every tile)."""

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + b"\x00\x00\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


# a transparent 1x1 PNG for every basemap tile
PNG = png_1x1()


def wkb_polygon(ring):
    body = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring))
    return body + b"".join(struct.pack("<dd", x, y) for x, y in ring)


def build_site(root, now):
    """The site's files and a two-pass schedule over Seattle: S1D EW in a day,
    and S1D EW 6 h ago (imaged, in no catalogue yet)."""
    site = root / "site"
    data = site / "data"
    data.mkdir(parents=True)
    for asset in (files("swathcast") / "site").iterdir():
        shutil.copyfile(asset, site / asset.name)
    begins = [now - timedelta(hours=6), now + timedelta(days=1)]
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    ring = [
        (-124.5, 46.5),
        (-121.0, 46.5),
        (-121.0, 49.0),
        (-124.5, 49.0),
        (-124.5, 46.5),
    ]
    pq.write_table(
        pa.table(
            {
                "sensor": ["sentinel-1"] * 2,
                "platform": ["S1D"] * 2,
                "mode": ["EW"] * 2,
                "orbit_direction": ["descending"] * 2,
                "track": pa.array([115] * 2, pa.int32()),
                "polarization": ["HH+HV"] * 2,
                "begin_time": pa.array(
                    [ms(b) for b in begins], pa.timestamp("ms", tz="UTC")
                ),
                "end_time": pa.array(
                    [ms(b + timedelta(minutes=2)) for b in begins],
                    pa.timestamp("ms", tz="UTC"),
                ),
                "time_is_estimated": [False] * 2,
                "geometry": [wkb_polygon(ring)] * 2,
            }
        ),
        data / "upcoming_passes.parquet",
    )
    split_schedule(data)
    stamp = now.isoformat()
    (data / "upcoming_passes.meta.json").write_text(json.dumps({"generated_at": stamp}))
    exp = {
        "measured": True,
        "median_h": 3.0,
        "p90_h": 5.0,
        "stratum": "all",
        "n_events": 99,
    }
    (data / "latency.json").write_text(
        json.dumps(
            {
                "data_as_of": stamp,
                "expected": {"cdse_grd": {"all": exp}},
                "sources": {"cdse_grd": {"label": "CDSE GRD"}},
            }
        )
    )
    (data / "health.json").write_text(json.dumps({"sources": {}}))
    (data / "orbits.json").write_text(json.dumps({"platforms": {}}))
    wide_swath_site(data, now)
    return site


SEATTLE_TILE = [
    (-126.0, 44.0),
    (-118.0, 44.0),
    (-118.0, 50.0),
    (-126.0, 50.0),
    (-126.0, 44.0),
]


def wide_swath_site(data, now):
    """MODIS (Terra) and Sentinel-3 (S3A) on one tile over Seattle: their
    passes from the fixture's orbital elements (wideswath.py, as the schedule
    job), and the same elements as tle.json, which the page times passes at
    the AOI with. Skipped without sgp4 (the page then has no such files)."""
    try:
        from swathcast import wideswath
        from swathcast.ephemeris import satrec
    except ImportError:
        return
    import pandas as pd

    from conftest import load_fixture

    omm = load_fixture("celestrak_omm.json")
    t0, t1 = (
        pd.Timestamp(now) - pd.Timedelta(days=2),
        pd.Timestamp(now) + pd.Timedelta(days=3),
    )
    centre = pd.DataFrame({"tile": ["h09v04"], "lon": [-122.0], "lat": [47.0]})
    sats = {p: satrec(omm[p]) for p in ("Terra", "S3A")}
    pq.write_table(
        pa.table(
            {
                "tile": ["h09v04"],
                "h": pa.array([9], pa.int8()),
                "v": pa.array([4], pa.int8()),
                "geometry": [wkb_polygon(SEATTLE_TILE)],
            }
        ),
        data / "sin_grid.parquet",
    )
    for mission, name in (
        ("modis", "modis_tile_passes"),
        ("sentinel-3", "s3_tile_passes"),
    ):
        p = wideswath.sin_tile_passes(centre, sats, mission, t0, t1)
        cols = {
            "tile": [str(x) for x in p["tile"]],
            "platform": [str(x) for x in p["platform"]],
            "time": pa.array(
                [int(t.timestamp() * 1000) for t in p["time"]],
                pa.timestamp("ms", tz="UTC"),
            ),
            "direction": [str(x) for x in p["direction"]],
            "xtrack_km": pa.array(p["xtrack_km"].astype(int).tolist(), pa.int16()),
        }
        if mission == "sentinel-3":
            cols["track"] = pa.array(p["track"].astype(int).tolist(), pa.int16())
            cols["anx_s"] = pa.array(p["anx_s"].astype(int).tolist(), pa.int16())
        pq.write_table(pa.table(cols), data / f"{name}.parquet")
    (data / "tle.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "satellites": {
                    p: {"norad": omm[p]["NORAD_CAT_ID"], "omm": omm[p]} for p in sats
                },
            }
        )
    )


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        urllib.request.urlopen(CDN, timeout=10).close()
    except OSError:
        unavailable("the JavaScript CDNs are not reachable")
    site = build_site(tmp_path_factory.mktemp("browser"), datetime.now(UTC))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=site)
    handler.log_message = lambda *a: None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/index.html"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(
                args=[
                    "--use-gl=angle",
                    "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader",
                ]
            )
        except Exception as exc:  # noqa: BLE001 - no browser installed
            unavailable(f"Chromium unavailable: {exc}")
        yield b
        b.close()


def stub(route):
    url = route.request.url
    host = url.split("/")[2]
    if host in STUBS:
        return route.fulfill(
            status=200,
            content_type="application/json",
            headers={"access-control-allow-origin": "*"},
            body=json.dumps(STUBS[host]),
        )
    if "tile" in url or "arcgisonline" in url:
        return route.fulfill(status=200, content_type="image/png", body=PNG)
    if "fonts.g" in url or "celestrak" in url:
        return route.abort()
    # the libraries: fetched here (Python honours a proxy the browser may not
    # be told about), once per run
    if url not in CACHE:
        try:
            CACHE[url] = fetch_library(url)
        except OSError as exc:
            # answered, never left hanging: a request the stub does not answer
            # stalls the page until the load timeout, with no clue why
            FAILED.append(f"{url}: {exc}")
            return route.fulfill(status=502, body=str(exc))
    status, ctype, body = CACHE[url]
    return route.fulfill(
        status=status,
        content_type=ctype,
        headers={"access-control-allow-origin": "*"},
        body=body,
    )


CACHE = {}
FAILED = []  # library URLs the stub could not fetch


def fetch_library(url, attempts=4):
    """A CDN file, retried: one slow answer from a CDN must not stall the
    page past its load timeout (it did on a CI runner)."""
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return r.status, r.headers.get("content-type", ""), r.read()
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


def open_page(browser, server, hash_="", width=1400, height=850):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(lambda u: not u.startswith("http://127.0.0.1"), stub)
    requests, done, console = [], set(), []
    page.on("request", lambda r: requests.append(r.url))
    page.on("requestfinished", lambda r: done.add(r.url))
    page.on("requestfailed", lambda r: done.add(r.url))
    page.on("console", lambda m: console.append(f"{m.type}: {m.text[:200]}"))
    page.goto(f"{server}#{hash_}")
    try:
        page.wait_for_function(
            "window.__state && window.__state.scheduleEnd && !window.__state.busy",
            timeout=120000,
        )
    except playwright_api.TimeoutError as exc:
        state = page.evaluate(
            "window.__state ? {busy: window.__state.busy, scheduleEnd: String(window.__state.scheduleEnd)} : null"
        )
        pending = [u for u in requests if u not in done]
        other = [u for u in requests if "tile" not in u and "arcgisonline" not in u]
        raise AssertionError(
            f"page never finished loading: state {state}, page errors {errors}, "
            f"library fetches failed {FAILED}, unanswered requests {pending}, "
            f"non-tile requests {other}, console (first) {console[:10]} (last) {console[-5:]}"
        ) from exc
    page.wait_for_timeout(1500)
    page.errors = errors
    return page


def js(page, code):
    return page.evaluate(code)


def test_one_list_with_now_and_one_selection(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6")
    assert page.errors == []
    # two tabs, one list at a time: "Planned" (open) from "Now", soonest first
    tabs = js(
        page,
        "[...document.querySelectorAll('#list-tabs button')].map(b => b.innerText.replace(/\\s+/g, ' ') + (b.classList.contains('on') ? ' *' : ''))",
    )
    assert tabs[0].startswith("Planned 1") and tabs[0].endswith("*")
    assert tabs[1].startswith("Last 90 days 1")
    kinds = js(
        page,
        "[...document.querySelectorAll('table.pass-list tbody tr')].map(r => r.className.split(' ')[0])",
    )
    assert kinds[0] == "now-row" and "sec" not in kinds
    rows = js(
        page,
        "[...document.querySelectorAll('table.pass-list tr.item')].map(r => r.className + ' ' + r.innerText)",
    )
    assert len(rows) == 1 and "S1D" in rows[0] and "in 24.0 h" in rows[0]
    # the other tab: the pass of 6 h ago, in no catalogue yet (EW: no RTC or CSLC is made)
    page.click('#list-tabs [data-tab="past"]')
    rows = js(
        page,
        "[...document.querySelectorAll('table.pass-list tr.item')].map(r => r.className + ' ' + r.innerText)",
    )
    assert len(rows) == 1 and rows[0].startswith("item past") and "6.0 h ago" in rows[0]
    page.click('#list-tabs [data-tab="planned"]')
    table = js(
        page,
        "[document.querySelector('table.pass-list').scrollWidth, document.querySelector('#results').clientWidth]",
    )
    assert table[0] <= table[1]
    # the legend says what Sentinel-1 shows and where its plan ends
    assert "Sentinel-1" in js(page, "document.getElementById('detail-hint').innerText")
    assert "plan to" in js(page, "document.getElementById('detail-hint').innerText")
    # selecting the row: its card, a one-line map label, its timeline marker
    page.click("table.pass-list tr.item")
    assert js(page, "document.querySelectorAll('#results tr.sel').length") == 1
    # (the synthetic pass is EW: latency is measured for IW only, and the card says so)
    assert "measured for IW only" in js(
        page, "document.querySelector('tr.detail-row').innerText"
    )
    assert "S1D" in js(page, "document.querySelector('.label-pop').innerText")
    assert js(page, "document.querySelectorAll('#aoi-timeline .mk.sel').length") == 1
    # a footprint picked on the map takes over the selection
    xy = js(
        page,
        "(() => { const q = window.__map.project([-122.3, 47.6]), r = window.__map.getCanvas().getBoundingClientRect(); return [q.x + r.left + 3, q.y + r.top + 3]; })()",
    )
    page.mouse.click(*xy)
    page.wait_for_timeout(1500)
    assert js(page, "document.querySelectorAll('#results tr.sel').length") == 0
    assert "Sentinel-1" in js(
        page,
        "document.querySelector('.maplibregl-popup:not(.label-pop) .headline').innerText",
    )
    # keyboard: a row is focusable and Enter selects it
    js(page, "document.querySelector('table.pass-list tr.item').focus()")
    page.keyboard.press("Enter")
    assert js(page, "document.querySelectorAll('#results tr.sel').length") == 1
    # a past pass picked on the timeline opens its tab, selected
    js(
        page,
        "[...document.querySelectorAll('#aoi-timeline .mk')].find(m => m.dataset.key.startsWith('r|')).dispatchEvent(new MouseEvent('click', {bubbles: true}))",
    )
    assert js(page, "document.querySelector('#list-tabs .on').dataset.tab") == "past"
    assert js(page, "document.querySelectorAll('#results tr.sel.past').length") == 1
    page.close()


def paste(page, text):
    if not page.is_visible("#paste-text"):
        page.click("#paste")
    page.fill("#paste-text", text)
    page.click("#paste-apply")
    page.wait_for_timeout(2500)
    return js(page, "document.getElementById('paste-err').textContent")


def test_aoi_checks_and_the_antimeridian(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6")
    before = js(page, "JSON.stringify(window.__state.aoi)")
    assert "write longitude first" in paste(page, "POINT(47.6 -122.3)").lower()
    assert "no features" in paste(page, '{"type":"FeatureCollection","features":[]}')
    assert js(page, "JSON.stringify(window.__state.aoi)") == before  # unchanged
    assert paste(page, "POLYGON((179 -17, -179 -17, -179 -16, 179 -16, 179 -17))") == ""
    assert "crossing 180" in js(
        page, "document.getElementById('draw-hint').textContent"
    )
    # the camera goes there (not the far side of the Earth), once it has moved
    page.wait_for_function(
        "Math.abs(window.__map.getCenter().lng) > 170", timeout=15000
    )
    assert js(page, "window.__state.aoi.type") == "MultiPolygon"
    assert paste(page, "-124,47.5,-123,48.1") == ""  # a w,s,e,n box
    assert page.errors == []
    page.close()


def test_dates_and_calendar(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6")
    start, end, lo = js(
        page,
        "['date-start', 'date-end'].map(i => document.getElementById(i).value).concat(document.getElementById('date-start').min)",
    )
    assert start == lo and end > start
    # an emptied date goes back to its default; reversed dates swap on screen
    js(
        page,
        "(() => { const e = document.getElementById('date-start'); e.value = ''; e.dispatchEvent(new Event('change')); })()",
    )
    assert js(page, "document.getElementById('date-start').value") == lo
    with page.expect_download() as dl:
        page.click(".export-bar button:nth-of-type(2)")
    ics = open(dl.value.path()).read()
    desc = "".join(
        line.lstrip()
        for line in ics.replace("\r\n ", "").splitlines()
        if line.startswith("DESCRIPTION")
    )
    assert desc and ";" not in desc.replace("\\;", "")  # RFC 5545: semicolons escaped
    page.close()


def test_phone_layout(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6", width=390, height=800)
    assert page.errors == []
    assert (
        js(page, "document.querySelector('.maplibregl-ctrl-zoom-in')") is None
    )  # no +/- buttons
    page.click("#legend-fold")
    assert (
        js(
            page,
            "getComputedStyle(document.getElementById('grid-palette').closest('label')).display",
        )
        != "none"
    )
    page.click(".mo-fold")
    assert (
        js(
            page,
            "getComputedStyle(document.querySelector('.map-opts .mo-row')).display",
        )
        != "none"
    )
    assert (
        js(page, "getComputedStyle(document.getElementById('aoi-timeline')).display")
        != "none"
    )
    page.close()


def test_direction_pills_and_reset(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6&m=s1,s2,landsat,nisar")
    pills = js(
        page,
        """Object.fromEntries([...document.querySelectorAll('.mission-block')].map(b => [
          b.querySelector('.mission-row').innerText.trim().split('\\n')[0],
          [...b.querySelectorAll('.frow')].filter(r => r.querySelector('.flabel').textContent === 'Direction')
            .flatMap(r => [...r.querySelectorAll('label.check')].map(l => [l.textContent.trim(), l.querySelector('input').checked, l.querySelector('input').disabled]))
        ]))""",
    )
    flat = {name: v for name, v in pills.items() if v}
    s1 = next(v for k, v in flat.items() if k.startswith("Sentinel-1"))
    assert ["Ascending (~6 PM)", True, False] in s1
    s2 = next(v for k, v in flat.items() if k.startswith("Sentinel-2"))
    assert [
        "Ascending (~10:30 PM)",
        False,
        True,
    ] in s2  # Sentinel-2 images descending only
    ls = next(v for k, v in flat.items() if k.startswith("Landsat"))
    assert ["Ascending (~10 PM)", False, False] in ls  # night scenes off by default
    assert ["Descending (~10 AM)", True, False] in ls
    nisar = next(v for k, v in flat.items() if k.startswith("NISAR"))
    assert ["Descending (~6 PM)", True, False] in nisar
    # Reset: confirmed, it clears the link's state and the remembered settings
    js(page, "localStorage.setItem('swathcast.basemap', 'osm')")
    page.once("dialog", lambda d: d.accept())
    with page.expect_navigation():
        page.click("#reset")
    page.wait_for_function("window.__state && !window.__state.busy", timeout=60000)
    assert js(page, "window.__state.aoi") is None
    assert js(page, "localStorage.getItem('swathcast.basemap')") is None
    # the map starts at zoom 3, and goes no further out
    assert js(page, "window.__map.getZoom()") == pytest.approx(3)
    assert js(page, "window.__map.getMinZoom()") == 3
    page.close()


def test_first_visit_tour(browser, server):
    # a first visit (no link state): the tour opens on its own, once
    page = open_page(browser, server)
    page.wait_for_selector(".tour:not([hidden])", timeout=15000)
    assert "1 of 6" in js(page, "document.querySelector('.tour-n').textContent")
    assert js(page, "localStorage.getItem('swathcast-tour')") == "seen"
    # each step: a red ring and label on what it is about
    assert "Missions" in js(
        page, "document.querySelector('.tour-label:not([hidden])').textContent"
    )
    # footprints at 30 % from the start
    assert js(page, "document.getElementById('opacity').value") == "30"
    # steps 1-3 show Landsat alone: Sentinel-1 unticked, Landsat ticked, visibly
    page.wait_for_function(
        """(() => { const on = [...document.querySelectorAll('.mission-row label.mission input')]
          .filter(i => i.checked).map(i => i.closest('label').textContent.trim());
          return on.length === 1 && on[0].startsWith('Landsat'); })()""",
        timeout=20000,
    )
    page.click(".tour-next")  # 2: dates
    page.click(".tour-next")  # 3: a footprint at Mt. Rainier
    page.click(".tour-next")  # 4: an AOI at Paradise
    page.wait_for_function(
        "window.__state.aoi && window.__state.aoi.type === 'Point'", timeout=40000
    )
    assert js(page, "window.__state.aoi.coordinates[0]") == pytest.approx(-121.7357)
    page.click(".tour-next")  # 5: its passes, the next one opened
    page.wait_for_selector("table.pass-list tr.item.sel", timeout=30000)
    assert js(page, "document.getElementById('aoi-timeline').hidden") is False
    # 6, on the Delivery page
    with page.expect_navigation():
        page.click(".tour-next")
    page.wait_for_selector(".tour:not([hidden])", timeout=15000)
    assert "6 of 6" in js(page, "document.querySelector('.tour-n').textContent")
    # the end starts the map over: no AOI, nothing in the address
    with page.expect_navigation():
        page.click(".tour-next")
    page.wait_for_function(
        "window.__state && window.__state.scheduleEnd", timeout=60000
    )
    page.wait_for_timeout(1000)
    assert js(page, "window.__state.aoi") is None
    assert js(page, "location.hash") == ""
    # seen: no tour this time
    assert js(
        page,
        "!document.querySelector('.tour') || document.querySelector('.tour').hidden",
    )
    assert page.errors == []
    page.close()


def test_only_the_passes_scroll(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6")
    js(page, "document.querySelector('.aoi-part > .panel-scroll').scrollTop = 400")
    top = js(
        page, "document.getElementById('sec-missions').getBoundingClientRect().top"
    )
    assert 50 < top < 140  # still under the panel's title bar
    page.close()


def test_now_sticks(browser, server):
    page = open_page(browser, server, "aoi=-122.3,47.6")
    # a short list box, scrolled to its end: "Now" stays under the column names
    rows = js(
        page,
        """(() => {
          const sc = document.querySelector('.aoi-part > .panel-scroll');
          sc.style.flex = 'none'; sc.style.height = '120px';
          sc.scrollTop = sc.scrollHeight;
          const r = (q) => document.querySelector(q).getBoundingClientRect();
          return [r('table.pass-list thead th').bottom, r('tr.now-row td').top, sc.getBoundingClientRect().top];
        })()""",
    )
    head_bottom, now_top, box_top = rows
    assert now_top == pytest.approx(head_bottom, abs=2)
    assert now_top > box_top
    page.close()


def test_wide_swath_missions_over_an_aoi(browser, server):
    """MODIS and Sentinel-3 (no plans: passes predicted from the orbit) over
    an AOI: each pass timed at the AOI, kept only inside its swath, with its
    view angle, light and (Sentinel-3) frame; the Light and view-angle
    filters, and their place in the link."""
    pytest.importorskip("sgp4")
    page = open_page(browser, server, "aoi=-122.3,47.6&m=modis,s3")
    assert page.errors == []
    page.wait_for_function(
        "window.__state.passes.some(p => p.mission === 'modis') && window.__state.passes.some(p => p.mission === 's3')",
        timeout=30000,
    )
    passes = js(
        page,
        """window.__state.passes.filter(p => ['modis', 's3'].includes(p.mission)).map(p => ({
          m: p.mission, plat: p.platform, timed: p.timedAtAOI, x: p.xtrack, vza: p.vza, light: p.light,
          track: p.track, frame: p.frame, inst: p.instruments || null, shapes: p.shapes.length }))""",
    )
    assert {p["m"] for p in passes} == {"modis", "s3"}
    for p in passes:
        assert p["timed"] and p["shapes"] == 1  # the strip imaged around the AOI
        assert p["light"] == "day"  # night passes are off at the start
        lo, hi = (-1178, 1178) if p["plat"] == "Terra" else (-500, 996)
        assert lo - 30 <= p["x"] <= hi + 30
        if p["m"] == "s3":
            assert 1 <= p["track"] <= 385 and p["frame"] % 180 == 0
            assert "SLSTR" in p["inst"]
        else:
            assert 0 <= p["vza"] < 70
    # the light filter: Day ticked, Night not; the view angle in the link
    pills = js(
        page,
        """[...document.querySelectorAll('.mission-block')].filter(b => b.innerText.startsWith('MODIS'))
          .flatMap(b => [...b.querySelectorAll('.frow')].filter(r => r.querySelector('.flabel').textContent === 'Light')
          .flatMap(r => [...r.querySelectorAll('label.check')].map(l => [l.textContent.trim(), l.querySelector('input').checked])))""",
    )
    assert pills == [["Day", True], ["Night", False]]
    assert "predicted to" in js(
        page, "document.getElementById('detail-hint').innerText"
    )
    page.close()
    page = open_page(
        browser, server, "aoi=-122.3,47.6&m=modis&modis.vza=30&modis.light=day,night"
    )
    page.wait_for_timeout(3000)
    vza = js(
        page,
        "window.__state.passes.filter(p => p.mission === 'modis').map(p => [p.vza, p.light])",
    )
    assert all(v <= 30 for v, _ in vza)
    assert "modis.vza=30" in js(page, "location.hash")
    assert page.errors == []
    page.close()
