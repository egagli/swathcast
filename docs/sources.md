# Sources: verified field notes

Everything here was checked against the live APIs, not taken from documentation. Each entry says
**what was verified, how, and when**, and what is still unknown. Where a timestamp's meaning could not
be confirmed, the source falls back to first-seen (which every source uses as the primary measure
anyway; see [methodology.md](methodology.md)).

Unless an entry gives another date, the checks below were run on **2026-09-29** (00:00–01:00 UTC)
with `curl` and the package's own source classes. The recorded responses are in `tests/fixtures/` (re-record with
`pixi run python tests/record_fixtures.py`); the live smoke tests are `pixi run test-live`.

## Constellation (as seen in the catalogues)

- CDSE IW GRDH products sensed 2026-09-27: **S1C 459, S1D 529, no S1A** (each also has a `_COG` twin).
- ASF's `SENTINEL-1A_DP_GRD_HIGH` collection's newest granule was sensed **2026-06-29**; S1C and S1D
  collections are current. This matches S1A's operational end in June 2026.
- Nothing in this repo hard-codes platforms: the platform is read from each product name (`S1C_…`).

## `cdse_grd`, `cdse_slc`: ESA Copernicus Data Space Ecosystem (reference catalog)

- **Endpoint**: `https://catalogue.dataspace.copernicus.eu/odata/v1/Products`, anonymous.
- **Listing query** (verified to return IW products; `$count` checks by hand):
  `Collection/Name eq 'SENTINEL-1' and PublicationDate gt … and PublicationDate le … and
  (contains(Name,'_IW_GRDH_1S') or contains(Name,'_IW_SLC__1S')) and not endswith(Name,'_COG.SAFE')`,
  `$expand=Attributes`, `$orderby=PublicationDate asc`, `$top=1000`, following `@odata.nextLink` to
  the end.
- **Volume**: 2026-09-28 publications: 987 IW GRDH, 992 IW SLC, 973 IW GRDH `_COG`. Publication is
  bursty (the hour 22:00–23:00 had no IW products at all), which is why the listing uses a time window
  plus overlap and never "latest N".
- **Identifiers**: `Name` is the ESA product name + `.SAFE`; the stored `granule_id` drops `.SAFE`.
  The last 4 hex characters are a product CRC: the `_COG` variant of the same slice has a different
  CRC (e.g. SAFE `…_55E1`, COG `…_7ED4`).
- **Fields used**: `ContentDate/Start|End` (sensing), `PublicationDate`, and the attributes
  `relativeOrbitNumber`, `orbitDirection`, `startTimeFromAscendingNode` (milliseconds; divided by
  1000), `polarisationChannels`, `timeliness` (`NRT-3h` or `Fast-24h`), `GeoFootprint`.
- **Timestamps on one product** (`S1C_IW_GRDH_1SDV_20260925T193709_…_55E1`): sensing 19:37:09,
  `processingDate` 21:20:58, `OriginDate` 21:27:14, `PublicationDate` 21:29:54. `ModificationDate`
  equalled `PublicationDate` on the products inspected.
- **`PublicationDate` semantics: not yet validated.** It is the natural candidate for "listed in the
  catalogue", and the listing itself filters on it, so a product whose `PublicationDate` precedes its
  actual indexing would be caught by the 3 h overlap. Whether it can precede visibility by more than
  that is exactly what the first-seen comparison below will show.
- **Paging cap, verified 2026-09-29: CDSE OData silently stops after about 11,000 rows.** A
  14-day query that `$count` puts at 26,662 products returned 11,000 (11 pages) and then no
  `@odata.nextLink`, with no error. The listing therefore counts first, bisects any window with more
  than 5,000 products, and raises if it receives fewer rows than the count
  (`tests/test_sources.py`). Hourly windows are a few hundred products, so steady-state polling was
  never affected; a multi-day outage recovery would have been.
- **`$count` runs ahead of the listing for just-published products, seen 2026-10-04.** The 14:18 UTC
  run counted 356 products and listed 344; re-run later, the same window listed all 356, and about
  10 of them had been published in the 3 minutes before the window's end. The poller failed on this
  intermittently (2026-10-03 and 2026-10-04). A short listing is now accepted only when a second
  count shows that every product published more than 15 minutes before the window's end was listed;
  the rest are listed by the next run, whose window starts 3 h before this one ended. Anything
  missing earlier in the window still raises (`tests/test_sources.py`).
