"""Minimal NASA CMR granule search with complete paging.

Public CMR search needs no credentials. Every search pages to the end with
the ``CMR-Search-After`` header; there is no "first N results" anywhere.
"""

from __future__ import annotations

import logging

import requests

from swathcast.sources.base import SourceError

LOGGER = logging.getLogger(__name__)

CMR_GRANULES_UMM = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
PAGE_SIZE = 2000


def search_umm(
    session: requests.Session,
    params: list[tuple[str, str]],
    *,
    max_items: int = 200_000,
) -> list[dict]:
    """POST a granule search and return every ``items`` entry (all pages)."""
    items: list[dict] = []
    headers: dict[str, str] = {}
    body = [*params, ("page_size", str(PAGE_SIZE))]
    while True:
        response = session.post(
            CMR_GRANULES_UMM, data=body, headers=headers, timeout=180
        )
        if not response.ok:
            raise SourceError(f"CMR HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        page = payload.get("items", [])
        items.extend(page)
        hits = int(response.headers.get("CMR-Hits", payload.get("hits", 0)))
        after = response.headers.get("CMR-Search-After")
        if not page or len(items) >= hits or not after:
            break
        if len(items) > max_items:
            raise SourceError(f"CMR search exceeded {max_items} granules")
        headers = {"CMR-Search-After": after}
    return items


def provider_insert_date(umm: dict) -> str | None:
    for entry in umm.get("ProviderDates", []) or []:
        if entry.get("Type") == "Insert":
            return entry.get("Date")
    return None


def additional_attribute(umm: dict, name: str) -> list[str]:
    for attr in umm.get("AdditionalAttributes", []) or []:
        if attr.get("Name") == name:
            return list(attr.get("Values") or [])
    return []
