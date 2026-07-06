# sovcloud-e2e-daily (personal Copilot CLI skill)

Personal, portable copy of the **`sovcloud-e2e-daily`** GitHub Copilot CLI skill.
It runs the SovClouds **CS-3** and **CS-5** E2E test playlists against the
**Delos** cloud (`AppDelos.config`), 3 runs each, and produces a best-of-3
stability report per playlist. Both playlists' reports land in one shared dated
folder (`--report-out`) with a pinned `--date`, so a run that spans midnight
stays in a single daily folder.

This repo exists only to move the skill between my own machines — it is **not**
a team/oncall skill and is intentionally kept out of the shared test repo.

## Install on another machine

Clone this repo directly into your Copilot skills folder so Copilot CLI
auto-discovers it (no copy-paste):

```powershell
git clone https://github.com/LuChen-Microsoft/sovcloud-e2e-daily-skill `
  "$env:USERPROFILE\.copilot\skills\sovcloud-e2e-daily"
```

That gives you `~/.copilot/skills/sovcloud-e2e-daily/SKILL.md` +
`scripts/`. Start Copilot CLI and trigger it with *"run daily sovcloud tests"* /
*"run CS-3 and CS-5"*.

To update later:

```powershell
git -C "$env:USERPROFILE\.copilot\skills\sovcloud-e2e-daily" pull
```

## Contents

- `SKILL.md` — the skill definition + workflow (read this for the full run/report steps).
- `scripts/Run-Playlist.ps1` — runs one playlist N times against a config, writes TRX.
- `scripts/parse_report.py` — aggregates TRX into the best-of-N stability report
  (stdlib only; no pip installs).

## Prerequisites (see `SKILL.md` for details)

- The test repo cloned to `Q:\repos\async_messaging_e2e-tests` (or pass `-RepoRoot`).
- Corp `az login` (tenant `72f988bf-86f1-41af-91ab-2d7cd011db47`) with APM pool access; SovCloud VPN.
- `dotnet nuget disable source scc`; .NET SDK + VS Test Platform (`vstest.console.exe`); Python 3.
