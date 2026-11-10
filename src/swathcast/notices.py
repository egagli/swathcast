"""Agency notices, fetched automatically (``swathcast notices``, daily).

The Satellites page lists each satellite's curated notices (platforms.json);
this adds the agencies' newest ones as they appear, from:

- **Copernicus Data Space Ecosystem** news RSS (Sentinel unavailability,
  extension, end-of-operations notices): ``dataspace.copernicus.eu/rss.xml``;
- **NASA Earthdata** "Data Alerts and Outages" listing (MODIS, VIIRS,
  NISAR, Landsat and Sentinel alerts, with issued and resolved dates);
- **NOAA OSPO** operational messages (JPSS / S-NPP / NOAA-20 / NOAA-21,
  and EUMETSAT Sentinel-3 relays);
- **USGS EROS** data notifications (Landsat; dated by their URL).

Each item is matched to the satellites (and missions) its title names, with
word-boundary patterns; anything naming none is dropped. A source that
fails is reported in the output and skipped; the others still run.

Items whose title reads like a change of a satellite's status (end of
operations, decommissioning, extension, transfer of duty, data opening...)
and that platforms.json does not cite yet are listed as needing a review:
the ``notices`` workflow opens or updates a ``registry-review`` issue.
"""

from __future__ import annotations

import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

CDSE_RSS = "https://dataspace.copernicus.eu/rss.xml"
EARTHDATA_ALERTS = "https://www.earthdata.nasa.gov/data/alerts-outages"
OSPO_MESSAGES = "https://www.ospo.noaa.gov/operations/messages.html"
USGS_EROS = "https://www.usgs.gov/centers/eros/data-notifications"
UA = "swathcast (+https://github.com/egagli/swathcast)"
KEEP_DAYS = 180

#: platform code -> title pattern (case-insensitive, word-bounded)
PLATFORM_PATTERNS = {
    "S1A": r"sentinel[- ]?1a|\bS-?1A\b",
    "S1B": r"sentinel[- ]?1b|\bS-?1B\b",
    "S1C": r"sentinel[- ]?1c|\bS-?1C\b|S1C/D",
    "S1D": r"sentinel[- ]?1d|\bS-?1D\b|S1C/D",
    "S2A": r"sentinel[- ]?2a|\bS-?2A\b",
    "S2B": r"sentinel[- ]?2b|\bS-?2B\b",
    "S2C": r"sentinel[- ]?2c|\bS-?2C\b",
    "S3A": r"sentinel[- ]?3a|\bS-?3A\b",
    "S3B": r"sentinel[- ]?3b|\bS-?3B\b",
    "S3C": r"sentinel[- ]?3c|\bS-?3C\b",
    "S3D": r"sentinel[- ]?3d|\bS-?3D\b",
    "S2D": r"sentinel[- ]?2d|\bS-?2D\b",
    "S1NG": r"sentinel[- ]?1 ?(?:next[- ]generation|NG)\b|sentinel[- ]?1[ef]\b",
    "L7": r"landsat[- ]?7\b",
    "LNEXT": r"landsat[- ]?next",
    "L8": r"landsat[- ]?8\b|landsat 8 and 9|landsat 8/9",
    "L9": r"landsat[- ]?9\b|landsat 8 and 9|landsat 8/9",
    "NISAR": r"\bnisar\b",
    "Terra": r"\bterra\b",
    "Aqua": r"\baqua\b",
    "SNPP": r"suomi[- ]?npp|\bS-?NPP\b|\bSNPP\b",
    "N20": r"noaa[- ]?20\b|\bJPSS-1\b",
    "N21": r"noaa[- ]?21\b|\bJPSS-2\b",
    "JPSS3": r"\bJPSS-3\b|noaa[- ]?22\b",
    "JPSS4": r"\bJPSS-4\b|noaa[- ]?23\b",
}
#: mission -> pattern for items naming the mission but no single satellite
MISSION_PATTERNS = {
    "s1": r"sentinel[- ]?1(?![a-d0-9])",
    "s2": r"sentinel[- ]?2(?![a-d0-9])",
    "s3": r"sentinel[- ]?3(?![a-d0-9])",
    "landsat": r"\blandsat\b",
    "modis": r"\bmodis\b",
    "viirs": r"\bviirs\b",
}
MISSION_OF = {
    "S1A": "s1", "S1B": "s1", "S1C": "s1", "S1D": "s1", "S1NG": "s1",
    "S2A": "s2", "S2B": "s2", "S2C": "s2", "S2D": "s2",
    "S3A": "s3", "S3B": "s3", "S3C": "s3", "S3D": "s3",
    "L7": "landsat", "L8": "landsat", "L9": "landsat", "LNEXT": "landsat", "NISAR": "nisar",
    "Terra": "modis", "Aqua": "modis", "SNPP": "viirs", "N20": "viirs", "N21": "viirs",
    "JPSS3": "viirs", "JPSS4": "viirs",
}  # fmt: skip
#: a title that may change a satellite's status in platforms.json
LIFECYCLE = re.compile(
    r"end of (?:operations|mission|life|service)|decommission|de-?orbit|cessation|"
    r"retire|passivation|extension campaign|extended|transfer of duty|replac|"
    r"user data opening|data opening|commissioning|now (?:fully )?operational|"
    r"primary satellite|launch(?:ed)?\b|disposal",
    re.I,
)