- **Latency shape seen in the listing** (`PublicationDate − ContentDate/Start`, 5,854 IW GRD+SLC
  products published 2026-09-26..28): median about 2 h, p95 about 6.6 h, a handful at 25 h and two at
  71 h. The poller therefore admits new rows up to 14 days after sensing.
- **Why CDSE is the reference**: it is the producer's own catalogue; in these checks it was complete
  for both platforms and published within hours, while ASF took days (below). Planetary Computer is
  also fast for GRD but ingests CDSE's `_COG` products, so it is downstream of CDSE by construction.

## `asf_grd`, `asf_slc`: ASF DAAC via NASA CMR

- **Endpoint**: `POST https://cmr.earthdata.nasa.gov/search/granules.umm_json`, anonymous (no
  Earthdata login needed for search), `provider=ASF`, `readable_granule_name[]=<ESA name>` (batches of
  200), `page_size=2000`, paged with `CMR-Search-After`.
- **Identifier matching**: verified that `readable_granule_name`, `producer_granule_id` and a
  `granule_ur` pattern each find the scene by its ESA name. The `GranuleUR` is the ESA name +
  `-GRD_HD` (GRD) or `-SLC` (SLC). The same name **also matches a `-METADATA_GRD_HD` /
  `-METADATA_SLC` granule** in the `…_META_…` collections; those are ignored (only the exact product
  suffix counts).
- **Collections** (not used for the lookup, listed for reference): `SENTINEL-1C_DP_GRD_HIGH`
  (C3486566209-ASF), `SENTINEL-1D_DP_GRD_HIGH` (C4163054563-ASF), `SENTINEL-1C_SLC` (C3470873558-ASF),
  `SENTINEL-1D_SLC` (C4175278193-ASF).
- **`ProviderDates[Insert]` is not a first-availability time. Verified:** for
  `S1C_IW_GRDH_1SDV_20260925T193709_…_55E1-GRD_HD`, three queries within about 11 minutes returned
  three different `Insert` dates: **23:59:42**, then **00:03:06**, then **00:10:53** (2026-09-28/29
  UTC), with `Insert` always equal to `Update` and to the `meta.revision-date`. The field records the
  latest (re-)ingest. The tracker stores both the earliest and the latest value it ever saw
  (`provider_timestamp_utc`, `provider_timestamp_latest_utc`), and the dashboard reports how often it
  changed.
- **ASF was days behind CDSE on 2026-09-29.** The newest S1C
  GRD at ASF had been *sensed* 2026-09-25 and inserted 2026-09-28 23:59: about **76 h** for a
  product CDSE published about 2 h after sensing. The poller's first run found **0 of 986** GRDs and
  **0 of 989** SLCs published by CDSE in the previous 24 h. Whether this is a backlog or the steady
  state is what the curves show.
- ASF's `PROCESSING_DATE` attribute on that granule (2026-09-28T23:59:29) is also an ASF-side
  time, unrelated to ESA's processing (21:20:58).

## `pc_grd`, `pc_rtc`: Microsoft Planetary Computer STAC

- **Endpoint**: `POST https://planetarycomputer.microsoft.com/api/stac/v1/search` with
  `collections`, `ids` (batches of 100), `limit`, and `fields` to return only `id` and
  `properties.s1:product_identifier`; `next` links followed.
- **Identifiers**: `sentinel-1-grd` item id = ESA name without the CRC
  (`S1C_IW_GRDH_1SDV_20260925T193709_20260925T193733_009608_0131FB`); `sentinel-1-rtc` item id = the
  same + `_rtc`. `s1:product_identifier` carries a full ESA name, and **it is the `_COG` product's
  CRC** (`…_7ED4`, not the SAFE's `…_55E1`): PC ingests CDSE's COG products. Matching is therefore
  on the CRC-free id, and a CRC mismatch is expected, not an error (it is kept in `notes` for
  inspection).
- **No publication timestamp exists in the STAC item.** Checked: top-level keys are `id, bbox, type,
  links, assets, geometry, collection, properties, stac_extensions, stac_version`; there is no
  `created`, `updated` or `published` in properties or assets. The only time-like property besides
  sensing is **`s1:processing_datetime`, which is ESA's processing time** (21:25:34 for the COG
  above, before CDSE even published the SAFE). Using it as "available at PC" would make GRD and RTC
  "latencies" identical. **PC sources are first-seen only.**
