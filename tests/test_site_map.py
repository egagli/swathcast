"""The map's DOM-free helpers under node: AOIs (aoi.js), times (common.js),
burst-map tiles (footprints.js), catalogue fetches (catalogues.js)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "src" / "swathcast" / "site"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def run_node(tmp_path, body):
    script = tmp_path / "s.mjs"
    imports = "\n".join(
        f"import * as {name} from {json.dumps((SITE / f'{mod}.js').as_uri())};"
        for name, mod in (
            ("a", "aoi"),
            ("c", "common"),
            ("fp", "footprints"),
            ("cat", "catalogues"),
            ("dr", "drape"),
            ("hi", "history"),
        )
    )
    script.write_text(
        f"""{imports}
const out = {{}};
const err = (f) => {{ try {{ f(); return null; }} catch (e) {{ return e.message; }} }};
{body}
console.log(JSON.stringify(out));
"""
    )
    res = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True
    )
    return json.loads(res.stdout)


def test_paste_formats_and_errors(tmp_path):
    out = run_node(
        tmp_path,
        """
out.wkt = a.parseAOI("POLYGON((-124 47.5, -123 47.5, -123 48.1, -124 48.1, -124 47.5))").geometry;
out.lower = a.parseAOI("point(-122.3 47.6)").geometry;
out.box = a.parseAOI("-124,47.5,-123,48.1").geometry;
out.feature = a.parseAOI('{"type":"Feature","geometry":{"type":"Point","coordinates":[10,45]}}').geometry;
out.wrapped = a.parseAOI("POINT(400 10)").geometry;
out.closed = a.parseAOI("POLYGON((0 0, 1 0, 1 1))");
out.swapped = err(() => a.parseAOI("POINT(47.6 -122.3)"));
out.emptyFc = err(() => a.parseAOI('{"type":"FeatureCollection","features":[]}'));
out.line = err(() => a.parseAOI('{"type":"LineString","coordinates":[[0,0],[1,1]]}'));
out.broken = err(() => a.parseAOI("POLYGON((1 2, 3 4"));
out.garbage = err(() => a.parseAOI("hello"));
out.badJson = err(() => a.parseAOI("{bad"));
out.twoCorners = err(() => a.parseAOI("POLYGON((0 0, 1 1, 0 0))"));
""",
    )
    assert out["wkt"]["type"] == "Polygon"
    assert out["lower"] == {"type": "Point", "coordinates": [-122.3, 47.6]}
    assert out["box"]["coordinates"][0][0] == [-124, 47.5]
    assert out["feature"]["coordinates"] == [10, 45]
    assert out["wrapped"]["coordinates"] == [40, 10]
    assert out["closed"]["note"] == "closed the ring"
    assert out["closed"]["geometry"]["coordinates"][0][-1] == [0, 0]
    assert "write longitude first" in out["swapped"].lower()
    assert out["emptyFc"] == "the FeatureCollection has no features"
    assert "LineString cannot be an AOI" in out["line"]
    assert "incomplete" in out["broken"]
    assert "west,south,east,north" in out["garbage"]
    assert "not valid JSON" in out["badJson"]
    assert "3 different corners" in out["twoCorners"]


def test_antimeridian(tmp_path):
    out = run_node(
        tmp_path,
        """
const naive = a.parseAOI("POLYGON((179 -17, -179 -17, -179 -16, 179 -16, 179 -17))");
out.naive = naive;
out.naiveBox = a.aoiBBox(naive.geometry);
out.naiveParts = a.aoiParts(naive.geometry);
out.naiveCentre = a.aoiCenter(naive.geometry);
out.east = a.parseAOI("POLYGON((179 -17, 181 -17, 181 -16, 179 -16, 179 -17))").geometry;
out.box = a.parseAOI("170,-20,-170,-10").geometry;
out.boxBBox = a.aoiBBox(out.box);
const pole = a.parseAOI("POLYGON((-180 85, -90 85, 0 85, 90 85, 180 85, 180 89, -180 89, -180 85))").geometry;
out.pole = pole.type; out.poleBox = a.aoiBBox(pole);
out.link = a.aoiFromParam(a.aoiParam(naive.geometry));
out.linkUnclosed = a.aoiFromParam("0,0;1,0;1,1").coordinates[0].length;
""",
    )
    assert out["naive"]["note"] == "read as crossing 180°"
    assert out["naive"]["geometry"]["type"] == "MultiPolygon"
    assert out["naiveBox"] == [179, -17, 181, -16]
    assert out["naiveParts"] == [[179, -17, 180, -16], [-180, -17, -179, -16]]
    assert abs(out["naiveCentre"][0]) == 180
    assert out["east"] == out["naive"]["geometry"]
    assert out["boxBBox"] == [170, -20, 190, -10]
    assert out["pole"] == "Polygon" and out["poleBox"] == [-180, 85, 180, 89]
    assert out["link"] == out["naive"]["geometry"]
    assert out["linkUnclosed"] == 4


def test_times(tmp_path):
    out = run_node(
        tmp_path,
        """
