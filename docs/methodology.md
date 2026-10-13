# Methodology: how delivery latency is measured

This page is the measurement design and the reasons for it. Per-source field notes (what each API
returns and what was verified) are in [sources.md](sources.md).

## The question

For a Sentinel-1 acquisition sensed at time *t₀*, how long until it can be downloaded from a given
distributor (ESA CDSE, ASF, Microsoft Planetary Computer, OPERA at ASF)? The answer is a
distribution, and its slow tail matters as much as its median: planning a near-real-time wet-snow
product or a field campaign around "usually 3 h" fails badly if 10 % of scenes take three days.

## Definition of latency

> **latency = `first_seen_utc` − `sensing_start`**, in hours, where `first_seen_utc` is the time of
> the first hourly poll of *this repository* that found the granule at that source.

- **One definition for every source.** Providers' own timestamps mean different things (ESA
  publication, CMR insert of the latest revision, nothing at all for Planetary Computer). Using our
  own observation time is the only way to make CDSE, ASF, PC and OPERA comparable.
- **Resolution ±1 poll interval.** The poller runs hourly, so a granule that appears just after a
  poll is recorded up to about an hour late. Each row keeps `last_not_seen_utc` (the last check that
  did not find it), so the true arrival lies in `(last_not_seen_utc, first_seen_utc]`.
- **Sensing start** is the product's `ContentDate/Start` from CDSE (the slice start, not the datatake
  start).
- **Provider timestamps** (`provider_timestamp_utc`, `provider_timestamp_field`) are kept for every
  sighting as a cross-check, with the earliest and the latest value seen. They are never the primary
  measure. None has been validated as a publication time (see [sources.md](sources.md)); ASF's CMR
  insert date demonstrably changes after first availability.

## The tracking universe

Each hour, the poller lists **every IW GRDH and IW SLC product that CDSE published since the last
run** (OData, `PublicationDate` window, paged to the end, with a 3 h overlap to absorb catalogue
indexing lag, duplicates dropped by name). `_COG` duplicates are excluded. Every listed product
becomes one row per applicable source:

| reference product | sources |
|---|---|
| IW GRDH | `cdse_grd`, `asf_grd`, `pc_grd`, `pc_rtc` |
| IW SLC  | `cdse_slc`, `asf_slc`, `opera_rtc`, `opera_cslc` |

Other sources are then asked about **those exact granules, by identifier**: ESA scene name at ASF
(CMR `readable_granule_name`), the CRC-free item id at Planetary Computer, and the parent SLC named in
each OPERA burst's `InputGranules`. Nothing is ever "the first N search results"; every search pages
to the end and refuses to continue past a hard cap rather than silently truncating.

Why CDSE: ESA produces the products, and in the first measurements CDSE published IW products a
median of about 2 h after sensing, while ASF took days (see [sources.md](sources.md)). A product
that reaches some other source but never CDSE is invisible to this design; that is an accepted
limitation. A source that is *faster* than CDSE cannot be told apart from CDSE, because it is first
checked when CDSE lists the product.

## Censoring

A granule not yet found is **right-censored**, not dropped. Dropping unseen granules (for example by
sampling "the first 500 results") biases every quantile low; the synthetic test in
`tests/test_stats.py` shows the size of that bias.

- A row stays open until it is seen or its source's **timeout** passes (default 14 days from
  sensing; per-source in `config.py`). Then `status = timed_out`.
- A pending row is censored at its **last successful check** (not "now": if a source is down, we do
  not know that the granule was absent).
- A timed-out row is censored at the timeout.

## Kaplan–Meier

`stats.kaplan_meier` estimates *S(t)*, the probability a granule is still unavailable *t* hours after
sensing, with the standard product-limit estimator (rows censored at *t* are at risk at *t*). The site
shows **F(t) = 1 − S(t)**, "fraction downloadable by *t* hours". From it:

- **median / p90 / p99**: the smallest *t* with F(t) ≥ q. If the curve never reaches q, the value is
  "—", never an extrapolation.
- **% not delivered** = 1 − F(timeout), reported only once some rows have been followed for the full
  timeout.
- A stratum with fewer than 20 sightings is **"not measured"**. There are no estimates from
  documentation anywhere (no Earth Engine figure, for example, since Earth Engine records no ingest
  time).

