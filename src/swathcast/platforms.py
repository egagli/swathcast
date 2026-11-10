"""The platform registry: each satellite's status, dates and official notices.

One file, ``site/platforms.json``, read by the schedule job (what to predict
and project), the satellite check, the hindcast and the site (the Satellites
page; retired platforms greyed out on the map). It is curated by hand from
the agencies' own notices, each entry with its sources and the date it was
last reviewed; the live part of the Satellites page (newest data, the
hourly check) comes from ``satellites.json``.

A *retired* platform (``status.state`` in :data:`RETIRED`) is not predicted,
planned or projected; its past acquisitions and delivery history stay.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).parent / "site" / "platforms.json"
#: states whose platform delivers no new data
RETIRED = frozenset({"retired", "decommissioned", "data_ended"})
STATES = (
    frozenset(
        {"operational", "extended", "drifting", "degraded", "commissioning", "other"}
    )
    | RETIRED
)


@cache
def registry(path: Path = REGISTRY_PATH) -> dict:
    return json.loads(Path(path).read_text())


def platforms(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    """{code: entry} over every mission (entry has its mission's key)."""
    out = {}
    for m in registry(path)["missions"]:
        for p in m["platforms"]:
            out[p["code"]] = {**p, "mission": m["key"]}
    return out


def retired(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    """{code: entry} of the platforms that deliver no new data."""
    return {c: p for c, p in platforms(path).items() if p["status"]["state"] in RETIRED}


def is_retired(code: str, path: Path = REGISTRY_PATH) -> bool:
    return code in retired(path)


def retired_note(code: str, path: Path = REGISTRY_PATH) -> str:
    """ "decommissioned: data ended 2026-11-02 (NOAA)" (or "" if active)."""
    p = retired(path).get(code)
    if not p:
        return ""
    s = p["status"]
    since = f" {s['since']}" if s.get("since") else ""
    by = f" ({s['by']})" if s.get("by") else ""
    what = "data ended" if s["state"] in ("decommissioned", "data_ended") else "retired"
    return f"{s['state'].replace('_', ' ')}: {what}{since}{by}"


def check(path: Path = REGISTRY_PATH) -> list[str]:
    """Problems with the registry (the site contract and the tests run it)."""
    import re

    problems = []
    day = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    try:
        reg = registry(path)
    except (OSError, ValueError) as exc:
        return [f"platforms.json unreadable: {exc}"]
    if not day.match(str(reg.get("reviewed", ""))):
        problems.append("platforms.json has no reviewed date")
    seen = set()
    for m in reg.get("missions", []):
        for p in m.get("platforms", []):
            code = p.get("code")
            if code in seen:
                problems.append(f"platforms.json: {code} twice")
            seen.add(code)
            s = p.get("status", {})
            if s.get("state") not in STATES:
                problems.append(
                    f"platforms.json {code}: unknown state {s.get('state')!r}"
                )
            if not s.get("source"):
                problems.append(f"platforms.json {code}: status without a source")
            for key in ("launch",):
                d = (p.get(key) or {}).get("date")
                if d and not day.match(d):
                    problems.append(f"platforms.json {code}: bad {key} date {d!r}")
            if s.get("since") and not day.match(s["since"]):
                problems.append(
                    f"platforms.json {code}: bad status date {s['since']!r}"
                )
            for n in p.get("notices", []):
                if not (
                    day.match(str(n.get("date", "")))
                    and str(n.get("url", "")).startswith("https://")
                ):
                    problems.append(
                        f"platforms.json {code}: notice without a date or https URL"
                    )
    return problems