- **Candidate cross-check, not implemented**: the Azure blob behind an asset reports
  `x-ms-creation-time` / `Last-Modified` (22:21:36 for the granule above, versus CDSE COG publication
  at 21:53:46) via a HEAD request with a SAS token. That is upload time, not STAC visibility, and costs
  one signed request per granule, so it is left as an idea.
- **First run**: of 986 GRDs CDSE published in the previous 24 h, PC GRD already had 863 and PC RTC
  564.

## `opera_rtc`, `opera_cslc`: OPERA RTC-S1 and CSLC-S1 at ASF DAAC via CMR

- **Collections**: `OPERA_L2_RTC-S1_V1` (C2777436413-ASF, 74.7 M granules) and
  `OPERA_L2_CSLC-S1_V1` (C2777443834-ASF, 8.5 M), both **provider ASF**, not LP DAAC.
- **Burst → SLC mapping, verified**: every OPERA granule's UMM has `InputGranules` naming the parent
  SLC exactly as CDSE names it (e.g. `S1C_IW_SLC__1SDH_20260925T094951_20260925T095018_009602_0131C1_4B01`),
  and `AdditionalAttributes/OPERA_BURST_ID` (e.g. `T054_114650_IW2`). The burst ID's track matches the
  SLC's relative orbit.
- **Lookup strategy**: CMR cannot search by `InputGranules`, so the source reads the **complete CMR
  update stream** (`updated_since=<cursor − 3 h>`, `temporal` limited to the last 21 days of sensing,
  paged to the end) and maps every burst to its SLC. The cursor is saved with the table and only
  advanced after a complete read. Volume: 576 RTC granules updated in the hour to 2026-09-29 00:05
  (umm_json about 10 MB, 2.8 s); 8,317 in the preceding 24 h. At ~18 kB per UMM record this is
  about 150–250 MB/day of download for RTC, acceptable for an hourly job.
- **`ProviderDates[Insert]`** is recorded per burst (minimum over bursts); like ASF's, it is not
  validated. On one burst, `Insert` (23:05:14) was 17 min after `ProductionDateTime` (22:48:05).
- **Observed latency at first look**: the 576 RTC bursts inserted in that hour were sensed 85–97 h
  earlier (about 4 days). **No CSLC-S1 granule was inserted in the 24 h to 2026-09-29 00:05 UTC**; the
  newest CSLC was sensed 2026-09-24 and inserted 2026-09-25. CSLC production looks episodic; the
  timeout will capture it.

## Sentinel-1 burst map (for the map's footprints, not a latency source)

- **Collection**: `Sentinel-1_Burst_Map` (C2450786986-ASF), 2.8 M granules, one per burst of the ESA
  burst grid (IW and EW). Queried from the browser with `bounding_box` and
  `granule_ur[]=*_IW*` (pattern), paged with `CMR-Search-After`. `Access-Control-Allow-Origin: *`
  (checked 2026-09-29).