The implementation is small and tested against a textbook example, a no-censoring case (KM equals
the empirical CDF), and an exponential sample with random censoring whose median is known
analytically (`tests/test_stats.py`). `lifelines` was not needed.

## Rows excluded from the statistics

Each exclusion is counted and shown in the dashboard (summary-table tooltip), not silently applied.

1. **Not prompt.** A row whose tracking began more than 2.5 h after CDSE's `PublicationDate`
   (`prompt = False`). This happens on the first run (the bootstrap lists the previous 24 h) and
   after a poller outage. For those rows, a sighting on the first check only says "it arrived at some
   point before now" (left-censoring), and for stream sources early bursts may have been missed.
   Rather than model left-censoring, such rows are tracked but not counted.
2. **Sighting after a poller gap.** If more than 3 h separate `last_not_seen_utc` and
   `first_seen_utc` (missed runs), `first_seen` is too coarse and the sighting is excluded.
3. **Outside learned coverage** (partial-coverage sources only, below).

## Partial-coverage sources

Planetary Computer and OPERA do not process every acquisition (for instance, 8,317 OPERA RTC-S1 bursts
were inserted into CMR in the 24 h to 2026-09-29 00:00 UTC; at the ~27 bursts per SLC seen that hour,
that is on the order of 300 SLCs, against about 990 IW SLCs a day at CDSE). Counting
every unprocessed ocean scene as "not delivered" would make "% not delivered" meaningless, and nothing
in the APIs states the coverage. So coverage is **learned from the data**:

- Each row gets a `coverage_cell` = relative orbit × 1-minute bin of along-orbit time since the
  ascending node (from CDSE's `startTimeFromAscendingNode`). The same ground location falls in the same
  cell every cycle, for every platform.
- For a partial-coverage source, only rows in cells where that source has delivered at least once
  are counted.

Consequences, stated plainly: "% not delivered" for these sources means *failures inside the area the
source serves*, not coverage. While the history is short, a cell becomes eligible only once something
in it has arrived, which slightly favours delivered rows; the effect shrinks as the history grows past
one repeat cycle. ASF GRD and SLC are treated as full-coverage (`coverage = "full"`), a hypothesis the
data will test: a full-coverage source with a large, geographically clustered "not delivered"
fraction would falsify it.

## Burst products (OPERA)

OPERA RTC-S1 and CSLC-S1 are per burst. For each parent SLC the row records the first burst sighting
(`first_seen_utc`, the headline latency), the set of burst IDs seen (`items`, `n_items`), and when the
last new burst arrived (`last_item_seen_utc`). The row keeps collecting until 24 h pass with no new
burst. The dashboard shows a secondary "last burst" curve, counted as an event only for settled rows.
How many bursts an SLC *should* produce is not known a priori (only land bursts are processed), so
"all bursts" is defined operationally as "no new burst for 24 h".

## Stratification

Summaries are computed per **source × product × platform × sensing month**. The headline table and
the map use the last 30 days of sensing. The trend chart is the monthly median. A split by orbit
direction or region is possible from the stored columns (`orbit_direction`, `centroid_lon/lat`) once
sample sizes allow; it is not shown yet.

## From latency to "when can I download this pass?"

For each upcoming Sentinel-1 IW pass in the schedule:

    expected_available_at[source] = pass.begin_time + quantile_q(latency curve of source)

using the platform's stratum when it has at least 20 sightings, else all platforms, else "not
measured" (`stats.expected_availability`, tested in `tests/test_expected.py`; `common.js` mirrors it).
Two caveats: `begin_time` is the datatake start, while slices later in a long datatake are sensed up
to a few minutes later; and the curve describes the last 30 days, not a guarantee.

## Sentinel-1 burst timing

ESA's plan gives datatakes (start, stop, track, a long footprint), not what is imaged over a given
place, or when. The map resolves them to the ESA burst grid:

1. **Bursts.** ASF's `Sentinel-1_Burst_Map` CMR collection (C2450786986-ASF) holds every burst of the
   fixed ESA burst grid with its polygon, track and `TIME_FROM_ANX_SEC`. The browser queries it for
   the AOI or map view (CORS is open). The time after ANX also follows exactly from the burst ID:
   `(id − 1) · 2.758273 + 2.299849 − T_orb · (track − 1)` plus a per-subswath constant (IW1 0.0008 s,
   IW2 0.8333 s, IW3 1.9113 s), with `T_orb = 12 d / 175` (checked against ASF for 2,500 bursts: exact
   to 0.1 ms).
2. **Orbit timing.** Each unit's ANX crossings form a lattice `epoch + k · period`, and orbit k has
   track `((k + offset) mod 175) + 1`. `orbit.fit` fits epoch, period and offset per platform from the
   CDSE products already in the tracking table (sensing start − `startTimeFromAscendingNode`); the
   publish step writes them to `orbits.json`. `startTimeFromAscendingNode` is measured from the ANX of
   the orbit the *datatake* began in, so it can exceed one period; the fit accounts for that.
   Validation (2026-09-29): fitted on 7 days, it predicted the next 7 days' 6,496 S1C/S1D GRD sensing
   starts to ≤ 0.37 s (p99 0.31 s); 3 slices starting 1–3 s after an ANX carry the previous track
   number in CDSE and did not match, which does not affect bursts.
3. **Planned or not.** A burst is imaged at `ANX_k + time_from_anx` for each orbit k on its track; it
   is *acquired* if that instant falls inside a planned IW datatake of the same platform (±3 s).
   For the Olympic Mountains, for example, the AOI is imaged ~4 min after the datatake starts.

`site/footprints.js` mirrors `orbit.py`; `tests/test_orbit.py` runs both on the same cases.

## Acquisition plans

The schedule is read from the agencies' own plans (`plans.py`):

- **Sentinel-1/2: ESA plan KMLs, newest wins.** ESA's acquisition-plans pages link one KML per
  platform per plan update (Sentinel-1: several a week, each running ~3 weeks ahead; Sentinel-2:
  fortnightly, ~18 days). Files overlap and later ones re-plan or drop datatakes, so each datatake is
  taken from the newest file whose window (from its file name) contains its start. Simply
  concatenating the files would keep re-planned datatakes as ghosts: on 2026-09-29 that kept 21 S1D
  and 6 S1C datatakes the newest plan no longer had. The platform is taken from the
  file name: ESA listed an S1D file under Sentinel-1C that day. Every placemark field is kept
  (datatake id, mode, swath, polarization, absolute/relative orbit).
- **Sentinel-1 direction** from the orbit at the datatake midpoint (SGP4): 3,637 of 3,653 CDSE
  products agreed (2026-09-26..29); all 16 misses poleward of 70°, where slices can cross the pole.
- **Landsat: USGS's calendar.** `cycles.json` maps each date to a 16-day cycle day;
  `cycle_path_row.json` lists the paths **and rows** acquired on each cycle day (using only the paths
  would count every row of them, including night-side rows). Checked against USGS STAC
  (`landsat-c2l1`) for 2026-09-10/11: 1,523/1,527 and 1,466/1,520 real scenes were on the calendar
  the same UTC date (1,514/1,520 within a day). Of the calendar's day-side scenes (both dates), 90–98 %
  appeared in STAC between 30° S and 90° N, 81 % at 30–60° S, but only 29 % south of 60° S (low sun
  near the equinox, presumably).
- **NISAR: NASA's observation plan** (the KMZ linked from NASA's NISAR data page): per frame, the
  dates and radar mode. Checked against CMR RSLC granules (C2853145197-ASF) sensed 2026-09-26/27:
  98.3 % of 2,804 real frames were in the plan on the same UTC date. The plan lists every date twice
  and often gives a date several modes (e.g. `L:SCI:DH:…, L:SCI:SV:…`); both are real, ASF has a
  DHDH and a NASV RSLC for such frames, so the rows are merged and every mode kept. About 20 % of the
  ROP358 plan's dates have a blank mode; they take the frame's last listed mode (`mode_source` =
  "earlier cycle"), which covered the real RSLC polarizations for 517 of 535 such frames (97 %),
  against 1,438 of 1,438 for dates whose mode is listed (2,000 most recent RSLCs, 2026-09-29).
