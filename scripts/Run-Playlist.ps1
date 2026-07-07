<#
.SYNOPSIS
  Run a SovClouds E2E test playlist N times against a given config, producing TRX results.

.DESCRIPTION
  Generalized runner used by the 'sovcloud-e2e-daily' skill. It:
    1. Patches MessagingE2ETests\app.config to point at -ConfigName (unless -SkipConfigPatch).
    2. Builds MessagingE2ETests.csproj (unless -SkipBuild).
    3. Extracts the test FQNs from the .playlist (TestWithNormalizedFullyQualifiedName).
    4. Enumerates the ACTUAL tests in the built DLL (/ListFullyQualifiedTests) so parameterized
       (data-driven) test cases are matched exactly - no '~' contains heuristic, no prefix collisions.
    5. Bin-packs tests into chunks (<= -MaxChunk, never splitting a class) so each vstest process
       finishes well within the ~1h APM token lifetime.
    6. For each run/chunk: refreshes APM_BEARER_TOKEN, runs vstest with a TRX logger into <OutDir>\trx.

  Requires: corp `az login` already done once; VPN access to the SovCloud; NuGet 'scc' source disabled
  (the repo only needs 'tps'); Visual Studio test platform installed.

.EXAMPLE
  .\Run-Playlist.ps1 -PlaylistPath "Q:\repos\async_messaging_e2e-tests\SovClouds CS-3.playlist" `
                     -ConfigName AppDelos.config -OutDir C:\temp\cs3 -Runs 3
#>
param(
  [Parameter(Mandatory)][string]$PlaylistPath,
  [Parameter(Mandatory)][string]$ConfigName,                 # e.g. AppDelos.config
  [Parameter(Mandatory)][string]$OutDir,                     # where trx\ + runner.log land
  [int]$Runs = 3,
  [int]$MaxChunk = 25,
  [string]$RepoRoot = 'Q:\repos\async_messaging_e2e-tests',
  [string]$ApmScope = 'api://d25164f2-e94b-44ea-9ddb-f858dfedb399/.default',
  [string]$ApmTenant = '72f988bf-86f1-41af-91ab-2d7cd011db47',
  [switch]$SkipBuild,
  [switch]$SkipConfigPatch
)
# NOTE: intentionally 'Continue', not 'Stop'. vstest.console.exe always writes a "Test Run Failed."
# banner to stderr whenever >=1 test in a chunk fails (normal/expected - see SKILL.md). PowerShell
# treats native-command stderr output as an ErrorRecord, and 'Stop' promotes that into a terminating
# exception that would abort the whole multi-hour run at the first failing test. Every step that must
# hard-stop on real failure (build, playlist extraction) already does an explicit $LASTEXITCODE/throw
# check below, so 'Continue' here does not mask genuine harness errors.
$ErrorActionPreference = 'Continue'

$proj     = Join-Path $RepoRoot 'MessagingE2ETests\MessagingE2ETests.csproj'
$appCfg   = Join-Path $RepoRoot 'MessagingE2ETests\app.config'
$binDir   = Join-Path $RepoRoot 'MessagingE2ETests\bin\Debug\net472'
$dll      = Join-Path $binDir 'MessagingE2ETests.dll'
$settings = Join-Path $binDir 'vsts.runsettings'

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$trxDir = Join-Path $OutDir 'trx'
New-Item -ItemType Directory -Force -Path $trxDir | Out-Null
$log = Join-Path $OutDir 'runner.log'
function Log($m){ $ts=(Get-Date).ToString('s'); "$ts $m" | Out-File -FilePath $log -Append -Encoding utf8; Write-Host "$ts $m" }

# --- locate vstest.console.exe across VS editions/years ---
function Find-VsTest {
  $roots = @('C:\Program Files\Microsoft Visual Studio','C:\Program Files (x86)\Microsoft Visual Studio')
  foreach($r in $roots){
    if(Test-Path $r){
      $hit = Get-ChildItem -Path $r -Recurse -Filter 'vstest.console.exe' -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match 'TestPlatform' } | Select-Object -First 1
      if($hit){ return $hit.FullName }
    }
  }
  $cmd = Get-Command vstest.console.exe -ErrorAction SilentlyContinue
  if($cmd){ return $cmd.Source }
  throw 'vstest.console.exe not found. Install the VS Test Platform.'
}
$vstest = Find-VsTest
Log "vstest: $vstest"

function Refresh-Token {
  for($i=0;$i -lt 4;$i++){
    $t = az account get-access-token --scope $ApmScope --tenant $ApmTenant --query accessToken -o tsv 2>$null
    if($t){ return $t.Trim() }
    Start-Sleep 5
  }
  throw 'APM token refresh failed (is `az login` done for the corp tenant?)'
}

# --- 1. patch app.config ---
if(-not $SkipConfigPatch){
  $patcher = Join-Path $RepoRoot 'Tools\PatchConfig.ps1'
  if(Test-Path $patcher){
    & $patcher -testConfigPath $appCfg -value "Configs\$ConfigName"
  } else {
    $xml = Get-Content $appCfg -Raw
    $xml = [regex]::Replace($xml, 'file="Configs\\[^"]+\.config"', "file=`"Configs\$ConfigName`"")
    Set-Content -Path $appCfg -Value $xml -Encoding utf8
  }
  Log "app.config -> Configs\$ConfigName"
}