- **Fields**: `GranuleUR` `S1_013_026564_IW2-BURSTMAP` (track, relative burst ID, subswath); UMM
  attributes `BURST_ID_FULL`, `PATH_NUMBER`, `SUBSWATH_NAME`, `ASCENDING_DESCENDING`,
  `TIME_FROM_ANX_SEC`; polygon in `SpatialExtent`. The lean `granules.json` format has only the title
  and polygon, which is enough because `TIME_FROM_ANX_SEC` follows from the ID (formula and
  per-subswath constants in [methodology](methodology.md#sentinel-1-burst-timing); fixture
  `tests/fixtures/burst_map_attrs.json`).

## Other missions (latency sources)

- **Bhoonidhi** (ISRO NRSC, added 2026-10-01; checked with a login from a GitHub runner the same
  day): `POST https://bhoonidhi-api.nrsc.gov.in/auth/token` with `{"userId", "password",
  "grant_type": "password"}` → `access_token` (Bearer), `refresh_token`; `POST /auth/logout` with the
  refresh token; `POST /data/search` with STAC search JSON (`collections`, `datetime`, `ids`, `limit`
  ≤ 500, CQL2 `filter`), paged by `links` `rel: next` (method POST; the next body adds a CQL2
  `IsDeleted = 'N'` filter). 403 "Max sessions already active", 429 on too many token requests; an
  empty search body is a 502 (APISIX gateway). A search over 10 days took ~30 s for 500 items, and the
  first hourly poll got repeated 500s on such a search, so the poller asks one sensing day at a time
  (~70 RSLC a day). NISAR collections: `NISAR_SSAR_RSLC`, `_GSLC`, `_GCOV`, `_RIFG`, `_RUNW`, `_GUNW`,
  `_ROFF`, `_GOFF`, plus `NISAR_SSAR-Beta_RSLC`, `_GSLC`, `_GCOV`; the RSLC collection's temporal
  extent starts 2025-10-03. Names: `NISAR_S1_PR_RSLC_<cycle>_<track>_<A|D>_<frame>_...` and
  `NISAR_S2_PR_GCOV_...` with the same tail. Properties include `Track`, `Frame`, `Node`
  (`Descending`), `Polarization` (`CP`), `Coverage` (`F`/`P`), `CycleNumber`, `RCID`, `Mode`,
  `Imaging_Orbit`, `SceneCenterLon/Lat`, `GenerationTime` (UTC, no zone) and `updated`
  (`YYYY-MM-DD HH:MM:SS`, India time, no zone). One item: sensed 12:44:19 UTC, `GenerationTime`
  17:04:17, `updated` 22:56:03 = 17:26:03 UTC; GCOV `updated` 13 min after the RSLC's. S-band
  products systematically since cycle 25 (2026-07-08), per
  [ISRO](https://www.isro.gov.in/NISARS_Band_SAR_Data_Products_Release.html). From this repository's
  development container the API host resets the TLS connection; from a GitHub runner it answers.

- **CDSE Sentinel-2**: `Collection/Name eq 'SENTINEL-2'` plus a `productType` attribute filter
  (`S2MSI1C`, `S2MSI2A`) and a `PublicationDate` window. Published on 2026-09-28: L1C 14,994, L2A
  15,405 (all baseline N0513). `ContentDate/Start` = `End` = the datatake start. `PublicationDate` −
  sensing: L1C median 3.72 h, L2A 4.49 h. ~450 a day per level share platform, sensing time and tile
  but differ in the processing time; both are real products, so the key is the full name.
- **Earth Search** (`earth-search.aws.element84.com/v1`): S2 ids (`S2C_13XDB_20260929_0_L2A`) are not
  derivable from the ESA name; look up with `query: {"s2:product_uri": {"in": [...names.SAFE]}}` (200 per
  request). `created` = `updated`; 15,228 of 15,405 L2A of 2026-09-28 present, `created` − CDSE
  `PublicationDate` median 0.44 h. It appears to ignore the `fields` extension (full items returned).
  `landsat-c2-l2` ids = USGS L2 id minus processing date and `_SR`; all 653 L9 L2 scenes of one window
  present, mirroring USGS within about a minute.
- **Planetary Computer** `sentinel-2-l2a`: id = ESA name without `_Nxxxx`; no `created`, so first-seen
  only; 100 ids per request (200 gave 504s). It lacked every L2A product CDSE published on 2026-09-28
  00:00–04:30 UTC (2,338), still missing 42 h later. `landsat-c2-l2`: same ids as Earth Search, has
  `created`, 521 of 653 present in the check.
- **USGS STAC** (`landsatlook.usgs.gov/stac-server`): a `created` query works on `landsat-c2l1` but
  returns nothing on `landsat-c2l2-sr` (while sorting on `properties.created` works), so both are
  listed by sorting newest first and paging back to the window start. About 800 L1 and 650 L2 scenes
  a day (Landsat 9); see the Landsat 8 note in the methodology.
- **HLS** (LP DAAC, CMR): HLSS30 v2.0 `C2021957295-LPCLOUD`, HLSL30 v2.0 `C2021957657-LPCLOUD`. Batch
  lookup with repeated `attribute[]=string,PRODUCT_URI,<name>.SAFE` (or `LANDSAT_PRODUCT_ID,<L1 id>`)
  and `options[attribute][or]=true`; `options[attribute][pattern]` is rejected (400). `updated_since`
  and `revision_date` include re-revisions of old granules (55,430 S30 "updated" on one day), so they
  are not a first-publication stream. ProviderDates Insert − sensing (2026-09-28): S30 median 40.2 h,
  L30 44.2 h. In checks, 13 of 36 sampled L1C tiles and 43 of 100 L9 L1 scenes had HLS granules.
- **NISAR** (ASF, CMR, PROVISIONAL_V1): L0B RRSD `C2853089814-ASF`, L1 RSLC `C2853145197-ASF`, L2 GCOV
  `C2854338529-ASF` (also GSLC, GUNW and others, not tracked). `created_at` windows work (106 RSLC,
  24 L0B in a two-hour window). Names: `NISAR_L1_PR_RSLC_<cycle>_<track>_<A|D>_<frame>_<mode>_<pol>_A_<start>_<end>_…`;
  the GCOV of a frame has the same name after `NISAR_L2_PR_GCOV_`, and each RSLC's `InputGranules` is
  its L0B. ProviderDates Insert − start (2026-09-28): L0B median 6.5 h, RSLC 44.2 h, GCOV 45.9 h.

## Acquisition plans (for the map, not latency sources)

- **ESA acquisition-plans pages** (`sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-{1,2}/acquisition-plans`):
  links of the form `/documents/d/sentinel/s1c_mp_user_<start>_<end>` (S1) and
  `s2b_mp_acq__kml_<start>_<end>` (S2), start/end in the name. On 2026-09-29 the S1 page linked 214
  files back to S1A in February (18 still covering the future) and filed one S1D plan under the
  Sentinel-1C heading. S1 placemark fields: `SatelliteId, DatatakeId, Mode, Swath, Polarisation,
  ObservationTimeStart/Stop, ObservationDuration, OrbitAbsolute, OrbitRelative` (polarizations seen:
  DV, DH, SH, SV); S2: `ID, Mode, ObservationTimeStart/Stop, ObservationDuration, OrbitAbsolute,
  OrbitRelative, Scenes` (modes NOBS, VIC, DARK-O, RAW, SUN).
- **USGS Landsat** (`landsat.usgs.gov/sites/default/files/landsat_acq/assets/json/`): `cycles.json`
  (cycle day of each date of a 16-day cycle, keyed from 1970-01-01), `cycle_path_row.json` (per
  mission and cycle day: `{path, rows: [...]}`, rows 1–248 including night-side rows). WRS-2 outlines
  (57,784) from the LandsatLook map service `nimbus.cr.usgs.gov/arcgis/rest/services/LLook_Outlines/MapServer/1`.
  USGS STAC (`landsatlook.usgs.gov/stac-server`, `landsat-c2l1`) has scene `datetime` (scene centre)
  and `created`; on 2026-09-26/27 it had no/few Landsat 8 scenes (0 and 114) against ~780 L9 a day.
- **NASA NISAR** (`science.nasa.gov/mission/nisar/data/`) links the observation plan
  (`NISAR_ROP358_TFDB_ObservationPlan_CY2026-20260305.kmz`, 17 MB) and a released-data KMZ. Placemarks
  `T<track>_F<frame>` with an HTML table of dates and radar modes. CMR has the NISAR science
  collections (e.g. L1 RSLC `C2853145197-ASF`; names `NISAR_L1_PR_RSLC_<cycle>_<track>_<A|D>_<frame>_<mode>_<pol>_…`),
  searchable anonymously; downloads redirect to Earthdata login.

## Sentinel-2 tiling grid and tile sensing times (for the map, not a latency source)

- ESA's tiling grid KML is served at `sentinels.copernicus.eu/documents/247904/1955685/S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.kml`
  (108,817,408 bytes, 56,686 placemarks, 2026-09-29). Each placemark's description carries `TILE_ID`,
  `EPSG`, `UTM_WKT` and an unsplit `LL_WKT` quadrilateral; the geometry itself is split at ±180 for the
  298 antimeridian tiles. The same file is mirrored on the HLS site.
- Planetary Computer `sentinel-2-l2a`: `datetime` is the datatake start (identical for every tile of a
  datatake); the per-tile time is `SENSING_TIME` in the `granule-metadata` asset (`MTD_TL.xml`, needs a
  SAS token). `sat:relative_orbit` and `s2:mgrs_tile` are present.

## CelesTrak orbital elements (for the map's live satellite dots, not a latency source)

- GP API, `gp.php?CATNR=<norad>&FORMAT=json` (OMM JSON), one request per satellite, refreshed by the
  poll job at most every 6 h (`swathcast/tle.py`). NORAD numbers as listed by CelesTrak on
  2026-09-29: S1A 39634, S1C 62261, S1D 66315, S2A 40697, S2B 42063, S2C 60989, L8 39084, L9 49260,
  NISAR 65053 (COSPAR 2025-163A). The map only shows platforms present in the schedule.
- From this development sandbox CelesTrak often reset the connection (~1 in 3 requests, through the
  sandbox's egress proxy); the fetcher retries 4 times and keeps a satellite's previous elements if
  all fail. Not yet observed from GitHub Actions.

## Not included

- **Google Earth Engine `COPERNICUS/S1_GRD`**: left out. There is no service account in this repo's
  secrets, and no ingest-time asset property has been confirmed (`system:version` is undocumented as
  an ingest time). Nothing about GEE is estimated or displayed.
- **OPERA DSWx-S1, DIST-S1**: phase 2; not started.
- **`_COG` GRDs at CDSE**: excluded from the universe; could be tracked as a separate source
  (`cdse_grd_cog`) from the same listing if wanted.

## First-seen vs provider timestamps

The dashboard's "Provider timestamps vs first seen" table is computed from all included sightings and
is the live version of this section. The first numbers from the local test runs are recorded in the
[milestone log](#milestone-log).

## Milestone log

- **2026-09-29 00:22 UTC**, first poll (local; the first Actions poll at 00:46 matched it): 1,975
  CDSE products listed from the 24 h bootstrap window; at the first check PC GRD had 863/986, PC RTC
  564/986, ASF GRD 0/986, ASF SLC 0/989, OPERA RTC 0/989 (its 2,235 stream items all belonged to older
  SLCs outside the universe), OPERA CSLC 0 (empty stream).
- **2026-09-29 01:23 UTC**, second poll, first steady-state evidence on CDSE `PublicationDate`: 42 new
  products; `first_seen − PublicationDate` ranged **0.7–53 min** (median 10 min), all inside one poll
  interval, and no product surfaced with a `PublicationDate` earlier than the previous run. So far
  consistent with `PublicationDate` being the time a product became listable. Not yet enough to call it
  validated (one hour, 42 products); the dashboard's provider-vs-first-seen table is the running test.
- Tracking-table audit after both runs: 2,017 granules, 8,068 rows, 0 missing, 0 duplicate, 0 invalid.
- **Incident, 2026-09-29 10:34 → 16:56 UTC: no data saved.** The first OPERA RTC burst match crashed
  the poller (a missing `items` value read as NaN) outside the per-source error isolation, so every
  hourly run failed without saving. The fix also isolates each source's result
  handling (see `tests/test_tracker.py`). Effect on the statistics, by the existing rules: CDSE
  products published during the gap entered tracking late and are excluded as *not prompt*; sightings
  whose previous negative check was before the gap are excluded as *after a poller gap*. Nothing was
  lost from the table; the recovery run found ASF GRD/SLC ~1,000 each and the first 88 OPERA RTC SLCs
  (14–34 bursts each). Pages was not yet enabled, so no banner was shown; the red `poll` runs were the
  only signal until the watchdog's weekly check.

---

**Status & next steps** (last updated 2026-09-29): every field and identifier above was checked
against the live APIs on 2026-09-29. Unvalidated: CDSE `PublicationDate` as a visibility time (to be
judged from first-seen data). Re-check when a new platform appears, when ASF latency changes by an
order of magnitude, or if a provider adds a publication timestamp.