- **Footprint geometry.** The plans' polygons have few vertices (a Sentinel-1 EW datatake: 5–11;
  a NISAR frame: 4–16) and are meant to be joined by great circles (ESA's KMLs set `tessellate`).
  Joined by straight lines in lon/lat, as the map had drawn them, high-latitude edges bend around the
  pole. So edges are densified along the great circle where it departs from the straight lon/lat
  segment by more than 0.01° (`schedule.geodesic_ring`), with continuous longitudes, then split at
  ±180°. Against the union of the real CDSE GRD footprints of 40 datatakes poleward of 70° (sensed
  26–29 Sep, in polar stereographic), the overlap (intersection over union) rose from a median 0.83
  (worst 0.27) to 0.97 (worst 0.82). Sentinel-2's plan polygons are dense enough already: tile
  matches to real L1C products were 98.9 % before and 98.8 % after. NASA's NISAR plan publishes a frame
  that crosses 180° as two placemarks, one piece on each side (299 frames in ROP358); they are joined
  into one frame (`plans.join_rings`). Full frames then overlap their real RSLC footprints by a median
  0.92 (worst 0.70) poleward of 60°, on either side of the date line; partial RSLC frames
  (`FULL_FRAME=FALSE`) cover about half the nominal frame. The map draws footprints clipped at ±85°:
  MapLibre extends any fill that reaches its Web Mercator tiles' edge (±85.05°) to the pole. On the
  globe, the parts poleward of 85° (Sentinel-1 EW reaches 87.7° N, NISAR 88° S) are drawn by an overlay
  (`site/overlay.js`: d3-geo, spherical polygons with great-circle edges) with the same colours and
  order, and can be hovered and clicked; the flat map ends at 85°. The data are not
  clipped, so an AOI inside a cap still lists its passes.
- **Edges near the poles.** Orbits converge, so footprints pile up: over 12 days a point at 60° is
  under about 5 footprints of each mission, at 82° about 18 Sentinel-1 swaths, at 75° about 22 NISAR
  frames. The map draws planned swaths without outlines (soonest on top, so the visible colour
  boundaries are the real edges), fades the other footprints' outlines from 60° to a trace at 75°,
  and leaves out the segments on ±180° where a footprint was split at the antimeridian (790 of them
  in the 2026-09-30 schedule), which are not edges. MapLibre's fill antialiasing is off: its 1 px
  outline in each polygon's own colour is drawn after all fills, so hidden footprints showed theirs.
- **Footprint timelines.** A footprint's popup lists its acquisitions of the last 90 days, read live
  in the browser from the catalogues (only products that exist; `site/history.js`): Sentinel-1 IW
  bursts from ASF's burst catalogue by full burst ID (the VV and VH bursts of one pass grouped);
  Sentinel-1 EW/SM swaths from ASF's Level-0 products of that track over the swath centre;
  Sentinel-2 tiles from Earth Search `sentinel-2-l1c` by MGRS tile (the tile's own sensing time and
  cloud cover; a reprocessed datatake counted once); Landsat scenes from Earth Search
  `landsat-c2-l2` by path/row (Level-2, days behind USGS's Level-1, whose STAC does not allow browser
  requests); NISAR frames from CMR's ASF L1 RSLC by track and frame (L-band; S-band needs a Bhoonidhi
  login). The planned acquisitions follow, and a timeline shows both.
- **Grey and filtered footprints.** A footprint whose acquisitions are all filtered out is not drawn.
  Grey means nothing is planned for it at all in the schedule, and is drawn only while that mission's
  filters and the dates are at their defaults. Fill opacity defaults to 30 % (adjustable on the map).
