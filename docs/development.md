# Development

## How it runs

| Workflow | When | What |
|---|---|---|
| [`schedule.yml`](../.github/workflows/schedule.yml) | daily | plans for the next 30 days, as far as each is published (ESA, USGS, NASA), orbit-predicted MODIS/VIIRS/Sentinel-3 passes, footprint grids and orbit timing → `data` branch |
| [`poll.yml`](../.github/workflows/poll.yml) | hourly | latency poller → `data` branch; fails if every source failed or one has failed > 24 h |
| [`pages.yml`](../.github/workflows/pages.yml) | after each poll/schedule, and on code changes | summarise, check the data contract, deploy Pages |
| [`watchdog.yml`](../.github/workflows/watchdog.yml) | weekly | opens/updates a `stale-data` issue if data is stale or a source is failing; closes it on recovery |
| [`ci.yml`](../.github/workflows/ci.yml) | push / PR | ruff, pytest (offline, recorded fixtures), site data-contract check |

The `data` branch holds `observations/year=YYYY/month=MM/part-0.parquet` (one row per granule ×
source), `state/cursors.json`, `health.json` and `schedule/`. It is replaced by a single commit on
every push, so the repository does not grow.

Keeping the scheduled jobs running, and the secrets they use: [operations.md](operations.md).

## Checking a plan by hand

To sanity-check the Sentinel-1 plan against ESA directly, open the newest KML linked from ESA's
[acquisition-plans page](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-1/acquisition-plans)
in Google Earth and compare with a box drawn over the same area on the map.

## Layout

```
src/swathcast/
  plans.py         the agencies' plans: ESA KMLs (newest wins), USGS Landsat calendar, NISAR plan
  schedule.py      assembles the schedule and the footprint grids (+ documented fixes)
  ephemeris.py     SGP4 sub-satellite points and closest-approach timing
  s2grid.py        ESA's Sentinel-2 tiling grid and per-tile times
  wideswath.py     MODIS/VIIRS/Sentinel-3: no plans; passes on the sinusoidal tiles from the orbit
  orbit.py         Sentinel-1 ANX lattice fitted from CDSE products (burst timing)
  sources/         one module per source behind lookup(ids) / list_new(window)
  tracker.py       hourly state update, censoring, timeouts, health
  stats.py         Kaplan–Meier, quantiles, expected availability, site summary
  publish.py       site data files + data-contract check
  storage.py       partitioned Parquet store (the table schema is here)
  timeutil.py      the one UTC timestamp parser
  site/            static pages: map (index.html) and dashboard
docs/              data-sources.md, methodology.md, sources.md, development.md, operations.md
```

## Sentinel-1 latency sources

| Key | Distributor | Product | Provider timestamp kept as cross-check |
|---|---|---|---|
| `cdse_grd` / `cdse_slc` | ESA Copernicus Data Space Ecosystem | IW GRDH / IW SLC (reference catalog) | OData `PublicationDate` |
| `asf_grd` / `asf_slc` | ASF DAAC (CMR) | IW GRDH / IW SLC | CMR `ProviderDates[Insert]` (changes on re-ingest) |
| `pc_grd` | Microsoft Planetary Computer | `sentinel-1-grd` | none exists (first-seen only) |
| `pc_rtc` | Microsoft Planetary Computer | `sentinel-1-rtc` | none exists (`s1:processing_datetime` is ESA's processing time) |
| `opera_rtc` | OPERA at ASF DAAC (CMR) | `OPERA_L2_RTC-S1_V1`, per burst → parent SLC | min `ProviderDates[Insert]` over bursts |
| `opera_cslc` | OPERA at ASF DAAC (CMR) | `OPERA_L2_CSLC-S1_V1`, per burst → parent SLC | min `ProviderDates[Insert]` over bursts |

Other missions have their own tracker (`missions.py`): Sentinel-2 (`sources/s2.py`), Landsat
(`sources/landsat.py`), NISAR (`sources/nisar.py`, `sources/bhoonidhi.py`), MODIS and VIIRS
(`sources/modis.py`: LANCE, LAADS, NSIDC via CMR) and Sentinel-3 (`sources/s3.py`: CDSE, EUMETSAT,
Planetary Computer).

**Google Earth Engine is not included**: there is no service account configured, and no asset
property has been confirmed to record ingest time. OPERA DSWx-S1 and DIST-S1 are possible later
additions.

## Add a source

1. Write `src/swathcast/sources/<name>.py` with a `Source` subclass: set `key`, `label`,
   `distributor`, `product`, `reference_product` (`"GRD"` or `"SLC"`: which CDSE product it is
   matched against), `coverage` (`"full"` or `"learned"`), and implement
   `lookup(ids) -> {id: Found}` returning **only** the ids that are available now. Raise on any
   incomplete lookup; never return a partial answer. For APIs that cannot look up by id, read a
   complete change stream with a cursor, as `sources/opera.py` does.
2. Register it in `SOURCE_CLASSES` in `sources/__init__.py` and give it options (timeout) in
   `config.TrackerConfig.source_options`.
3. Verify its field semantics and identifier matching against the live API, write them up in
   `docs/sources.md`, record a fixture in `tests/record_fixtures.py`, and add offline tests plus a
   `@pytest.mark.live` smoke test.
4. Give it a colour slot in `site/common.js` (`SOURCE_SLOT`) and a popup position in `site/map.js`.