# --- 2. build ---
if(-not $SkipBuild){
  Log "building $proj ..."
  dotnet build $proj -c Debug -v minimal 2>&1 | Tee-Object -FilePath (Join-Path $OutDir 'build.log') | Out-Null
  if($LASTEXITCODE -ne 0){ throw "build failed (see $OutDir\build.log)" }
  Log "build ok"
}
if(-not (Test-Path $dll)){ throw "test dll missing: $dll" }

# --- 3. extract wanted FQNs from playlist ---
$wanted = Select-String -Path $PlaylistPath -Pattern 'Name="TestWithNormalizedFullyQualifiedName"\s+Value="([^"]+)"' -AllMatches |
          ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
if(-not $wanted){ throw "no tests extracted from $PlaylistPath" }
$wanted | Set-Content -Path (Join-Path $OutDir 'tests.txt') -Encoding utf8
Log "wanted tests: $($wanted.Count)"

# --- 4. enumerate actual tests in the dll ---
$actualFile = Join-Path $OutDir 'actual_tests.txt'
& $vstest $dll /ListFullyQualifiedTests /ListTestsTargetPath:$actualFile *> (Join-Path $OutDir 'list.log')
$actual = @()
if(Test-Path $actualFile){ $actual = Get-Content $actualFile | Where-Object { $_ } }
Log "actual tests in build: $($actual.Count)"
$actualSet = [System.Collections.Generic.HashSet[string]]::new()
foreach($a in $actual){ [void]$actualSet.Add($a) }

# vstest TestCaseFilter treats ( ) & | = ! ~ \ as operators; data-driven case
# names like Foo(True) or Bar(plain bizChat message) contain these and must be
# escaped with a backslash or the WHOLE chunk filter is rejected ("Missing Operator").
function Escape-FilterValue($v){
  return ($v -replace '([\\()&|=!~])', '\$1')
}

# resolve each wanted FQN to one or more EXACT filter terms
function Resolve-Terms($t){
  if($actualSet.Contains($t)){ return @("FullyQualifiedName=$(Escape-FilterValue $t)") }
  $params = $actual | Where-Object { $_.StartsWith($t + '(') }   # data-driven cases
  if($params){ return ($params | ForEach-Object { "FullyQualifiedName=$(Escape-FilterValue $_)" }) }
  return @()   # not present in this build (stale)
}

# --- 5. chunk by class (all but last FQN segment), bin-pack <= MaxChunk ---
$byClass = [ordered]@{}
foreach($t in $wanted){
  $p = $t.Split('.'); $cls = ($p[0..($p.Length-2)] -join '.')
  if(-not $byClass.Contains($cls)){ $byClass[$cls] = New-Object System.Collections.ArrayList }
  [void]$byClass[$cls].Add($t)
}
$chunks = New-Object System.Collections.ArrayList
$cur = New-Object System.Collections.ArrayList
foreach($cls in $byClass.Keys){
  $clsTests = $byClass[$cls]
  if($cur.Count -gt 0 -and ($cur.Count + $clsTests.Count) -gt $MaxChunk){
    [void]$chunks.Add($cur); $cur = New-Object System.Collections.ArrayList
  }
  foreach($t in $clsTests){ [void]$cur.Add($t) }
}
if($cur.Count -gt 0){ [void]$chunks.Add($cur) }

# --- 6. run ---
$startTime = Get-Date
Log "START playlist=$([IO.Path]::GetFileName($PlaylistPath)) config=$ConfigName total=$($wanted.Count) classes=$($byClass.Count) chunks=$($chunks.Count) runs=$Runs"
for($r=1;$r -le $Runs;$r++){
  Log "=== RUN $r START ==="
  for($c=0;$c -lt $chunks.Count;$c++){
    $terms = @(); foreach($t in $chunks[$c]){ $terms += Resolve-Terms $t }
    $terms = $terms | Sort-Object -Unique
    if(-not $terms){ Log "RUN $r CHUNK $c skipped (no resolvable tests)"; continue }
    $filter = ($terms -join '|')
    try { $env:APM_BEARER_TOKEN = Refresh-Token } catch { Log "TOKEN FAIL run$r chunk$c : $_"; continue }
    $cc = '{0:D2}' -f $c
    $trxName  = "run${r}_chunk${cc}.trx"
    $chunkLog = Join-Path $trxDir "run${r}_chunk${cc}.console.log"
    Log "RUN $r CHUNK $c ($($terms.Count) terms) -> $trxName"
    & $vstest $dll /Settings:$settings /TestCaseFilter:$filter /Logger:"trx;LogFileName=$trxName" /ResultsDirectory:$trxDir *> $chunkLog
    Log "RUN $r CHUNK $c done exit=$LASTEXITCODE"
  }
  Log "=== RUN $r DONE ==="
}
$endTime = Get-Date
$meta = [ordered]@{
  playlist        = [IO.Path]::GetFileName($PlaylistPath)
  config          = $ConfigName
  runs            = $Runs
  startTime       = $startTime.ToString('o')          # ISO-8601 with local UTC offset
  endTime         = $endTime.ToString('o')
  timezone        = [System.TimeZoneInfo]::Local.Id
  utcOffset       = $startTime.ToString('zzz')
  durationMinutes = [math]::Round(($endTime - $startTime).TotalMinutes, 1)
}
$meta | ConvertTo-Json | Set-Content -Path (Join-Path $OutDir 'run_meta.json') -Encoding utf8
Log "ALL DONE (start=$($meta.startTime) end=$($meta.endTime) tz=$($meta.timezone) durMin=$($meta.durationMinutes))"