- **Polarizations are the real channels**, only for the SAR missions. ESA's S1 codes and the third
  token of a NISAR mode map to channels: SH → HH, SV → VV, DH → HH+HV, DV → VV+VH,
  QP → HH+HV+VH+VV, QD → HH+VV (NISAR "quasi-dual": HH and VV on two separate 5 MHz sub-bands, RSLC
  products SVSH/SHSV). The NISAR L-band mapping was checked against the RSLCs' `FREQUENCY_A/B_POLARIZATION`
  attributes. NISAR's polarization is given per band, because its radar-mode mnemonics are per band: `L:` modes are
  JPL's L-band radar (products from ASF), `S:` modes ISRO's S-band radar (products from ISRO's
  [Bhoonidhi](https://bhoonidhi.nrsc.gov.in), systematically since cycle 25, 2026-07-08). A date can carry
  several modes and a joint L+S mode (`L:SCI:DH:40M+05N:FS:B4:D02  S:DB:CP:37W:B4` → "L HH+HV, S RH+RV").
  In 1–13 Oct 2026, 1,955 of 15,795 NISAR frame acquisitions (12 %) include S-band, 11 of them S-band
  only; S-band modes are SH, DH, DV and CP, compact pol: right-circular transmit, H and V receive
  ("RH+RV", per WMO OSCAR). The map
  filters NISAR by band and by polarization; an acquisition passes if any of its band/polarization
  pairs is selected. Only L-band was checked against products (ASF RSLC). The dashboard's NISAR
  tab is L-band (ASF); S-band latency at ISRO's Bhoonidhi is its own tab (mission `nisar_s`; see
  [sources](sources.md)). Sentinel-2 and Landsat have no polarization, so the map's filters are
  per mission.

### Horizon and gaps

The schedule is built 30 days ahead (`schedule --days 30`), but only from published plans, so it
reaches only as far as each agency publishes. As of 2026-10-01:

| Mission | Plan published by | Reaches | Why it stops there |
|---|---|---|---|
| Sentinel-1C/1D | ESA, a new KML most days, each ~20–22 days long | 22 Oct (S1C), 21 Oct (S1D) | ESA plans ~3 weeks ahead. One S1D file is named for 15 Sep–5 Dec (`s1d_mp_user_20260915t185252_20261205t204800`) but its datatakes end on 9 Oct, so it adds nothing |
| Sentinel-2A/2B/2C | ESA, a KML per platform every 2 weeks, each 18 days long | 19 Oct (S2A, S2C), 12 Oct (S2B) | S2B's newest file was issued 24 Sep; the next one extends it |
| Landsat 8/9 | USGS acquisition calendar (paths per day, a fixed 16-day cycle) | all 30 days | |
| NISAR | NASA observation plan (KMZ) | all 30 days | the plan covers the whole of 2026 |

Nothing past a plan's end is filled in: extending a 12-day (S1) or 5-day (S2) repeat would be a
guess, since ESA re-plans datatakes in every file. The daily schedule job picks up each new file, so
the Sentinel horizon rolls forward by itself. A footprint's popup says where each platform's plan
ends, so "nothing planned" after that date is not read as "no pass". Its timeline writes the gap
(Δ) between consecutive acquisitions, recent ones from the catalogue and upcoming ones from the plan
(the gap across "now" runs from the last real one to the first planned one). Where they do not all
fit (Sentinel-2 tiles see a pass every 1–2 days), the longest gaps are labelled first and every
dot's tooltip carries its gap.

### ISRO's NISAR ROP

ISRO publishes its NISAR reference observation plan as a map
([bhoonidhi.nrsc.gov.in/NISAR/ROP.html](https://bhoonidhi.nrsc.gov.in/NISAR/ROP.html)): a GeoServer
WFS (`NISAR_ROP:ROP_commissioning_<n>`, one layer per 12-day cycle, listed by
`POST /NISAR/ROP {"action":"GetAllLayers"}`; cycles to the end of 2026), each feature a datatake
segment with planned `startTime`/`endTime`, `RCID`, `Application` and direction, about 8.8k a
cycle (~10 MB). What it adds to NASA's KMZ is planned times per segment; it has no frame numbers.
Compared on 2026-10-01 with this site's plan for cycle 32: 95% of the passes both plans contain fall
inside ISRO's planned window, so the orbit-derived times stand. 18% of NASA's planned frames had no
ISRO segment; in the previous cycle (31) frames like these were still acquired 75.5% of the time,
so the ROP is not a better list of what will be acquired, and the map keeps the NASA plan.

## Landsat and NISAR timing

The Landsat and NISAR plans give dates, not times. A time estimated from the nodal
crossing (10:12 local solar time for Landsat, 06:00/18:00 for NISAR) at the footprint centroid would be, on
the scenes below, median 35 min (Landsat) and 42 min (NISAR) off, up to hours at high
latitude, because local solar time along a sun-synchronous track varies with latitude. Instead
(`ephemeris.nearest_pass`), the day's orbit is propagated once (SGP4, CelesTrak elements) and each
footprint gets the pass on its date, in its direction, that passes it as expected:

- **Landsat**: WRS-2 scene centres lie on the nominal ground track, so the pass that comes closest
  (accepted within 60 km). Against 4,707 USGS STAC scene times (2026-09-10/11/26/27): median 0.3 s,
  p99 6 s; closest-approach distance median 2.4 km. 6 were a day off where the calendar and UTC dates
  differ. p95 0.5 s for scenes 1–2 days from the elements' epoch, 5–6 s at 17–18 days.
- **NISAR** looks left: frame centres were 561–579 km (p5–p95) left of the ground track in the same
  check, so the pass that has the frame ~567 km to its left (accepted within 60 km). Against 2,804
  RSLC granule mid-times: median 0.65 s, p95 14 s (frames are ~35 s long), 24 over a minute.

Without elements, both fall back to the formula (the `timing` column says which was used).

**Landsat's turnaround rows** (2026-10-05). At rows 122 (~82° S) and 246–248 (~81° N) the track
turns: it runs neither north nor south, so the direction filter could drop the scene's own pass,
and ~540 scenes a month (of ~70,000) took the formula, up to an orbit off. Poleward of 78° a scene
left without a pass is now looked for in either direction (elsewhere a pass the other way can cross
a scene centre, so the filter stays). Three USGS row-246 scenes (2026-08-25/26), timed with elements
a month later, come out within 15 s (`test_landsat_turnaround_rows_timed_from_the_orbit`).

## The live satellites on the map

The map draws each selected satellite where SGP4 (satellite.js, the same CelesTrak elements as the
timing above) puts it now, at its altitude, with its orbit, and the strip of ground its instrument
sees (`site/swathgeom.js`), as signed distances across the ground track, perpendicular to the
direction of travel over the rotating Earth (what a zero-Doppler radar looks along):

| Instrument | Strip (km from the ground track) | From |
|---|---|---|
| Sentinel-1 IW | 369–619 right | 250 km swath; GRD footprint centres 488–500 km right (`ephemeris.py`) |
| Sentinel-1 EW | 215–625 right | 400 km swath, incidence 18.9–47° |
| Sentinel-1 SM | 215–625 right | the plan does not name the beam: the extent of all six 80 km beams |
| NISAR | 446–688 left | ~240 km swath; frame centres 561–579 km left (see above) |
| Sentinel-2 MSI | 145 either side | 290 km swath |
| Landsat OLI | 92.5 either side | 185 km swath |

The strip is filled where the plan has the satellite imaging: inside a Sentinel-1/2 datatake, within
half a frame (18 s) of a NISAR frame time or half a scene (16 s) of a Landsat scene time. Checked on
2026-10-05 against the next 30 h of the plans: at times inside each datatake (every 20 s), the strip's
line of sight lay inside the planned footprint for 100% of samples (median, p10) for 1,444 IW and 721
EW samples and 98.3% (mean) for 2,431 Sentinel-2 ones; the strip's centre was inside 400 of 400
sampled NISAR frames and Landsat scenes at their times, and over all of the schedule's orbit-timed
frames and scenes it was within 300 km of the centre of every one (39,566 NISAR frames, 69,457
Landsat scenes). SM is the exception, as expected: its beam's 80 km strip is a third of the extent
drawn.

## Sentinel-2 tile timing

**Tiles versus swaths.** A tile counts as imaged by a datatake when any part of it is: Sentinel-2
makes a product for a tile even when the swath only clips it (mostly no-data). So zoomed in, a tile can
be coloured by a pass a day before the swath that covers a given point inside it, and tiles can light up
north of the planned swaths' reach. Each tile pass carries `coverage`, the fraction of the tile inside
the datatake footprint, shown in the map as "covers ~N% of the tile". Checked against the real L1C
footprints of two datatakes on 2026-09-22 (S2B relative orbit 70, S2C 142; 579 tiles): median absolute
error 1.3 percentage points, 90 % within 9, correlation 0.90; of the 210 tiles under half covered, 181
were predicted under half. Of the S2B datatake's 333 predicted tiles, 6 had no product (343 products).
The plan in late September/October has no Sentinel-2 datatake north of 81.0° N (on 2026-09-22 one
reached 82.5° N, as did its products): Sentinel-2 images only in daylight, and the high Arctic is
going into polar night.

The Sentinel-2 plan also gives datatakes only, and a datatake can run pole to pole in 32 minutes.
Planetary Computer's STAC `datetime` is the *datatake* start for every tile of it (26 of the 624 L2A items
of the 2026-09-26 S2A relative-orbit-21 datatake, sampled south to north, all carry 08:27:51Z, while
their granule `SENSING_TIME`s run from 08:28 to 09:00), so "the time of the pass" needs care. `s2grid.py`:

1. **Tiles.** ESA's tiling grid KML (`S2A_OPER_GIP_TILPAR_MPC__20151209…_B00.kml`, 56,686 tiles,
   109 MB, cached in CI) gives each tile's corners. A tile is acquired by every nominal (`NOBS`)
   datatake that intersects it. Checked on three long datatakes (2026-09-26..27) against Planetary
   Computer: every produced tile intersected its datatake, and 98 % of intersecting tiles were
   produced (the others overlapped by < 2 %). Calibration, dark and raw takes are not tiled.
2. **Time: from the orbit.** The plan says *whether* a tile is imaged; the satellite's orbit says
   *when*. The schedule job fetches CelesTrak elements for S2A/B/C, propagates them with SGP4
   (`ephemeris.py`, the same model the map uses in the browser), and takes the moment the ground
   track passes closest to the tile centre (1 s steps, parabolic refinement), clamped to the
   datatake ± 30 s (real tiles were sensed up to 16 s before their datatake's planned start).
   Against 91 granule `SENSING_TIME`s in 7 datatakes (2026-09-26..27; fixture
   `tests/fixtures/s2_tile_sensing.json`, elements of 2026-09-29, so propagated 2–3 days back):
   median 0.4 s, max 8.6 s. Every tile's closest approach was within 234 km of the track, as the
   290 km swath plus half a tile allows. Not yet checked: propagation more than ~3 days ahead of the
   elements; the schedule is rebuilt daily, so the next few days always use fresh elements.
3. **Fallback: from latitude** (per platform, if its elements cannot be fetched; the `timing` column
   says which was used). The nadir latitude fixes the argument of latitude `u`
   (`sin lat = sin i · sin u`, i = 98.62°), `u` advances uniformly over the 10 d / 143 orbit period,
   anchored on the datatake end at its southern edge. Same 91 tiles: at most 35 s off. It depends on
   knowing the pass direction, which it takes from local solar time; on the full 2026-09-29 schedule
   that misclassified 2,622 of 117,826 tile passes on high-latitude (50–82°) datatakes as ascending,
   putting them ~2 min off. This is why the orbit is the primary method. The datatake start alone is
   up to 32 min off.

The schedule job writes `s2_grid.parquet` (corners, 1e-4° integers, ~0.4 MB) and
`s2_tile_passes.parquet` (tile, platform, relative orbit, time); the browser loads them only when
Sentinel-2 is needed.

## Other missions: Sentinel-2, Landsat 8/9, HLS, NISAR

The same tracker runs once per mission (`missions.py`), with its own reference catalogue, sources and
table under `data/missions/<key>/`; definitions (first-seen latency, censoring, Kaplan–Meier, prompt
rows, learned coverage, timeouts) are unchanged. Only the universes and matching differ:

| Mission | Universe (reference, first-seen at listing) | Sources | Matched on |
|---|---|---|---|
| Sentinel-2 | CDSE L1C and L2A by `PublicationDate`, **1-in-8 MGRS tile sample** (`crc32(tile) % 8 == 0`) | Earth Search L1C and L2A; Planetary Computer L2A; HLS S30 | ESA name (`s2:product_uri`; PC id without `_Nxxxx`; HLS `PRODUCT_URI`) |
| Landsat 8/9 | USGS STAC `landsat-c2l1` and `landsat-c2l2-sr`, by `created` | Earth Search and Planetary Computer `landsat-c2-l2`; HLS L30 | L2 id without processing date and `_SR`; HLS `LANDSAT_PRODUCT_ID` = the L1 id |
| NISAR (L-band) | CMR L0B RRSD and L1 RSLC (ASF, PROVISIONAL_V1), by `created_at` | GCOV | the RSLC name with the product prefix swapped (`InputGranules` confirms) |
| NISAR S-band | ISRO Bhoonidhi `NISAR_SSAR_RSLC` sensed in the last 10 days (needs a login) | Bhoonidhi GCOV | the RSLC name with `L1`→`L2`, `RSLC`→`GCOV` (assumed, see below) |

- **Why a Sentinel-2 sample.** CDSE publishes ~15,000 L1C and ~15,400 L2A products a day
  (2026-09-28); tracking all of them at four sources would multiply the hourly job's table and lookups
  by ~8. The sample is fixed in advance and spatially uniform (every product of a sampled tile is
  tracked), so it estimates the same distributions; it is not "the first N results".
- **Sentinel-2 sensing time** is CDSE's `ContentDate/Start`, the datatake start shared by every tile
  of a datatake: a tile is imaged up to ~30 min later, so per-tile latency is at most that much lower
  than reported.
- **HLS** covers land only and processes only some inputs (e.g. not every Landsat T2 scene), so both
  HLS sources use learned coverage (tile, or path/row), and their timeouts are 10 days (median insert
  − sensing ~40–44 h on 2026-09-28).
- **Landsat 8**: its real-time (RT) products are not in USGS's public STAC (2026-09-29); L8 scenes
  enter as T1/T2 7–12 days after sensing, so they are rarely *prompt* and Landsat 8 statistics mostly
  reflect Landsat 9 until that changes.
- **NISAR** products are provisional; searching is anonymous, downloading needs an Earthdata login.
- **NISAR S-band** (`sources/bhoonidhi.py`) is ISRO's, distributed by
  [Bhoonidhi](https://bhoonidhi.nrsc.gov.in), whose [STAC search API](https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/)
  needs a Bhoonidhi account. The poller reads `BHOONIDHI_USER_ID` and `BHOONIDHI_PASSWORD` from
  repository secrets and skips the mission without them (it never fails the job for that). Each run
  logs in once and logs out at the end, because Bhoonidhi limits concurrent sessions. Bhoonidhi
  documents no publication time, so the universe is every RSLC item sensed in the last 10 days, first
  seen by the hourly listing (asked one sensing day at a time); a sighting is prompt only when the
  previous listing ran within 3 h (plus the 3 h overlap), so the first run's backlog and the run after
  an outage are excluded. S-band names follow the L-band convention with ISRO's `S1`/`S2` in place of
  `L1`/`L2` (`NISAR_S1_PR_RSLC_<cycle>_<track>_<A|D>_<frame>_…`), so each RSLC's GCOV name is known
  from its own; both were confirmed on live items (2026-10-01).

## Storage and the run loop

- The table is partitioned Parquet by sensing month on an orphan `data` branch that is
  **squash-force-pushed** (always one commit, so the repository never grows). This was chosen over
  release assets because the poller has to read and rewrite state every hour; a branch is a plain
  `git clone --depth 1`, and a GitHub Release would need an upload/delete cycle per run and has no
  atomic replace.
- Each run (`tracker.Tracker.run`) reads the months that can still hold open rows, extends the
  universe, polls each source independently, closes timed-out rows, and writes the months, cursors
  and `health.json` back. It is **idempotent** (re-running at the same time adds nothing) and
  **resumable** (cursors are saved with the table; a failed source keeps its rows open and is
  retried next hour).
- A source that raises is logged with a traceback and recorded in `health.json`; the others carry on.
  The job exits non-zero if every source failed in a run or any source has been failing for more
  than 24 h, but the data branch is pushed first.

---

**Status & next steps** (last updated 2026-10-12): implemented and unit-tested for every mission,
including Sentinel-1 burst timing and Sentinel-2 tile timing; the hourly poller has run since
2026-09-29. Next: revisit the 2.5 h prompt tolerance and 3 h gap rule against observed outages; add
a direction/region split when sample sizes allow.