const d = new Date("2026-10-06T19:21:00Z"), now = new Date("2026-10-06T01:36:00Z").getTime();
out.utc = c.utcText(d); out.sameDay = c.utcText(d, new Date("2026-10-06T00:00:00Z"));
out.ahead = c.rel(d, now); out.ago = c.rel(new Date(now - 3 * 3.6e6), now); out.soon = c.rel(new Date(now + 5e3), now);
out.days = [c.dayLabel(d, new Date(now)), c.dayLabel(new Date("2026-10-07T01:00:00Z"), new Date(now)), c.dayLabel(new Date("2026-10-04T01:00:00Z"), new Date(now))];
""",
    )
    assert out["utc"] == "Tue 6 Oct 19:21 UTC"
    assert out["sameDay"] == "19:21 UTC"
    assert (
        out["ahead"] == "in 17.8 h"
        and out["ago"] == "3.0 h ago"
        and out["soon"] == "in 1 min"
    )
    assert [x["rel"] for x in out["days"]] == ["today", "tomorrow", "2 days ago"]
    assert out["days"][0]["name"] == "Tuesday 6 Oct"


def test_burst_tiles(tmp_path):
    out = run_node(
        tmp_path,
        """
out.point = fp.burstTiles([-122.3, 47.6, -122.3, 47.6]);
out.small = fp.burstTiles([-124, 48, -122, 50]);
out.big = fp.burstTiles([-40, 30, 40, 60]);
out.max = fp.MAX_TILES;
""",
    )
    assert out["point"] == 1 and out["small"] == 1
    assert out["big"] > out["max"]


def test_catalogue_retry(tmp_path):
    out = run_node(
        tmp_path,
        """
let calls = 0;
globalThis.fetch = async () => { calls++; return calls === 1 ? { ok: false, status: 503 } : { ok: true, json: async () => ({ fine: true }) }; };
out.got = await cat.getJSON("https://example.org/x", undefined, { wait: 1 });
out.calls = calls;
calls = 0;
globalThis.fetch = async () => { calls++; return { ok: false, status: 404 }; };
out.notFound = await cat.getJSON("https://example.org/x", undefined, { wait: 1 }).catch((e) => e.message);
out.calls404 = calls;
out.plat = [cat.plat("Sentinel-1C"), cat.plat("LANDSAT_9"), cat.plat("unknown")];
""",
    )
    assert out["got"] == {"fine": True} and out["calls"] == 2
    assert out["notFound"] == "example.org HTTP 404" and out["calls404"] == 1
    assert out["plat"] == ["S1C", "L9", None]


def test_every_site_import_is_exported():
    """A named import from a sibling module that does not export it breaks
    the whole page; check them all without a browser."""
    import re

    missing = []
    for f in sorted(SITE.glob("*.js")):
        src = f.read_text()
        for names, mod in re.findall(
            r'import\s*\{([^}]+)\}\s*from\s*"\./([\w.]+)"', src
        ):
            target = (SITE / mod).read_text()
            for name in [n.strip().split(" as ")[0].strip() for n in names.split(",")]:
                if name and not re.search(
                    rf"export\s+(async\s+)?(function|const|let|class)\s+{name}\b",
                    target,
                ):
                    missing.append(f"{f.name}: {name} from {mod}")
    assert missing == []


def test_drape_reprojects_onto_mercator_rows(tmp_path):
    """Browse images are resampled onto rows regular in Web Mercator; the
    UTM forward and inverse agree, and a lat/lon image's middle row is not
    its middle latitude (the error draping it as it is would make)."""
    out = run_node(
        tmp_path,
        """
