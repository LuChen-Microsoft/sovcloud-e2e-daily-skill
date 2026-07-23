---
name: sovcloud-e2e-daily
description: >-
  Run the SovClouds CS-3 and CS-5 E2E test playlists against the Delos cloud (AppDelos.config),
  3 runs each, and produce a separate best-of-3 stability report per playlist. Use when the user
  asks to run the daily SovCloud E2E tests, run CS-3 / CS-5 playlists, or generate the daily Delos
  stability reports. Triggers: "run daily sovcloud tests", "run CS-3 and CS-5", "daily delos e2e report".
user-invocable: true
---

# SovClouds daily E2E run (CS-3 + CS-5) — Delos stability reports

Run the two SovCloud playlists 3× each against Delos and produce **one stability report per playlist**.
Both playlists run against **`AppDelos.config`** (confirmed by the repo owner). Each full run of CS-3
(~190 tests) takes ~1.5–2h; CS-5 (~44 tests) is much shorter. Budget ~6–8h total for both, 3 runs each.

## Inputs / defaults

| Thing            | Value |
|------------------|-------|
| Repo root        | `Q:\repos\async_messaging_e2e-tests` |
| CS-3 playlist    | `Q:\repos\async_messaging_e2e-tests\SovClouds CS-3.playlist`  → label `CS-3 (Basic Messaging)` |
| CS-5 playlist    | `Q:\repos\async_messaging_e2e-tests\SovClouds CS-5.playlist`  → label `CS-5` |
| Config (both)    | `AppDelos.config`, cloud `Delos` |
| Runs per playlist| `3` (best-of-3 stability) |
| Skill scripts    | `~/.copilot/skills/sovcloud-e2e-daily/scripts/` (`Run-Playlist.ps1`, `parse_report.py`) |
| Output root      | one dated folder per daily run, e.g. `~/.copilot/session-state/<session>/files/daily-<yyyyMMdd>/` with `cs3\` and `cs5\` **run** subfolders; **both final reports land together in that dated root** |

## Prerequisites (verify once at the start, fix if missing)

1. **Corp `az login`** is done for tenant `72f988bf-86f1-41af-91ab-2d7cd011db47` (APM token source).
   Test: `az account get-access-token --scope "api://d25164f2-e94b-44ea-9ddb-f858dfedb399/.default" --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47 --query expiresOn -o tsv`.
   If it fails, ask the user to run `az login --tenant 72f988bf-86f1-41af-91ab-2d7cd011db47`.
2. **VPN** to the SovCloud is connected (AzureVPN). Without it the APIs are unreachable.
3. **NuGet `scc` source disabled** — that global feed is dead (404) and breaks restore. Run once:
   `dotnet nuget disable source scc` (harmless if already disabled; the repo only needs `tps`).
4. **VS Test Platform** present — `Run-Playlist.ps1` auto-discovers `vstest.console.exe`.

`Run-Playlist.ps1` already handles per-chunk APM token refresh (the token lives ~1h, so it is
re-acquired before every chunk), chunking by test class to stay within that window, and exact
matching of data-driven test cases via `/ListFullyQualifiedTests`.

## Workflow

Do **CS-3 and CS-5 as two independent passes**. Each pass writes its own **run artifacts** (`trx\`,
`runner.log`, `run_meta.json`) to its own `cs3\` / `cs5\` subfolder so the parallel/sequential runs never
collide, but **both final reports are written into the single shared dated root** (`$out`) so you don't
have to hunt across folders. Pick the **date once at the very start** and pass it to both report steps —
that way a run that drags past midnight (CS-3 alone is ~1.5–2h; both together 6–8h) still produces one
`daily-<yyyyMMdd>` folder and one dated report set instead of splitting across two calendar days.

### 1. Kick off the runs (long-running — run in the background)

Pick `$date` and `$out = <output-root>` **once** (create it). For **each** playlist run `Run-Playlist.ps1`.
Because the whole thing is multi-hour, launch it as a **detached** background process and monitor via the
log, rather than blocking a single tool call. The first invocation builds; the second can pass `-SkipBuild`
(the DLL is already built and the config is unchanged — both use `AppDelos.config`).

```powershell
$skill = "$env:USERPROFILE\.copilot\skills\sovcloud-e2e-daily\scripts"
$repo  = 'Q:\repos\async_messaging_e2e-tests'
$date  = Get-Date -Format 'yyyyMMdd'          # PIN the date once for the whole daily run
$out   = "<output-root>\daily-$date"          # e.g. ...\files\daily-20260706
New-Item -ItemType Directory -Force -Path $out | Out-Null

