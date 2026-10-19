# External scheduler transition (cron-job.org)

The external scheduler is a *trigger* only. All Fubon credentials, FinLab completeness checks,
Google Drive completion markers, state writes, and result emails remain in GitHub Actions.

## GitHub API request for every cron-job.org job

- Method: `POST`
- URL: `https://api.github.com/repos/hh4832/complete-pullback-fubon-pipeline/dispatches`
- Headers:
  - `Accept: application/vnd.github+json`
  - `Content-Type: application/json`
  - `Authorization: Bearer <fine-grained GitHub PAT>`
- Body (raw JSON): `{"event_type":"fubon-pipeline-daily"}`
- Success at the dispatch transport layer: HTTP 204 (does **not** prove successful research execution).

The fine-grained PAT needs access to this repository and Contents: read/write.
Do not commit the token or store it in the request body.

## Cron jobs (time zone: Asia/Taipei)

Create **four separate jobs** if multiple schedules cannot be configured for one job.
The day-of-week mapping is intentional: Monday-Friday evening and Tuesday-Saturday early morning,
matching the existing UTC Monday-Friday GitHub schedule.

| Job name | Taiwan local crontab | Local execution |
| --- | --- | --- |
| Fubon pipeline evening | `30 22 * * 1-5` | Mon-Fri 22:30 |
| Fubon pipeline after midnight | `30 0 * * 2-6` | Tue-Sat 00:30 |
| Fubon pipeline early | `30 3 * * 2-6` | Tue-Sat 03:30 |
| Fubon pipeline last retry | `30 6 * * 2-6` | Tue-Sat 06:30 |

These are **Asia/Taipei** expressions, not UTC.

## Safety and activation sequence

1. Leave the four external jobs **inactive** while creating their request settings.
2. Verify each job has the correct URL, method, event type, headers and local timezone.
3. Use the **inactive** Evening job to run one safe transport smoke test:
   temporarily replace the JSON body with
   `{"event_type":"fubon-pipeline-daily","client_payload":{"smoke_test":true}}`,
   then click the provider's manual Test/Run option. HTTP 204 should produce
   a GitHub `repository_dispatch` run with the `external-dispatch-smoke` job
   successful and the `run-pipeline` job **skipped** (no Fubon login, FinLab
   processing, Drive writes, or result emails). Restore the original production
   body immediately after the test:
   `{"event_type":"fubon-pipeline-daily"}`. Do not leave smoke_test=true in any
   scheduled job.
4. Activate the four external jobs only after the smoke test succeeds. Verify
   a subsequent real HTTP 204 and its corresponding Github Actions run.
5. After a real external run is verified, reduce GitHub's native `schedule`
   to a small staggered fallback; until then leave existing GitHub schedules
   intact. Verify effective as-of date, one successful pipeline, one Drive
   completion marker and no redundant result email on fallback.

The workflow already treats `repository_dispatch` and `schedule` as automatic runs:
both require the expected as-of date and check the Drive completion marker.
Only explicit `workflow_dispatch` bypasses those checks.
`concurrency.cancel-in-progress: false` serializes GitHub runs in this repository.
This is not a transactional cross-system lock; operational audit is still required.

The optional `client_payload.smoke_test=true` is a transport-only validation
mode. It must be the JSON boolean `true` and is **not** an automatic retry.
Normal cron-job.org requests omit `client_payload` entirely.
