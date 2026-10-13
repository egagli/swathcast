"""site/colors.js: continuous ramps for the next-acquisition grid."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "src" / "swathcast" / "site"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ramps_hit_their_stops_and_viridis_lightens_monotonically(tmp_path):
    script = tmp_path / "c.mjs"
    script.write_text(
        f"""
import {{ RAMPS, rampColor, hexToOklab, colorForDays, scaleT }} from {json.dumps((SITE / "colors.js").as_uri())};
const out = {{}};
for (const [n, stops] of Object.entries(RAMPS)) {{
  out[n] = {{
    stops, ends: [rampColor(n, 0), rampColor(n, 1)],
    at: stops.map((_, i) => rampColor(n, i / (stops.length - 1))),
    L: Array.from({{ length: 51 }}, (_, i) => hexToOklab(rampColor(n, i / 50))[0]),
  }};
}}
out.scale = [scaleT(0), scaleT(7.5), scaleT(30), scaleT(40), scaleT(-1)];
out.clamp = [colorForDays("greenred", 99), colorForDays("greenred", 30)];
console.log(JSON.stringify(out));
"""
    )
    out = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    for name in ("greenred", "cvd"):
        assert out[name]["at"] == out[name]["stops"]  # passes through every stop
    assert out["greenred"]["ends"] == ["#00441b", "#67001f"]  # dark green -> dark red
    assert out["cvd"]["ends"] == ["#440154", "#fde725"]  # dark (soon) -> light
    lum = out["cvd"]["L"]
    assert all(b > a for a, b in zip(lum[:-1], lum[1:], strict=True))
    assert out["scale"] == [0, 0.5, 1, 1, 0]
    assert out["clamp"][0] == out["clamp"][1]