# CS-3 (builds + patches app.config to AppDelos.config)
& "$skill\Run-Playlist.ps1" -PlaylistPath "$repo\SovClouds CS-3.playlist" `
    -ConfigName AppDelos.config -OutDir "$out\cs3" -Runs 3

# CS-5 (same config + dll already built -> skip build)
& "$skill\Run-Playlist.ps1" -PlaylistPath "$repo\SovClouds CS-5.playlist" `
    -ConfigName AppDelos.config -OutDir "$out\cs5" -Runs 3 -SkipBuild
```

Run these **sequentially** (one playlist after the other) — they share the single built DLL and the
same machine, and parallel vstest processes would contend for the APM pool and skew results. Monitor
`"$out\cs3\runner.log"` / `"$out\cs5\runner.log"`; each ends with `ALL DONE`. A clean log shows every
`RUN r CHUNK c done exit=N` line — `exit=1` just means ">=1 test failed in that chunk", which is normal;
it is **not** a harness error. Wholesale token/auth failure would instead show `TOKEN FAIL` or a chunk
with 0 passes across an entire run.

### 2. Generate the per-playlist reports

After **each** playlist's `runner.log` reaches `ALL DONE`:

```powershell
python "$skill\parse_report.py" --trx "$out\cs3\trx" --tests "$out\cs3\tests.txt" `
    --label "CS-3 (Basic Messaging)" --cloud Delos --config AppDelos.config --runs 3 `
    --outdir "$out\cs3" --report-out "$out" --date $date

python "$skill\parse_report.py" --trx "$out\cs5\trx" --tests "$out\cs5\tests.txt" `
    --label "CS-5" --cloud Delos --config AppDelos.config --runs 3 `
    --outdir "$out\cs5" --report-out "$out" --date $date