def match(title: str) -> tuple[list[str], list[str]]:
    """(platform codes, missions) a title names; missions only when no
    satellite of that mission is named."""
    plats = [p for p, pat in PLATFORM_PATTERNS.items() if re.search(pat, title, re.I)]
    named = {MISSION_OF[p] for p in plats}
    missions = sorted(
        named
        | {
            m
            for m, pat in MISSION_PATTERNS.items()
            if m not in named and re.search(pat, title, re.I)
        }
    )
    return plats, missions


def _clean(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x or ""))).strip()


_MONTH = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        1,
    )
}


def us_date(text: str) -> str | None:
    """'Oct. 27, 2026', 'Sept. 5, 2026', 'March 31, 2022' -> '2026-10-27'."""
    m = re.match(r"([A-Za-z]+)\.? (\d{1,2}), (\d{4})", text.strip())
    if not m or m[1][:3].lower() not in _MONTH:
        return None
    return f"{m[3]}-{_MONTH[m[1][:3].lower()]:02d}-{int(m[2]):02d}"


# ------------------------------------------------------------------ parsers
def parse_cdse(xml_text: str) -> list[dict]:
    out = []
    for it in ET.fromstring(xml_text).findall(".//item"):
        title, link = _clean(it.findtext("title")), (it.findtext("link") or "").strip()
        try:
            day = parsedate_to_datetime(it.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            continue
        out.append({"date": day, "title": title, "url": link, "source": "cdse"})
    return out


def parse_earthdata(page: str) -> list[dict]:
    """The alert cards: title, issued and resolved dates, link."""
    hrefs = {}
    for h, x in re.findall(
        r'href="(/data/alerts-outages/[A-Za-z0-9\-\.]+)"[^>]*>(.*?)</a>',
        page,
        flags=re.S,
    ):
        hrefs.setdefault(_clean(x), h)
    text = _clean(re.sub(r"<[^>]+>", " | ", page))
    out = []
    for title, issued, resolved in re.findall(
        r"\| ([^|]{6,200}?) \|[ |]*Issued: \|[ |]*([A-Z][a-z]+\.? \d{1,2}, \d{4}) \|[ |]*(?:Resolved: \|[ |]*([^|]+?) \|)?",
        text,
    ):
        title = title.strip()
        day = us_date(issued)
        if not day or title not in hrefs:
            continue
        item = {
            "date": day,
            "title": title,
            "url": f"https://www.earthdata.nasa.gov{hrefs[title]}",
            "source": "earthdata",
        }
        res = us_date(resolved or "")
        item["resolved"] = res or ((resolved or "").strip() or None)
        out.append(item)
    return out


def parse_ospo(page: str) -> list[dict]:
    """Messages are dated in their file name: MSG_YYYYMMDD_HHMM.html."""
    out = []
    for h, x in re.findall(
        r'<a[^>]+href="([^"]*MSG_\d{8}_\d{4}\.html)"[^>]*>(.*?)</a>', page, flags=re.S
    ):
        d = re.search(r"MSG_(\d{4})(\d{2})(\d{2})_", h)
        url = (
            h
            if h.startswith("http")
            else "https://www.ospo.noaa.gov/" + re.sub(r"^(?:\.\./)+|^/", "", h)
        )
        out.append(
            {
                "date": f"{d[1]}-{d[2]}-{d[3]}",
                "title": _clean(x),
                "url": url,
                "source": "ospo",
            }
        )
    return out


def parse_usgs(page: str) -> list[dict]:
    """EROS notifications are dated in their path: /centers/eros/YYYY-MM-DD-slug."""
    out = []
    for h, day, x in re.findall(
        r'href="(/centers/eros/(\d{4}-\d{2}-\d{2})-[a-z0-9\-]+)"[^>]*>(.*?)</a>',
        page,
        flags=re.S,
    ):
        title = _clean(x)
        if title:
            out.append(
                {
                    "date": day,
                    "title": title,
                    "url": f"https://www.usgs.gov{h}",
                    "source": "usgs",
                }
            )
    return out


SOURCES = {
    "cdse": ("Copernicus Data Space Ecosystem news", CDSE_RSS, parse_cdse),
    "earthdata": (
        "NASA Earthdata alerts and outages",
        EARTHDATA_ALERTS,
        parse_earthdata,
    ),
    "ospo": ("NOAA OSPO operational messages", OSPO_MESSAGES, parse_ospo),
    "usgs": ("USGS EROS data notifications", USGS_EROS, parse_usgs),
}


# ------------------------------------------------------------------- driver
def collect(
    fetch,
    now: datetime | None = None,
    cited: set[str] | None = None,
    previous: dict | None = None,
) -> dict:
    """Fetch every source (``fetch(url) -> text``), keep the items that name
    a tracked satellite or mission from the last KEEP_DAYS, newest first.

    A source that fails keeps its items from ``previous`` (the last report),
    and says since when: NASA Earthdata answers GitHub's runners with 403 and
    USGS often times out, so a run elsewhere (or a good day) is not lost."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    cited = cited or set()
    items, sources = {}, {}
    for key, (label, url, parse) in SOURCES.items():
        try:
            got = parse(fetch(url))
            sources[key] = {
                "label": label,
                "url": url,
                "ok": True,
                "items": len(got),
                "fetched_at": now.isoformat(timespec="seconds"),
            }
        except Exception as exc:  # noqa: BLE001 - one failing source must not stop the rest
            LOGGER.warning("notices: %s failed: %s", key, exc)
            prev = (previous or {}).get("sources", {}).get(key, {})
            last_ok = prev.get("fetched_at") if prev.get("ok") else prev.get("last_ok")
            kept = [
                it for it in (previous or {}).get("items", [])
                if it.get("source") == key and it["date"] >= cutoff
            ]  # fmt: skip
            for it in kept:
                items.setdefault(it["url"], {**it, "cited": it["url"] in cited})
            sources[key] = {
                "label": label,
                "url": url,
                "ok": False,
                "error": str(exc)[:300],
                "last_ok": last_ok,
                "kept": len(kept),
            }
            continue
        for it in got:
            if it["date"] < cutoff:
                continue
            plats, missions = match(it["title"])
            if not plats and not missions:
                continue
            it = {**it, "platforms": plats, "missions": missions}
            it["lifecycle"] = bool(LIFECYCLE.search(it["title"]))
            it["cited"] = it["url"] in cited
            items.setdefault(it["url"], it)
    out = sorted(items.values(), key=lambda x: (x["date"], x["title"]), reverse=True)
    return {
        "fetched_at": now.isoformat(timespec="seconds"),
        "sources": sources,
        "items": out,
    }


def review(report: dict, days: int = 60, now: datetime | None = None) -> list[dict]:
    """Items that may change a satellite's status and platforms.json does
    not cite: recent lifecycle titles naming a satellite."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).date().isoformat()
    return [
        it for it in report["items"]
        if it["lifecycle"] and not it["cited"] and it["platforms"] and it["date"] >= since
    ]  # fmt: skip


def issue_body(items: list[dict], run_url: str = "") -> tuple[str, str]:
    marker = "review: " + ",".join(sorted(it["url"] for it in items))
    lines = [
        f"<!-- {marker} -->",
        "The daily notices job (`swathcast notices`) found agency notices that may change a "
        "satellite's status and that `src/swathcast/site/platforms.json` does not cite yet. "
        "Read each, then update the registry (status, its date and source, the notice) or leave "
        "it if it changes nothing by adding its URL to `acknowledged`; citing or acknowledging a "
        "notice there clears it from this list.",
        "",
        "| Date | Satellites | Notice | Source |",
        "|---|---|---|---|",
    ]
    for it in items:
        lines.append(
            f"| {it['date']} | {', '.join(it['platforms'])} | [{it['title']}]({it['url']}) | {it['source']} |"
        )
    lines += [
        "",
        f"This issue updates itself and closes when the list is empty. {run_url}",
    ]
    return marker, "\n".join(lines) + "\n"


def run(
    out: Path,
    issue: Path | None = None,
    run_url: str = "",
    previous: Path | None = None,
) -> dict:
    import requests

    from swathcast.platforms import registry

    session = requests.Session()
    session.headers["User-Agent"] = UA

    def fetch(url: str) -> str:
        r = session.get(url, timeout=60)
        r.raise_for_status()
        return r.text

    reg = registry()
    cited = {
        n["url"]
        for m in reg["missions"]
        for p in m["platforms"]
        for n in p.get("notices", [])
    }
    cited |= set(reg.get("acknowledged", []))  # reviewed: they change nothing
    prev = None
    if previous and Path(previous).exists():
        try:
            prev = json.loads(Path(previous).read_text())
        except ValueError:
            prev = None
    report = collect(fetch, cited=cited, previous=prev)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n")
    marker, body = issue_body(review(report), run_url)
    if issue:
        Path(issue).write_text(body)
    report["marker"] = marker
    return report
