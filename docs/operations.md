# Operations: keeping the scheduled jobs alive

GitHub disables scheduled workflows in public repositories after 60 days without repository
activity. swathcast guards against that, and against failing jobs, five ways:

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

If every scheduled workflow *is* disabled, the watchdog cannot run either. The signals are then the
site's banner, the absence of new commits on the `data` branch, and GitHub's own email about the
disabled workflows. Re-enabling is one click per workflow in the Actions tab.

## Secrets

| Secret | Used by | Without it |
|---|---|---|
| `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` | `poll` (exact placement of NISAR and OPERA browse images) | previews are fitted to the catalogue footprint instead (NISAR 0–5 km off, OPERA 1.5–3 km) |
| `BHOONIDHI_USER_ID`, `BHOONIDHI_PASSWORD` | `poll` (NISAR S-band latency at ISRO's Bhoonidhi, and the S-band history the map's popup shows) | the S-band mission is skipped and the popup shows L-band only; the job does not fail |

Secrets are not passed to workflows triggered by pull requests from forks; `ci.yml` needs none.
