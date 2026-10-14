# Data sources, by mission

Where every number on the site comes from. Three uses:

- **Planned**: upcoming acquisitions (the map's colours, the "Upcoming" list in a footprint's popup).
  Built once a day by the `schedule` job (`plans.py`, `schedule.py`), up to 30 days ahead where the
  plan reaches that far: on 2026-10-01 Sentinel-1 to 21–22 Oct and Sentinel-2 to 12–19 Oct (ESA
  publishes no further), Landsat and NISAR all 30 days. Nothing past a plan's end is extrapolated
  ([methodology](methodology.md#horizon-and-gaps)). MODIS, VIIRS and Sentinel-3 have no plans (they
  image on every orbit): their passes are predicted from the orbit for all 30 days
  ([methodology](methodology.md#modis-viirs-and-sentinel-3-timing)).
- **History**: a footprint's recent acquisitions (the popup's "Recent acquisitions", last 90 days).
  Read live in the browser from the catalogue (`site/history.js`); only products that exist.
- **Latency**: how long until a product is downloadable (the Delivery page, "likely downloadable").
  Measured by the hourly `poll` job (`tracker.py`, `sources/`), never taken from documentation.

Details and validation: [methodology](methodology.md) (timing, footprints, polarizations) and
[sources](sources.md) (each API's quirks).

## At a glance

| Mission | Planned from | History from | Latency measured at |
|---|---|---|---|
| Sentinel-1 | ESA acquisition-plan KMLs; burst times from orbit timing | ASF (burst catalogue; Level-0 for EW/SM) | CDSE, ASF, Planetary Computer, OPERA (ASF) |
| Sentinel-2 | ESA acquisition-plan KMLs; tile times from orbit (SGP4) | Earth Search (L1C) | CDSE, Earth Search, Planetary Computer, HLS S30 |
| Landsat 8/9 | USGS acquisition calendar + WRS-2; scene times from orbit (SGP4) | USGS Level-1 (this site's `landsat_l1/<path>.parquet`, from the poller's listing of USGS STAC; night scenes too) + Earth Search (Collection 2 Level-2) | USGS STAC, Earth Search, Planetary Computer, HLS L30 |
| NISAR L-band | NASA observation plan (KMZ); frame times from orbit (SGP4) | NASA CMR (ASF RSLC) | ASF via CMR (L0B, RSLC, GCOV) |
| NISAR S-band | NASA observation plan (KMZ), S-band modes | Bhoonidhi RSLC (this site's `nisar_s/<track>.parquet`, from the poller's listing; Bhoonidhi needs a login) | ISRO Bhoonidhi (RSLC, GCOV) |
| MODIS (Terra, Aqua) | no plan: orbit (SGP4) on the sinusoidal tiles | NASA CMR (LAADS Level-1B + LANCE NRT) | LANCE, LAADS, NSIDC |
| VIIRS (Suomi NPP, NOAA-20, NOAA-21) | no plan: orbit (SGP4) on the sinusoidal tiles | NASA CMR (LAADS Level-1B + LANCE NRT) | LANCE, LAADS, NSIDC |
| Sentinel-3 (OLCI, SLSTR) | no plan: orbit (SGP4) on the sinusoidal tiles, passes labelled with their frame | CDSE (OLCI EFR, SLSTR RBT) | CDSE, EUMETSAT, Planetary Computer |

## Sentinel-1

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| [ESA Sentinel-1 acquisition plans](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-1/acquisition-plans) | Planned | KMLs linked from [`sentinels.copernicus.eu/…/sentinel-1/acquisition-plans`](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-1/acquisition-plans) | datatakes: begin/end, mode, polarization, relative/absolute orbit, swath; newest file wins; ~3 weeks ahead |
| ESA burst grid ([ASF burst map, `C2450786986-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2450786986-ASF.html)) | Planned | [NASA CMR](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html) burst footprints (`fetchBursts`, `site/footprints.js`) | IW burst IDs and footprints; times from the plan + orbit timing |
| [CelesTrak](https://celestrak.org) elements | Planned | [`celestrak.org/NORAD/elements/gp.php`](https://celestrak.org/NORAD/documentation/gp-data-formats.php) (GP data, OMM JSON) | pass direction of each datatake (SGP4) |
| [ASF Search API](https://docs.asf.alaska.edu/api/basics/), SLC-BURST | History (IW bursts) | `api.daac.asf.alaska.edu/services/search/param?dataset=SLC-BURST&fullBurstID=…` | burst products (VV/VH of one pass grouped), satellite, direction |
| [ASF Search API](https://docs.asf.alaska.edu/api/basics/), Level-0 | History (EW/SM swaths) | `…/param?platform=SENTINEL-1&beamMode=EW&processingLevel=RAW&relativeOrbit=…&intersectsWith=POINT(…)` (stripmap: `beamMode=S1,S2,…,S6`, as ASF names it by its beam) | Level-0 products of the track over the swath centre, one per orbit |
| [Copernicus Data Space Ecosystem (CDSE)](https://dataspace.copernicus.eu) | Latency (reference) | [OData catalogue](https://documentation.dataspace.copernicus.eu/APIs/OData.html), `catalogue.dataspace.copernicus.eu/odata/v1/Products` | IW GRDH and SLC by publication time: the tracking universe |
| [ASF DAAC](https://asf.alaska.edu) | Latency | [NASA CMR](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html) | GRD, SLC |
| [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com) | Latency | STAC `planetarycomputer.microsoft.com/api/stac/v1` | [`sentinel-1-grd`](https://planetarycomputer.microsoft.com/dataset/sentinel-1-grd), [`sentinel-1-rtc`](https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc) |
| [OPERA](https://www.jpl.nasa.gov/go/opera/) (at ASF) | Latency | [NASA CMR](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html) | [RTC-S1](https://www.earthdata.nasa.gov/data/catalog/asf-opera-l2-rtc-s1-v1-1) (`OPERA_L2_RTC-S1_V1`), CSLC-S1 (`OPERA_L2_CSLC-S1_V1`), per burst |

## Sentinel-2

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| [ESA Sentinel-2 acquisition plans](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-2/acquisition-plans) | Planned | KMLs linked from [`sentinels.copernicus.eu/…/sentinel-2/acquisition-plans`](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-2/acquisition-plans) | nominal datatakes (NOBS): begin/end, relative orbit; ~18 days ahead |
| ESA MGRS tiling grid (KML) | Planned | [`S2A_OPER_GIP_TILPAR_MPC__…_B00.kml`](https://sentiwiki.copernicus.eu/web/s2-products) (URL in `s2grid.py`) | tile footprints; tile passes = datatakes intersecting a tile, with coverage fraction |
| CelesTrak elements | Planned | as above | per-tile sensing times (SGP4) |
| [Earth Search](https://element84.com/earth-search/), [`sentinel-2-l1c`](https://earth-search.aws.element84.com/v1/collections/sentinel-2-l1c) | History (tiles) | `earth-search.aws.element84.com/v1/search` (`grid:code = MGRS-<tile>`, one-digit zones without the zero) | tile sensing time, cloud cover; a reprocessed datatake counted once |
| [CDSE](https://dataspace.copernicus.eu) | Latency (reference) | [OData](https://documentation.dataspace.copernicus.eu/APIs/OData.html), `SENTINEL-2` L1C and L2A | publication time; 1-in-8 MGRS tile sample |
| [Earth Search](https://element84.com/earth-search/) | Latency | STAC [`sentinel-2-l1c`](https://earth-search.aws.element84.com/v1/collections/sentinel-2-l1c), [`sentinel-2-l2a`](https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a) | matched on `s2:product_uri` |
| [Planetary Computer](https://planetarycomputer.microsoft.com) | Latency | STAC [`sentinel-2-l2a`](https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a) | first seen only |
| [HLS S30](https://lpdaac.usgs.gov/products/hlss30v002/) (LP DAAC) | Latency | NASA CMR [`C2021957295-LPCLOUD`](https://cmr.earthdata.nasa.gov/search/concepts/C2021957295-LPCLOUD.html) | matched on `PRODUCT_URI` |

## Landsat 8/9

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| [USGS acquisition calendar](https://landsat.usgs.gov/landsat_acq) | Planned | `landsat.usgs.gov/sites/default/files/landsat_acq/assets/json/` `cycles.json`, `cycle_path_row.json` | paths and rows acquired on each date |
| USGS WRS-2 outlines | Planned | LandsatLook map service (`nimbus.cr.usgs.gov/arcgis/rest/services/LLook_Outlines/MapServer`) | scene footprints |
| [USGS STAC](https://landsatlook.usgs.gov/stac-server), `landsat-c2l1` | Planned (outlines) | `landsatlook.usgs.gov/stac-server/search` (last 17 days, fields extension) | each path/row's latest Level-1 footprint replaces its WRS-2 cell (cells are ~2 km off and ~14 % small) |
| CelesTrak elements | Planned | as above | scene centre times (SGP4) |
| [Earth Search](https://element84.com/earth-search/), [`landsat-c2-l2`](https://earth-search.aws.element84.com/v1/collections/landsat-c2-l2) | History (scenes) | `earth-search.aws.element84.com/v1/search` (`landsat:wrs_path`, `landsat:wrs_row`) | Level-2 scenes, cloud cover (days behind Level-1; USGS's STAC does not allow browser requests and its CMR collection has no granules) |
| [USGS STAC](https://landsatlook.usgs.gov/stac-server) | Latency (reference) | `landsat-c2l1`, `landsat-c2l2-sr` | listed by `created` (sorted) |
| [Earth Search](https://element84.com/earth-search/), [Planetary Computer](https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2) | Latency | STAC `landsat-c2-l2` | L2 mirrors |
| [HLS L30](https://lpdaac.usgs.gov/products/hlsl30v002/) (LP DAAC) | Latency | NASA CMR [`C2021957657-LPCLOUD`](https://cmr.earthdata.nasa.gov/search/concepts/C2021957657-LPCLOUD.html) | matched on `LANDSAT_PRODUCT_ID` |

## NISAR (L-band, JPL radar)

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| [NASA NISAR observation plan](https://science.nasa.gov/mission/nisar/data/) (KMZ) | Planned | linked from [`science.nasa.gov/mission/nisar/data/`](https://science.nasa.gov/mission/nisar/data/) (ROP358) | frames, dates, radar modes (per band); blank modes carried from the frame's last listed one |
| CelesTrak elements | Planned | as above | frame times (SGP4, the frame ~567 km left of the track) |
| [ISRO NISAR ROP](https://bhoonidhi.nrsc.gov.in/NISAR/ROP.html) (GeoServer) | Checked against, not used yet | `bhoonidhi.nrsc.gov.in/geoserver/NISAR_ROP/ows` (WFS, one layer per cycle; listed by `POST /NISAR/ROP {"action":"GetAllLayers"}`) | datatake segments with planned start/end times, RCID, application; see methodology |
| NASA CMR, ASF L1 RSLC | Planned (outlines) | `cmr.earthdata.nasa.gov/search/granules.json` ([`C2853145197-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2853145197-ASF.html), last 13 days) | each (track, frame, direction)'s latest RSLC footprint replaces the plan's frame (7-11 km off) |
| NASA CMR, ASF L1 RSLC | History (frames) | `cmr.earthdata.nasa.gov/search/granules.umm_json` ([`C2853145197-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2853145197-ASF.html), `TRACK_NUMBER`, `FRAME_NUMBER`) | sensing time, polarizations per frequency, full or partial frame |
| NASA CMR, ASF L2 GCOV | History (previews) | as above, [`C2854338529-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2854338529-ASF.html) | each RSLC's GCOV (same cycle, track, direction, frame and start minute), for its browse images |
| [ASF](https://nisar-docs.asf.alaska.edu/access-overview/) via CMR | Latency (reference) | [`C2853089814-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2853089814-ASF.html) (L0B), [`C2853145197-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2853145197-ASF.html) (RSLC) | listed by `created_at` |
| [ASF](https://nisar-docs.asf.alaska.edu/access-overview/) via CMR | Latency | [`C2854338529-ASF`](https://cmr.earthdata.nasa.gov/search/concepts/C2854338529-ASF.html) (GCOV) | matched on the RSLC name |

## NISAR (S-band, ISRO radar)

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| NASA NISAR observation plan (KMZ) | Planned | as above | the `S:` part of each radar mode |
| [Bhoonidhi STAC API](https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/) | History | as below (login: repository secrets) | each run's listing plus a backfill of 92 days, 3 sensing days a run (`missions/nisar_s/history.parquet`), published per track as `nisar_s/<track>.parquet` (the site cannot hold a login): track, frame, direction, sensing time, polarization (`DH` → HH+HV, `DV` → VV+VH, `CP` → RH+RV), partial frame, and an item's browse link if it has one (none used yet) |
| [Bhoonidhi STAC API](https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/) | Latency (reference) | `bhoonidhi-api.nrsc.gov.in/data/search` (`NISAR_SSAR_RSLC`; login: repository secrets) | RSLC items sensed in the last 10 days, asked one day at a time; `Track`, `Frame`, `Node`, `Polarization`; `updated` (India time) as the publication time |
| [Bhoonidhi STAC API](https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/) | Latency | `NISAR_SSAR_GCOV` | matched on the RSLC name (`NISAR_S1_…_RSLC_…` → `NISAR_S2_…_GCOV_…`, same tail) |
| [ISRO NISAR ROP](https://bhoonidhi.nrsc.gov.in/NISAR/ROP.html) (GeoServer) | Checked against, not used yet | as for L-band | the same datatake segments (one plan for both radars) |

## MODIS (Terra, Aqua) and VIIRS (Suomi NPP, NOAA-20, NOAA-21)

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| [CelesTrak](https://celestrak.org) elements | Planned | as above | every pass, predicted from the orbit (SGP4): time, cross-track distance, view zenith angle |
| MODIS sinusoidal grid | Planned | computed (`wideswath.sin_grid`: 10° tiles, h00–35 × v00–17) | the 460 tiles that hold any of the Earth; the grid of MOD09GA, MOD10A1, VNP10A1, … |
| [NASA LANCE](https://www.earthdata.nasa.gov/data/tools/lance) via CMR | History (newest 7 days), Latency (reference) | `cmr.earthdata.nasa.gov/search/granules.umm_json`: [`C1426414410-LANCEMODIS`](https://cmr.earthdata.nasa.gov/search/concepts/C1426414410-LANCEMODIS.html) (`MOD021KM`), `C1426616847-LANCEMODIS` (`MYD021KM`), `C2185497928-LANCEMODIS` (`VNP02MOD_NRT`), `C2208778455-LANCEMODIS` (`VJ102MOD_NRT`), `C3383723948-LANCEMODIS` (`VJ202MOD_NRT`) | near-real-time Level-1B granules, listed by `created_at` |
| [NASA LAADS DAAC](https://ladsweb.modaps.eosdis.nasa.gov) via CMR | History, Latency | MODIS Level-1B collection 6.1 (`C1378227407-LAADS`, `C1379758607-LAADS`) and 7 (`C3861664481-LAADS`, `C3861713185-LAADS`); VIIRS `C2105087643-LAADS`, `C2103811746-LAADS`, `C3478403600-LAADS` | standard Level-1B granules, matched on platform + start stamp |
| LANCE and [NSIDC DAAC](https://nsidc.org/data/mod10_l2) via CMR | Latency | snow cover swath: `MOD10_L2`/`MYD10_L2` NRT (`C2007659515-LANCEMODIS`, `C2007631329-LANCEMODIS`) and v61 (`C3024162910-NSIDC_CPRD`, `C3090964320-NSIDC_CPRD`); `VNP10`/`VJ110`/`VJ210` NRT (`C2780575359`, `C2781269342`, `C4232151476-LANCEMODIS`) and v2 (`C3173404788`, `C3173450415`, `C3824913163-NSIDC_CPRD`) | daytime land granules only (learned coverage) |
| [NASA Worldview / GIBS](https://nasa-gibs.github.io/gibs-api-docs/) | Preview | `gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi` (`MODIS_Terra_…`, `MODIS_Aqua_…`, `VIIRS_SNPP_…`, `VIIRS_NOAA20_…`, `VIIRS_NOAA21_CorrectedReflectance_TrueColor`; `VIIRS_SNPP_…`, `VIIRS_NOAA20_CorrectedReflectance_TrueColor_Granule`) | the day's true-colour mosaic of the platform, or one granule, lat/lon |

## Sentinel-3 (OLCI, SLSTR)

| Data source | Used for | Endpoint | What is used |
|---|---|---|---|
| CelesTrak elements | Planned | as above | passes (SGP4) on the sinusoidal tiles; relative orbit from the node longitude, frame = seconds after the node |
| [CDSE](https://dataspace.copernicus.eu) | History, Latency (reference) | [OData](https://documentation.dataspace.copernicus.eu/APIs/OData.html), `SENTINEL-3`, `OL_1_EFR` and `SL_1_RBT` | NRT frames by `PublicationDate` (the universe); NTC versions; history over a tile centre |
| CDSE | Latency | `OL_2_LFR` (OLCI land), `SL_2_LST` (SLSTR land surface temperature) | Level-2, matched on the frame (learned coverage) |
| [EUMETSAT Data Store](https://data.eumetsat.int) | Latency | `api.eumetsat.int/data/search-products/1.0.0/os` (OpenSearch, `EO:EUM:DAT:0409`, OLCI Level-1B EFR; anonymous search) | first sighting of the frame; `updated` kept as a cross-check |
| [Planetary Computer](https://planetarycomputer.microsoft.com) | Latency | STAC [`sentinel-3-olci-lfr-l2-netcdf`](https://planetarycomputer.microsoft.com/dataset/sentinel-3-olci-lfr-l2-netcdf), [`sentinel-3-slstr-lst-l2-netcdf`](https://planetarycomputer.microsoft.com/dataset/sentinel-3-slstr-lst-l2-netcdf) | NTC Level-2, matched on the frame (learned coverage) |

---

## Browse images ("Preview on map")

| Mission | Source, in order | Placement |
|---|---|---|
| Sentinel-1 | Planetary Computer RTC preview (colour, or VV/VH/HH/HV alone); its GRD preview; OPERA RTC-S1 browse of the burst (CMR `OPERA_L2_RTC-S1_V1`, image at ASF, public with CORS); ASF's GRD browse | RTC: UTM grid, exact. GRD: lat/lon. OPERA: exact, from the burst's grid in its GeoTIFF header (fixed per burst; read by the poller with an Earthdata login, `browse_extents/opera/<track>.json`); a burst not read yet is fitted to its footprint, 1.5-3 km off. ASF browse: radar geometry, no CORS, not placed |
| Sentinel-2 | Earth Search L2A thumbnail | UTM grid, exact |
| Landsat | Planetary Computer rendered preview | UTM grid, exact |
| MODIS, VIIRS | NASA Worldview (GIBS) corrected-reflectance true colour: SNPP and NOAA-20, the pass's own granules (6-minute granule layers); otherwise, or until those are published, the platform's day mosaic | lat/lon, exact (WMS over the tile or the AOI's surroundings), shown only within the tile (or the pass's granule footprints) |
| Sentinel-3 | CDSE's quicklook of the OLCI EFR frame (true colour; OData `Assets`, `QUICKLOOK`, served by `download.dataspace.copernicus.eu` with CORS), daytime (descending) frames only | instrument geometry (scan lines × across-track columns), placed from the orbit: SGP4 with the frame's first and last scan lines and swath edges from its footprint; to a few km ([methodology](methodology.md#sentinel-3-previews)) |
| NISAR (L-band) | ASF's browse of the acquisition's GCOV (made for every RSLC, ~1 h after it): the colour composite (`_NATIVE.png`) or one channel (`_NATIVE_A_HHHH.png`, `_NATIVE_A_HVHV.png`, `_NATIVE_B_VVVV.png`, ...), geocoded and terrain-corrected; until the GCOV is out, the RSLC's lat/lon colour composite (`_LATLON.png`) | GCOV: its own grid (UTM; polar stereographic near the poles), exact: the four corners from the granule's `_NATIVE.kml` (the same for every channel; read by the poller with an Earthdata login, `browse_extents/nisar_gcov/<track>.json`; the grid moves up to ~5 km between repeats of a frame), the UTM zone found from them (a polar grid is stretched between its corners). Otherwise, and for the RSLC, the lat/lon composite fitted to the CMR footprint, 0-5 km off |
| NISAR (S-band) | none: Bhoonidhi's products need a login (the poller records any browse link an item has, to check) | — |

## Terms and attribution

swathcast publishes metadata derived from these sources (product identifiers, sensing and first-seen
times, footprints, plan entries), not the products themselves. Each provider's terms apply to what
is derived from it.

| Provider | What swathcast derives from it | Terms |
|---|---|---|
| ESA / Copernicus (plans, tiling grid, CDSE) | plans, footprints, publication times | [Copernicus Sentinel data legal notice](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice): free use, including redistribution and modification, with the notice "Contains modified Copernicus Sentinel data [year]" |
| NASA (CMR, ASF DAAC, OPERA, LP DAAC HLS, LANCE, LAADS DAAC, NSIDC DAAC, NISAR plan, GIBS) | footprints, granule times, plan entries, Worldview previews | [NASA Earth science data use guidance](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance): open, no restrictions on use; citation requested |
| USGS (acquisition calendar, STAC, WRS-2) | plans, scene footprints and times | U.S. Government work, public domain; credit "Landsat imagery courtesy of the U.S. Geological Survey" |
| Element 84 Earth Search, Microsoft Planetary Computer | catalogue times, browse images | mirrors of the ESA and USGS data above, under the same terms ([Earth Search](https://element84.com/earth-search/), [Planetary Computer terms](https://planetarycomputer.microsoft.com/terms)) |
| EUMETSAT Data Store (Sentinel-3 OLCI) | product identifiers and their `updated` times | Copernicus Sentinel data, under the same Copernicus notice as ESA's ([EUMETSAT data policy](https://www.eumetsat.int/data-policy)) |
| ISRO / NRSC Bhoonidhi (NISAR S-band) | item identifiers, sensing times, track, frame and polarization, and publication times | NISAR data are open data under ISRO's [data policy](https://bhoonidhi.nrsc.gov.in/bhoonidhi_resources/help/UIM2024/4-UIM2024-Bhoonidhi_SpacePolicy_Implementation.pdf) |
| CelesTrak | orbital elements (`tle.json`) | [celestrak.org](https://celestrak.org/webmaster.php), public GP data |
| Esri, OpenStreetMap | basemap tiles, shown by the map only | [Esri terms](https://www.esri.com/en-us/legal/terms/full-master-agreement), [OpenStreetMap (ODbL)](https://www.openstreetmap.org/copyright) |

---

**Status & Next Steps** (last updated 2026-10-14; the NISAR GCOV browse rows checked live that day, the Bhoonidhi history row not yet run live; MODIS, VIIRS and Sentinel-3 rows checked live 2026-10-13): every row was checked against the live service on
2026-09-29..10-01, the outline and browse rows on 2026-10-06, Bhoonidhi with authenticated searches
from a GitHub runner on 2026-10-01. The ISRO ROP is compared with this site's NISAR plan in the
[methodology](methodology.md#isros-nisar-rop) but not used for the map yet. Links and terms added
2026-10-12.