let worst = 0;
for (const [lon, lat, z, s] of [[-122.3, 47.6, 10, false], [-71.6, -33.05, 19, true], [15.6, 78.2, 33, false]]) {
  const [x, y] = dr.lonLatToUtm(lon, lat, z, s);
  const [lo, la] = hi.utmToLonLat(x, y, z, s);
  worst = Math.max(worst, Math.hypot((lo - lon) * 111320 * Math.cos(lat * Math.PI / 180), (la - lat) * 110570));
}
out.worst = worst;
const g = dr.mercatorGrid({ kind: "latlon", bbox: [-125, 46.9, -121, 49.9] }, 1000, 1000, 1000, 1000);
out.bbox = g.bbox; out.mid = g.toSource(500, 500); out.top = g.toSource(0, 0);
const utm = { kind: "utm", epsg: 32610, transform: [10, 0, 499980, 0, -10, 5300040], shape: [10980, 10980] };
const cs = dr.imageCorners(utm), px = dr.lonLatToPixel(utm, 343, 343);
out.cornerPx = cs.map(([lon, lat]) => px(lon, lat).map((v) => Math.round(v * 1000) / 1000));
out.widened = hi.widenToImage([[-125, 49.9], [-121, 49.9], [-121, 46.9], [-125, 46.9]], [10, 20, 989, 979], 1000, 1000);
""",
    )
    assert out["worst"] < 0.01  # metres
    assert out["bbox"] == [-125, 46.9, -121, 49.9]
    assert (
        abs(out["mid"][0] - 500.5) < 1e-6 and 6 < 500 - out["mid"][1] < 8
    )  # pixel centres
    assert out["top"][1] < 1
    assert out["cornerPx"] == [[0, 0], [343, 0], [343, 343], [0, 343]]
    (w, n), (e, _), (_, s) = out["widened"][:3]
    assert w < -125 and e > -121 and n > 49.9 and s < 46.9


def test_opera_fit_and_single_channel_previews(tmp_path):
    """An OPERA burst browse (no published grid) is fitted to its footprint:
    its data spans the footprint's UTM box east-west, square pixels, centred
    north-south. A Sentinel-1 channel preview is grey, one asset, rescaled."""
    out = run_node(
        tmp_path,
        """
const ring = [[-123.22137, 47.34044], [-122.57974, 47.41795], [-121.96916, 47.48818], [-122.01451, 47.65312], [-122.68493, 47.57605], [-123.24976, 47.50787], [-123.22137, 47.34044]];
const epsg = dr.utmEpsg(-122.6, 47.5), box = dr.utmBox(epsg, ring);
const w = dr.fitUtmToData(epsg, box, [64, 63, 978, 418], 1024, 465);
const px = dr.lonLatToPixel(w, 1024, 465);
out.epsg = epsg; out.box = box; out.tr = w.transform;
out.west = px(...ring.reduce((a, p) => (p[0] < a[0] ? p : a)));
out.vh = hi.singleBand("sentinel-1-rtc", "S1C_X_rtc", "VH");
out.hh = hi.singleBand("sentinel-1-grd", "S1A_Y", "HH");
""",
    )
    assert out["epsg"] == 32610
    x0, y0, x1, y1 = out["box"]
    s = out["tr"][0]
    assert abs(s - (x1 - x0) / 915) < 1e-9 and out["tr"][4] == -s  # square pixels
    assert (
        abs(out["west"][0] - 64) < 0.01
    )  # the westmost corner at the data's left edge
    assert abs((out["tr"][5] - (63 + 418 + 1) / 2 * s) - (y0 + y1) / 2) < 1e-6
    assert "assets=vh" in out["vh"] and "rescale=0,0.12" in out["vh"]
    assert "colormap_name=gray" in out["vh"]
    assert "assets=hh" in out["hh"] and "rescale=0,600" in out["hh"]
