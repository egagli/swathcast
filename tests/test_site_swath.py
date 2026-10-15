"""site/swathgeom.js and history.js: the live swaths' geometry and browse corners."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "src" / "swathcast" / "site"


def run_node(tmp_path, body):
    script = tmp_path / "s.mjs"
    script.write_text(
        f"""
import * as g from {json.dumps((SITE / "swathgeom.js").as_uri())};
import * as h from {json.dumps((SITE / "history.js").as_uri())};
const out = {{}};
{body}
console.log(JSON.stringify(out));
"""
    )
    res = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True
    )
    return json.loads(res.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_swaths_sit_where_the_products_are(tmp_path):
    out = run_node(
        tmp_path,
        """
for (const [s, m] of [["sentinel-1", "IW"], ["sentinel-1", "EW"], ["sentinel-1", "XX"],
  ["nisar", null], ["sentinel-2", null], ["landsat-9", null]]) out[`${s}|${m}`] = g.swathRange(s, m);
out.east = g.destination(0, 0, 90, 111.19508);
out.north = g.destination(10, 80, 0, 1111.9508);
""",
    )
    iw = out["sentinel-1|IW"]
    assert 488 <= (iw[0] + iw[1]) / 2 <= 500  # GRD centres right of the track
    assert iw[1] - iw[0] == 250
    assert out["sentinel-1|XX"] == iw  # unknown mode: IW
    assert out["sentinel-1|EW"][1] - out["sentinel-1|EW"][0] > 400
    ni = out["nisar|null"]
    assert sum(ni) / 2 == pytest.approx(-567, abs=1)  # left of the track
    assert out["sentinel-2|null"] == [-145, 145]
    assert out["landsat-9|null"] == [-92.5, 92.5]
    assert out["east"] == pytest.approx([1, 0], abs=1e-6)
    assert out["north"][1] == pytest.approx(90, abs=1e-3)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_utm_corners_match_the_item_bbox(tmp_path):
    """S2C_10TET_20261003_0_L2A: proj:transform and shape of its TCI against
    the item's own lon/lat bbox (Earth Search)."""
    out = run_node(
        tmp_path,
        """
out.c = h.utmCorners(32610, [10, 0, 499980, 0, -10, 5300040], [10980, 10980]);
out.south = h.utmToLonLat(500000, 10000000, 31, true);
out.bad = h.utmCorners(3857, [1, 0, 0, 0, -1, 0], [1, 1]);
""",
    )
    lons = [p[0] for p in out["c"]]
    lats = [p[1] for p in out["c"]]
    bbox = [-123.000251, 46.856649, -121.532736, 47.853537]
    assert min(lons) == pytest.approx(bbox[0], abs=1e-3)
    assert max(lons) == pytest.approx(bbox[2], abs=1e-3)
    assert min(lats) == pytest.approx(bbox[1], abs=1e-3)
    assert max(lats) == pytest.approx(bbox[3], abs=1e-3)
    assert out["south"] == pytest.approx([3, 0], abs=1e-9)
    assert out["bad"] is None