```

`--outdir` stays the per-playlist run folder (that is where `trx\` and `run_meta.json` live and where the
parser reads timing from). `--report-out "$out"` sends the **deliverables** to the shared dated root, and
`--date $date` pins the filename + reports-root date so CS-3 and CS-5 always share one folder even across
midnight. In the shared root each writes the styled `<slug>_<yyyyMMdd>.md` (already unique — e.g.
`CS-3_20260706.md`, `CS-5_20260706.md`) plus a playlist-key-suffixed `report_cs3.md` / `report_cs5.md` and
`metrics_cs3.json` / `metrics_cs5.json` so the two passes never overwrite each other. (Omitting
`--report-out`/`--date` keeps the old per-folder `report.md` / `metrics.json` behavior.)
Both reports start with timestamp lines: **report-generation time** (with timezone) and, when
`run_meta.json` is present (written by `Run-Playlist.ps1` at `ALL DONE`), the **test start → end time**
with timezone and total duration. If you regenerate a report from existing `trx\` without re-running,
the "Tests ran" line is preserved as long as `run_meta.json` is still in the output folder.

**Reports root + previous-run comparison.** The parser persists to `--reports-root\<yyyyMMdd>\`: the
human-readable styled report `<slug>_<yyyyMMdd>.md` (e.g. `CS-3_20260706.md`) and the plain
`report_<playlist-key>.md` (e.g. `report_cs3.md`) land **directly in the dated folder** — both playlists'
reports are visible side-by-side without opening a subfolder. Only the machine-readable `metrics.json` is
nested one level deeper, under `<yyyyMMdd>\<playlist-key>\metrics.json` (playlist-key `cs3`/`cs5`), because
`find_prev_metrics()` globs that exact shape to discover prior runs — it isn't meant to be browsed directly.
This means the **human-readable reports sync across machines too** (not just the metrics), so you can read a
run's report from either machine. The **default reports root is `%OneDrive%\delos-test-reports`** when
OneDrive is available (falling back to
`Q:\delos-test-reports`), so both the reports and the comparison history are **shared across machines** — run
the daily reports from either machine and the trend still lines up. On each run it auto-discovers the
**latest earlier** `metrics.json` for the same playlist under that root and emits a
`📈 Comparison vs previous run (<date>)` section (pass-rate, deterministic %, executed count, and
per-category failure deltas with 🔼/🔽 arrows). It also emits a **per-test fixed/new churn** subsection —
✅ tests that were failing last run and now pass, 🆕 tests that were passing and now fail (each tagged with
its failure category), a ♻️ still-failing count, and a **net failing-test change** line. This surfaces churn
that flat headline numbers hide: e.g. if 3 timeouts are fixed but 3 *different* tests newly time out, the
`Timeout` count stays 6→6 and the pass rate looks unchanged, yet the churn block still shows the 3 fixed and
3 new by name. The churn diff needs the previous run's `metrics.json` to carry `fail_tests` (added in this
skill version); runs whose prior predates it print a one-line "populates from next run onward" notice. The
**first** run for a playlist has no prior, so it prints a
one-line "baseline" note instead — that is expected. Override discovery with `--prev <metrics.json|folder>`
or point at a different root with `--reports-root`. Do **not** add any peer/teammate comparison — the only
built-in comparison is against this folder's prior run.

**Note:** the parser writes the report to the reports root **before** the agent enrichment in step 3 (so
that synced copy still has the `[AGENT: ...]` placeholders). After you enrich the styled report in `$out`,
copy the final version over the reports-root copy so the synced report is the enriched one (see step 3).

### 3. Enrich the styled reports (this is the high-value step)

`report_styled.md` contains the deterministic numbers plus `[AGENT: ...]` placeholders. For **each**
report, replace the placeholders by reading the captured per-test `StdOut` in the TRX/console logs:

- **Comparison vs previous run** — if the auto-generated `📈 Comparison vs previous run` section is
  present, replace its `[AGENT: ...]` line with ONE short sentence summarizing the trend: net better/worse,
  the biggest mover, and whether any **executed-count change** points to a config/skip difference vs a real
  change. **Explicitly call out how many tests were FIXED vs NEWLY failing** from the ✅/🆕 churn subsection —
  especially when the pass rate is flat, because equal category counts can hide a fully churned bucket (e.g.
  timeouts fixed while a different set of tests newly times out). If a ✅ fixed test maps to a known code
  change (e.g. a merged timeout-bump PR), name it; if the 🆕 new failures share a root cause with the fixed
  ones (same flaky event-polling/latency pattern), say so. (No peer/teammate comparison — only this folder's prior run.)
- **Known issues & status** — for every failure category, give the root cause + a concrete fix. Known
  buckets and their established narratives:
  - `C2-Aad50020` (AADSTS50020): account/guest **not provisioned as external/B2B** in the Delos tenant;
    `AadTokenProvider` retries ~7× then hits the 60s NUnit timeout. Fix: onboard the affected accounts
    (pools `msg_delos/edu_guest`, `msg_delos/base_*`) as B2B users.
  - `C6-Trouter` ("not connected within timeout"): intermittent on long-poll/event tests; 10s client
    connect timeout too short under parallel load. Fix: bump to ~60s + retry once in `WorkingContextExtensions.cs`.
  - `Timeout` (uncategorized 60s/90s): dominant bucket; root cause not in the NUnit message — triage
    per-occurrence from StdOut (event polling never satisfied, sync lag, TeamsMT latency) and reclassify.
  - `C8-ChatAcl403` (ChatService `403` `AclCheckFailed` / MFE `THREADACCESSDENIED`): an admin/space-admin
    setting is **not honored** — the admin's message delete/edit is rejected (e.g.
    `ToggleAdminDeleteEnabled_AdminHonors`, `TestRestoreArchivedTeam_DeleteOthersMessageAllowedAgain_ForAdmin`).
    Owner: **ChatService / msgapi**. IcM-worthy.
  - `C9-ChatNotFound` (ChatService `404` NotFound): a msgapi GET returns 404 for a resource that should
    exist — e.g. the per-user notifications stream `GET .../v1/users/ME/conversations/48:notifications/messages`
    returns `LocationLookupFailed` / mfeDiagCode `002-THREADRESOURCENOTFOUND-404`
    (`GroupChatTests.LikeNotification` / `MentionNotification`), msgapi's MFE unable to locate
    `teamsstream_notifications_<user>@thread.v2`. Owner: **ChatService / msgapi** (they may hand off to the
    notifications-stream/Substrate pipeline if that stream is simply unprovisioned on Delos). IcM-worthy.
  - `C10-TeamsMT403` (TeamsMT `403` Forbidden): a TeamsMT/middletier roster/role op is rejected (e.g.
    `bulkUpdateRoledMembers` on guest promote/demote, `PromoteDemoteGuest_InitiatorAdmin_ThrowsException`).
    Owner: **TeamsMT / middletier**. IcM-worthy.
  - `Other`: residual **uncategorized** signatures only — open the StdOut, name the specific signature, and
    if it recurs extend `classify()` + `LABELS` with a new named bucket (as `C8`–`C10` were).
  - `C7-Scheduling` (Scheduling Service meeting-creation failure): `MeetingTests.*` fail with a
    `SchedulingException` from `POST .../teams/v1/meetings` — the **Scheduling Service** rejects/aborts
    meeting creation on Delos. Seen as `Unauthorized` (401) **or** `InternalServerError` (500, e.g. the
    Scheduler's `MiddleTierServiceClient.GetUserRegion` call failing, errorSubCode 9024). Owner is the
    Scheduling Service, **not** messaging tokens. (Historically mislabeled `C5-MsgAPI401`; the classifier now
    separates it.) Do **not** route this to the AAD/token pool owner.
  - `C5-MsgAPI401` (genuine MsgAPI token 401): only a real messaging-token failure — `<<< 401` with the
    token **kid/metadata** signature. A bare `401 Unauthorized` from another service is **not** this bucket.
  - Note `C5-MsgAPI401`, `C7-Scheduling`, and `C3-AMS` only if they actually occur (media/AMS are skipped on Delos).
- **Path to green** — order the fixes by how many tests they unblock.
- **Incident filing** — suggest an IcM **only** for genuine product/service failures — the
  `C8-ChatAcl403`, `C9-ChatNotFound`, `C10-TeamsMT403`, and `C7-Scheduling` buckets (plus any residual
  `Other`) — split by owning service (resolve owner via `enghub-resolve_service`; ownership rotates). For evidence, pull the
  **failing-request MS-CV + UTC time** straight from the report's `🔎 Failure telemetry (per test)` appendix
  (the parser extracts the correlation vector of the failing 4xx/5xx request per test). Include the MS-CV,
  UTC time, test names, Delos tenant id `4d8a5373-66c4-4716-a044-f39e72033963`, and the UTC time window
  (from the "Tests ran" line). Route AADSTS50020 to the test-pool owner (see the `Owner` attribute on the
  failing test) — it is **not** an IcM.

The reports include, for each failing test, a `🔎 Failure telemetry` appendix table
(test → category → **MS-CV of the failing request** → **UTC time**). When a CV/time could not be captured
from StdOut the cell shows `-` (common for pure NUnit timeouts that never got a service response).

**After enriching**, copy the final styled report over the reports-root skeleton so the **synced** copy is
the enriched one (the parser wrote the pre-enrichment version there). For each playlist, note the reports
root copy is now **flat** (directly under the date folder, not under a `cs3\`/`cs5\` subfolder):

```powershell
$rr = if($env:OneDrive){"$env:OneDrive\delos-test-reports"}else{'Q:\delos-test-reports'}
Copy-Item "$out\CS-3_$date.md" "$rr\$date\" -Force
Copy-Item "$out\CS-5_$date.md" "$rr\$date\" -Force
```

### 4. Present results

Post **two separate** styled reports in chat (CS-3 first, then CS-5). Lead with a one-line verdict per
playlist (best-of-3 pass rate + deterministic %) and the previous-run trend if available. Tell the user
where the artifacts live: **both styled reports sit together in the shared dated root** `$out`
(`CS-3_<yyyyMMdd>.md`, `CS-5_<yyyyMMdd>.md`, `report_cs3.md`/`report_cs5.md`, `metrics_cs3.json`/
`metrics_cs5.json`), while each playlist's raw run artifacts (`trx\`, `runner.log`, `run_meta.json`) stay in
its `$out\cs3\` / `$out\cs5\` subfolder. The reports are **also** persisted under the reports root
(`%OneDrive%\delos-test-reports\<date>\` when OneDrive is available, else `Q:\delos-test-reports\<date>\`) —
`<slug>_<date>.md` and `report_<key>.md` land **directly in that date folder** (flat, both playlists
visible together at a glance); only `metrics.json` lives one level deeper under `<date>\<key>\` since it's
machine-readable bookkeeping, not something you'd normally open. One date folder per daily run, so the
reports **and** the history sync across machines via OneDrive.

## Failure-triage classification (reference)

`parse_report.py classify()` buckets each failure from its StdOut, in this order:
`AADSTS50020 → other AADSTS → Trouter ("not connected within") → Scheduling Service
("schedulingexception" / "scheduler.communications" / "/teams/v1/meetings") → MsgAPI 401 (requires
"<<< 401" **and** a "kid"/"metadata" token signature) → AMS (asyncmediaexception) →
generic "exceeded timeout value" → ChatService ACL 403 ("aclcheckfailed" / "threadaccessdenied") →
ChatService 404 ("chatserviceresponseexception" + "received: notfound") → TeamsMT 403
("teamsmtexception" + "forbidden") → Other`. Scheduling Service failures are classified **before** MsgAPI401
so they are never mislabeled as a messaging-token failure. The three product-rejection buckets
(`C8-ChatAcl403`, `C9-ChatNotFound`, `C10-TeamsMT403`) are matched **after** the generic timeout rule so a
plain NUnit timeout is never reclassified by an incidental 4xx elsewhere in its captured StdOut. If a new
recurring signature still appears in the `Other` bucket, extend `classify()` and `LABELS` with a new named
bucket and re-run the parser.

## Notes & gotchas

- **Stale playlist entries**: FQNs in the playlist but not in the build are reported under "stale" and
  excluded from the denominator — they are not failures. Consider pruning them from the `.playlist`.
- **Not-applicable on Delos**: media/AMS, scheduled-draft, and forward-with-media tests are skipped
  (config has TODO placeholders); they show as `NotExecuted` and are excluded from the denominator.
- **Side effects**: the runner patches `MessagingE2ETests\app.config` to `AppDelos.config` and leaves it
  there. That is intended for this workflow.
- **Do not** persist the APM token anywhere — it is acquired fresh per chunk and is short-lived.
- To re-run only the reports without re-testing, skip step 1 and run step 2 against existing `trx\`.

## Running this skill on another machine (sync setup)

This personal copy of the skill lives in a **private GitHub repo**
(`LuChen-Microsoft/sovcloud-e2e-daily-skill`) and both machines use it as a **git clone**, so the skill
files sync via git and the report/comparison history syncs via **OneDrive**. To set up a new machine:

1. **Install the skill (git clone).** Clone the private repo straight into the Copilot skills folder so
   Copilot CLI auto-discovers it — no copy-paste:
   ```powershell
   git clone https://github.com/LuChen-Microsoft/sovcloud-e2e-daily-skill `
     "$env:USERPROFILE\.copilot\skills\sovcloud-e2e-daily"
   ```
   (Requires a GitHub credential with access to the private repo — e.g. `gh auth login` once.)
2. **Keep it in sync.** Git does **not** auto-sync. Before running, `git -C <skill-folder> pull`; after
   editing the skill, `git -C <skill-folder> commit`/`push`. Editing on both machines without pulling first
   causes normal git conflicts — pull first.
3. **Clone the test repo** to `Q:\repos\async_messaging_e2e-tests` (the default `-RepoRoot`; else pass
   `-RepoRoot <path>` to `Run-Playlist.ps1` and adjust the `$repo`/`--trx`/`--tests` paths).
4. **Toolchain**: .NET SDK + the **VS Test Platform** (`vstest.console.exe` — ships with Visual Studio or the
   standalone `Microsoft.TestPlatform`; the runner auto-discovers it) and **Python 3** on PATH
   (`parse_report.py` uses only the stdlib — no pip installs).
5. **Access / prerequisites** (same as the local run, see *Prerequisites* above):
   - Corp `az login` for tenant `72f988bf-86f1-41af-91ab-2d7cd011db47` (APM token source), with the current
     user in the APM pool group for Delos.
   - **VPN** to the SovCloud connected (AzureVPN) — the APIs are otherwise unreachable.
   - `dotnet nuget disable source scc` once (dead feed) and access to the `tps` NuGet feed.
6. **Reports + comparison history (OneDrive)**: `parse_report.py` defaults `--reports-root` to
   `%OneDrive%\delos-test-reports` and writes each run's **report** (`<slug>_<date>.md`, `report_<key>.md`)
   **directly under** `<reports-root>\<date>\` (flat — both playlists' reports visible together), plus
   `metrics.json` one level deeper under `<date>\<key>\`, so both the reports and the *"Comparison vs
   previous run"* trend **follow you across machines** as long as **OneDrive is signed in and synced** on
   both. If a machine has no OneDrive it falls
   back to `Q:\delos-test-reports` (local only); pass `--reports-root <path>` to override.
7. Invoke the skill the same way — trigger phrases like *"run daily sovcloud tests"* / *"run CS-3 and CS-5"*,
   or run the scripts directly as shown in the Workflow above.
