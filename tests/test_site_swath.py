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