SATELLITE_JS = (
    "https://cdnjs.cloudflare.com/ajax/libs/satellite.js/6.0.2/satellite.min.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wide_swath_passes_match_python(tmp_path):
    """The map times a MODIS/VIIRS/Sentinel-3 pass at the AOI itself
    (swathgeom.closestApproach, satellite.js); the schedule times it at the
    tile centre (wideswath.tile_passes, sgp4). Same elements, same answer."""
    import urllib.request

    import pandas as pd

    from conftest import load_fixture
    from swathcast import wideswath
    from swathcast.ephemeris import satrec

    try:
        lib = urllib.request.urlopen(SATELLITE_JS, timeout=20).read().decode()
    except OSError:
        pytest.skip("satellite.js CDN not reachable")
    (tmp_path / "satellite.cjs").write_text(lib)
    omm = load_fixture("celestrak_omm.json")
    pts = [(-121.76, 46.85), (10.0, 78.0), (147.0, -42.0), (-60.0, -75.0)]
    t0 = pd.Timestamp("2026-10-13T00:00Z").timestamp()
    cases = []
    for plat in ("Terra", "N21", "S3A"):
        p = wideswath.tile_passes(
            satrec(omm[plat]),
            plat,
            [x for x, _ in pts],
            [y for _, y in pts],
            t0,
            t0 + 86400,
        )
        cases += [
            {
                "plat": plat,
                "lon": pts[r.i][0],
                "lat": pts[r.i][1],
                "t": r.time,
                "x": r.xtrack_km,
            }
            for r in p.itertuples()
        ]
    assert len(cases) > 20
    script = tmp_path / "w.mjs"
    script.write_text(
        f"""
import {{ createRequire }} from "module";
globalThis.satellite = createRequire(import.meta.url)({json.dumps(str(tmp_path / "satellite.cjs"))});
const g = await import({json.dumps((SITE / "swathgeom.js").as_uri())});
const omm = {json.dumps({k: omm[k] for k in ("Terra", "N21", "S3A")})};
const cases = {json.dumps(cases)};
const out = cases.map((c) => {{
  const sat = satellite.json2satrec(omm[c.plat]);
  const r = g.closestApproach(sat, c.lon, c.lat, c.t * 1000 - 900e3, c.t * 1000 + 900e3);
  return r && [r.t / 1000 - c.t, r.xtrack - c.x];
}});
console.log(JSON.stringify({{ out, vza: g.viewZenith(1171, 705), edge: g.WIDE_SWATH_KM }}));
"""
    )
    res = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    assert all(r is not None for r in res["out"])
    assert max(abs(r[0]) for r in res["out"]) < 1.0  # seconds
    assert max(abs(r[1]) for r in res["out"]) < 3.0  # km
    assert res["vza"] == pytest.approx(wideswath.view_zenith_deg(1171, 705), abs=1e-6)
    assert {k: tuple(v) for k, v in res["edge"].items()} == wideswath.SWATH_KM


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_modis_viirs_scan_lines_meet_the_real_granule_corners(tmp_path):
    """MODIS and VIIRS satellites are not yaw-steered: a scan line is
    perpendicular to the orbit's inertial direction, ~3 deg off the ground
    track's perpendicular. On a recorded LANCE granule of each, the live
    strip's corners (swathgeom.crossTrack with the sensor) land nearer the
    real corner pixels than the ground-track perpendicular does, and each
    corner's imaging time (scanTime) is the granule's start or end."""
    import urllib.request

    from conftest import load_fixture

    try:
        lib = urllib.request.urlopen(SATELLITE_JS, timeout=20).read().decode()
    except OSError:
        pytest.skip("satellite.js CDN not reachable")
    (tmp_path / "satellite.cjs").write_text(lib)
    omm = load_fixture("celestrak_omm.json")
    fx = load_fixture("cmr_wideswath_umm.json")
    cases = []
    for key, plat, inst in (
        ("modis_nrt", "Terra", "MODIS"),
        ("viirs_nrt", "N20", "VIIRS"),
    ):
        u = fx[key]["umm"]
        pts = u["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]["GPolygons"][0][
            "Boundary"
        ]["Points"][:-1]
        r = u["TemporalExtent"]["RangeDateTime"]
        cases.append(
            {
                "plat": plat,
                "inst": inst,
                "t0": r["BeginningDateTime"],
                "t1": r["EndingDateTime"],
                "corners": [[p["Longitude"], p["Latitude"]] for p in pts],
            }
        )
    script = tmp_path / "scan.mjs"
    script.write_text(
        f"""
import {{ createRequire }} from "module";
globalThis.satellite = createRequire(import.meta.url)({json.dumps(str(tmp_path / "satellite.cjs"))});
const g = await import({json.dumps((SITE / "swathgeom.js").as_uri())});
const omm = {json.dumps({k: omm[k] for k in ("Terra", "N20")})};
const R = 6371.0088, D = Math.PI / 180;
const hav = (a, b) => 2 * R * Math.asin(Math.sqrt(Math.sin((b[1] - a[1]) * D / 2) ** 2 + Math.cos(a[1] * D) * Math.cos(b[1] * D) * Math.sin((b[0] - a[0]) * D / 2) ** 2));
const out = {json.dumps(cases)}.map((c) => {{
  const sat = satellite.json2satrec(omm[c.plat]), sensor = g.sensorOf(c.plat), range = g.swathRange(sensor, c.inst);
  const t0 = Date.parse(c.t0), t1 = Date.parse(c.t1);
  const ends = (s) => [t0, t1].flatMap((t) => {{ const x = g.crossTrack(sat, t, range, s); return [x.a, x.b]; }});
  const err = (s) => Math.max(...c.corners.map((p) => Math.min(...ends(s).map((q) => hav(p, q)))));
  const dt = c.corners.map(([lon, lat]) => {{
    const ca = g.closestApproach(sat, lon, lat, t0 - 600e3, t1 + 600e3, 5), t = g.scanTime(sat, lon, lat, ca.t);
    return Math.min(Math.abs(t - t0), Math.abs(t - t1)) / 1000;
  }});
  return {{ plat: c.plat, sensor, inertial: err(sensor), ground: err(null), dt }};
}});
console.log(JSON.stringify(out));
"""
    )
    res = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    for r in res:
        assert r["sensor"] in ("modis", "viirs")
        assert r["inertial"] < r["ground"], r  # the inertial scan line fits better
        assert r["inertial"] < 70, r  # km, worst corner
        assert max(r["dt"]) < 8, (
            r
        )  # s: a corner is imaged at the granule's start or end


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worldview_preview_of_one_pass_within_its_tile(tmp_path):
    """A VIIRS pass's preview: Worldview's granules of that pass (6-minute
    steps, one more either side), the day's mosaic as the fallback, clipped
    to the tile; MODIS (no granule layer): the day's mosaic, clipped."""
    out = run_node(
        tmp_path,
        """
out.times = h.granuleTimes(["2026-10-13T18:36:00Z", "2026-10-13T18:42:10Z", "2026-10-13T18:36:00Z"]);
const clip = {type: "Polygon", coordinates: [[[-100, 30], [-90, 30], [-92, 40], [-102, 40], [-100, 30]]]};
const box = [-102, 30, -90, 40];
const v = await h.fetchBrowse({kind: "gibs", platform: "N20", date: "2026-10-13", bbox: box, clip, granules: out.times});
out.v = {n: v.georef.urls.length, layer: new URL(v.georef.urls[0]).searchParams.get("LAYERS"),
         time: new URL(v.georef.urls[1]).searchParams.get("TIME"), fallback: new URL(v.georef.fallback).searchParams.get("LAYERS"),
         clip: v.georef.clip === clip, source: v.source};
const m = await h.fetchBrowse({kind: "gibs", platform: "Terra", date: "2026-10-13", bbox: box, clip});
out.m = {urls: m.georef.urls, fallback: m.georef.fallback, layer: new URL(m.georef.url).searchParams.get("LAYERS"), clip: Boolean(m.georef.clip)};
""",
    )
    assert out["times"] == [
        "2026-10-13T18:30:00Z",
        "2026-10-13T18:36:00Z",
        "2026-10-13T18:42:00Z",
        "2026-10-13T18:48:00Z",
    ]
    v = out["v"]
    assert v["n"] == 4 and v["layer"].endswith("_Granule")
    assert v["time"] == "2026-10-13T18:36:00Z"
    assert v["fallback"] == "VIIRS_NOAA20_CorrectedReflectance_TrueColor"
    assert v["clip"] and "this pass's granules (18:30-18:48 UTC)" in v["source"]
    m = out["m"]
    assert m["urls"] is None and m["fallback"] is None and m["clip"]
    assert m["layer"] == "MODIS_Terra_CorrectedReflectance_TrueColor"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_worldview_night_preview(tmp_path):
    """A night pass: the night's thermal mosaic (no granule layer), nothing
    keyed out (the warm sea is dark), and for SNPP/NOAA-20 the Day/Night
    Band as a second channel; MODIS: band 31 alone."""
    out = run_node(
        tmp_path,
        """
const box = [-125, 44, -118, 50];
const lay = (u) => new URL(u).searchParams.get("LAYERS");
const v = await h.fetchBrowse({kind: "gibs", platform: "N20", date: "2026-10-12", bbox: box, night: true});
out.v = {layer: lay(v.georef.url), key: v.georef.key, urls: v.georef.urls ?? null, bands: v.bands.map((b) => [b.label, lay(b.url)])};
const m = await h.fetchBrowse({kind: "gibs", platform: "Aqua", date: "2026-10-12", bbox: box, night: true});
out.m = {layer: lay(m.georef.url), bands: m.bands, time: new URL(m.georef.url).searchParams.get("TIME")};
""",
    )
    v = out["v"]
    assert v["layer"] == "VIIRS_NOAA20_Brightness_Temp_BandI5_Night"
    assert v["key"] is None and v["urls"] is None
    assert [b[1] for b in v["bands"]] == [
        "VIIRS_NOAA20_Brightness_Temp_BandI5_Night",
        "VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance",
    ]
    m = out["m"]
    assert m["layer"] == "MODIS_Aqua_Brightness_Temp_Band31_Night"
    assert m["bands"] is None and m["time"] == "2026-10-12"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_sentinel3_quicklook_is_placed_from_the_orbit(tmp_path):
    """A Sentinel-3 quicklook (rows: scan lines in time order; columns:
    across the track, right edge of the swath first) placed from the orbit:
    on the recorded S3B frames (relative orbit 304, ~80 N, descending), the
    footprint's points map onto the image's edges; s3Browse takes the quicklook from CDSE's download
    host (the catalogue's link redirects there)."""
    import urllib.request

    from conftest import load_fixture

    try:
        lib = urllib.request.urlopen(SATELLITE_JS, timeout=20).read().decode()
    except OSError:
        pytest.skip("satellite.js CDN not reachable")
    (tmp_path / "satellite.cjs").write_text(lib)
    omm = load_fixture("celestrak_omm.json")["S3B"]
    items = [
        v
        for v in load_fixture("cdse_s3_products.json")["value"]
        if "_SL_1_RBT_" in v["Name"] or "_OL_1_EFR_" in v["Name"]
    ]
    script = tmp_path / "s3.mjs"
    script.write_text(
        f"""
import {{ createRequire }} from "module";
globalThis.satellite = createRequire(import.meta.url)({json.dumps(str(tmp_path / "satellite.cjs"))});
const g = await import({json.dumps((SITE / "swathgeom.js").as_uri())});
const h = await import({json.dumps((SITE / "history.js").as_uri())});
const sat = satellite.json2satrec({json.dumps(omm)});
const out = {json.dumps(items)}.map((p) => {{
  const ring = p.GeoFootprint.coordinates[0];
  const m = g.swathImageModel(sat, Date.parse(p.ContentDate.Start), Date.parse(p.ContentDate.End), ring);
  const f = ring.map(([lon, lat]) => m.toFraction(lon, lat));
  const b = h.s3Browse({{ ...p, Assets: [{{ Type: "QUICKLOOK", DownloadLink: "https://catalogue.dataspace.copernicus.eu/odata/v1/Assets(x)/$value" }}] }}, "S3B");
  return {{ name: p.Name, f, url: b.url, kind: b.kind, slstr: b.slstr }};
}});
console.log(JSON.stringify(out));
"""
    )
    res = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    assert len(res) == 3  # one RBT, two EFR (NR and NT)
    for r in res:
        u = [x[0] for x in r["f"]]
        v = [x[1] for x in r["f"]]
        # every footprint point on the image, and the four edges reached
        assert min(u) > -0.02 and max(u) < 1.02 and min(v) > -0.02 and max(v) < 1.02, r[
            "name"
        ]
        assert min(u) < 0.02 and max(u) > 0.98 and min(v) < 0.02 and max(v) > 0.98, r[
            "name"
        ]
        assert r["kind"] == "s3"
        assert r["slstr"] == ("_SL_1_RBT_" in r["name"])
        assert r["url"].startswith("https://download.dataspace.copernicus.eu/")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_night_sentinel3_quicklook_is_turned_north_up(tmp_path):
    """An ascending (night) SLSTR frame: CDSE draws its quicklook north up,
    half a turn from the scan order, and the model places it so: Sicily
    and Lecce land where they are on the image (within ~3 % of it)."""
    import urllib.request

    from conftest import load_fixture

    try:
        lib = urllib.request.urlopen(SATELLITE_JS, timeout=20).read().decode()
    except OSError:
        pytest.skip("satellite.js CDN not reachable")
    (tmp_path / "satellite.cjs").write_text(lib)
    fx = load_fixture("cdse_s3_night_frame.json")
    script = tmp_path / "night.mjs"
    script.write_text(
        f"""
import {{ createRequire }} from "module";
globalThis.satellite = createRequire(import.meta.url)({json.dumps(str(tmp_path / "satellite.cjs"))});
const g = await import({json.dumps((SITE / "swathgeom.js").as_uri())});
const p = {json.dumps(fx["product"])};
const sat = satellite.json2satrec({json.dumps(fx["omm"])});
const ring = p.GeoFootprint.coordinates[0];
const m = g.swathImageModel(sat, Date.parse(p.ContentDate.Start), Date.parse(p.ContentDate.End), ring);
console.log(JSON.stringify({{ sicily: m.toFraction(14.0, 37.55), lecce: m.toFraction(18.17, 40.35), f: ring.map(([x, y]) => m.toFraction(x, y)) }}));
"""
    )
    out = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    assert out["sicily"] == pytest.approx([0.48, 0.50], abs=0.03)
    assert out["lecce"] == pytest.approx([0.73, 0.31], abs=0.03)
    u = [x[0] for x in out["f"]]
    v = [x[1] for x in out["f"]]
    assert min(u) == pytest.approx(0, abs=0.02) and max(u) == pytest.approx(1, abs=0.02)
    assert min(v) == pytest.approx(0, abs=0.02) and max(v) == pytest.approx(1, abs=0.02)
