# Operations: keeping the scheduled jobs alive

GitHub disables scheduled workflows in public repositories after 60 days without repository
activity. swathcast guards against that, and against failing jobs, six ways:

1. **Every poll is repository activity.** Each hourly `poll` run pushes to the `data` branch.
2. **The watchdog re-enables the jobs.** The weekly `watchdog` workflow runs `gh workflow enable` on
   `poll`, `schedule`, `pages` and itself (it requests `actions: write` for this).
3. **The site says when its data are stale.** Both pages show a banner when a dataset is older than
   twice its cadence: 48 h for the schedule, 2 h for latency.
4. **The watchdog opens an issue.** It opens or updates a `stale-data` issue when data are stale or a
   source is failing, which notifies the repository owner, and closes it on recovery.
5. **The schedule job opens its own issues, the same day.** A failed scheduled `schedule` run opens
   or updates a `schedule-failed` issue. A plan file ESA lists but serves as a 404 is skipped (the
   run still succeeds, from the platform's older files) and opens a `plan-file-missing` issue naming
   it. Each closes itself on the first run that no longer has the problem.
6. **A satellite (or one of its data streams) that goes silent opens an issue.** A source can answer
   every hour with nothing new because its satellite stopped, or because the distributor moved its
   products: `health.json` cannot see either. After each poll, `swathcast satellites`
   (`satellites.py`) compares each platform's newest data, per stream, with that stream's p95
   delivery time, its longest normal gap and the passes the schedule says were due. A platform is
   `silent` when none of its streams has delivered for longer than max(48 h, 1.5 x its longest gap
   over 60 days) past its delivery time while passes were due, and `stream_silent` when one stream
   is silent while another still delivers (NISAR's L-band at ASF while its S-band arrives from
   ISRO). One `satellite-status` issue lists them; it is edited and commented on only when the list
   changes, and closed when it is empty. The site reads the same check (`satellites.json`): a
   silent satellite's projection is paused and its passes carry the warning; a silent stream is
   noted. Our own failing sources make the status `unknown`, not silent. Run it by hand with
   `swathcast satellites --data-dir data --schedule-dir data/schedule`.

   A satellite the registry (`src/swathcast/site/platforms.json`) marks retired is reported as
   `retired`, not `silent`: its silence is expected, so it opens no issue.

   **Agency notices, fetched daily.** The `notices` workflow (`swathcast notices`, `notices.py`)
   reads the Copernicus Data Space news RSS, NASA Earthdata's alerts and outages, NOAA OSPO's
   operational messages and USGS EROS's data notifications, keeps the last 180 days of items whose
   title names a tracked satellite or mission (word-boundary patterns), and writes them to the data
   branch (`notices/notices.json`; the site's `notices.json`). The Satellites page lists the newest
   per satellite (and mission-wide ones per mission) beside the curated ones, with each feed's fetch
   status. A source that fails is skipped and reported there. A notice whose title reads like a
   status change (end of operations, decommissioning, extension, transfer of duty, data opening…)
   that `platforms.json` neither cites nor lists under `acknowledged` opens a `registry-review`
   issue, edited only when the list changes and closed when it is empty: update the entry (and cite
   the notice), or add its URL to `acknowledged` if it changes nothing.

   **Keeping the registry current.** `platforms.json` holds each satellite's status (with the date
   it began and its source), launch and operations dates, and up to a few of the agency's notices,
   with the date each entry was last reviewed; the Satellites page shows it. Update it when an
   agency announces a change (an extension, a decommissioning, a new satellite): the `satellite-
   status` issue is the usual prompt. The site contract (`swathcast check-site`) checks it (states,
   dates, sources, https links).

   Its first run on real data (2026-11-09) found both kinds: Suomi NPP's VIIRS had delivered
   nothing to LANCE or LAADS since 2026-11-02 15:24 UTC (`silent`); and ASF had moved NISAR to new
   `V1` collections (L0B `C3622265756-ASF`, RSLC `C3622236985-ASF`, GSLC `C3622244601-ASF`, GCOV
   `C3622241360-ASF`) around 2026-10-21, so the `PROVISIONAL` ones the poller lists had been empty
   since (`stream_silent`, the satellite fine) while the source still reported "ok". The NISAR
   sources were moved to the V1 collections on 2026-11-10 ([sources](sources.md)).

If every scheduled workflow *is* disabled, the watchdog cannot run either. The signals are then the
site's banner, the absence of new commits on the `data` branch, and GitHub's own email about the
disabled workflows. Re-enabling is one click per workflow in the Actions tab.

## Secrets

| Secret | Used by | Without it |
|---|---|---|
| `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` | `poll` (exact placement of NISAR and OPERA browse images) | previews are fitted to the catalogue footprint instead (NISAR 0–5 km off, OPERA 1.5–3 km) |
| `BHOONIDHI_USER_ID`, `BHOONIDHI_PASSWORD` | `poll` (NISAR S-band latency at ISRO's Bhoonidhi, and the S-band history the map's popup shows) | the S-band mission is skipped and the popup shows L-band only; the job does not fail |

Secrets are not passed to workflows triggered by pull requests from forks; `ci.yml` needs none.
