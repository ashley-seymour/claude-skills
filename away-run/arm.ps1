<#
.SYNOPSIS
  Arm an away run: cut the away/<date> worktree, seed the run folder, run the
  config's bootstrap and wait for it, stage the watchdog in scratch, and
  register the repeating scheduled task away-<repo>-<date>.
.EXAMPLE
  powershell -NoProfile -File arm.ps1 -Repo <repo> -StopAt "2026-10-01 07:00" -Queue <queue.md> -WhatIf
.NOTES
  ASCII only (PowerShell 5.1). Always run with -WhatIf first. The run uses
  --permission-mode bypassPermissions; tell the operator before arming.
  Reads <repo>/.claude/project-workflow.json if present: projects_dir (default
  my-projects; the run folder defaults to <projects_dir>/away/<date>) and
  bootstrap (argv list or null; {worktree} in any element becomes the worktree
  path; run with cwd = <repo>; a non-zero exit refuses before any task exists).
  arm refuses, before creating anything, a bootstrap that is not a JSON list
  (a single string would run as one executable name) or that has an element
  that is not a string, is empty, or contains a double quote (PowerShell 5.1
  drops empty native arguments and mangles embedded quotes); and a
  projects_dir that is not a non-empty string.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [Parameter(Mandatory = $true)] [string] $Repo,
  [Parameter(Mandatory = $true)] [string] $StopAt,
  [Parameter(Mandatory = $true)] [string] $Queue,
  [string] $Date = (Get-Date -Format 'yyyy-MM-dd'),
  [string] $RunFolder = '',
  [int] $FirstRunInMin = 10,
  [int] $IntervalMin = 15,
  [int] $MaxTurns = 150,
  [int] $BudgetUsd = 25,
  [int] $ReserveMin = 30,
  [int] $WindowCapUsd = 40,
  [int] $LeaseMin = 90,
  [string] $Bash = 'C:/Program Files/Git/bin/bash.exe',
  [switch] $WakeToRun,
  [switch] $NoRegister   # stage everything but register no task (supervised foreground run)
)
$ErrorActionPreference = 'Stop'

function Fwd([string] $p) { return $p.Replace('\', '/') }
function Write-Lf([string] $path, [string] $text) {
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($path, $text.Replace("`r`n", "`n"), $enc)
}

$skill = Fwd $PSScriptRoot
$Repo = Fwd (Resolve-Path $Repo).Path
$repoName = Split-Path $Repo -Leaf
$wtRoot = (Fwd (Split-Path $Repo -Parent)) + "/$repoName-worktrees"
$wt = "$wtRoot/away-$Date"
$scratch = "$wtRoot/away-$Date-run"
$branch = "away/$Date"
$task = "away-$repoName-$Date"

# Repo config (optional): projects_dir and bootstrap; other keys are not arm's.
$projectsDir = 'my-projects'
$bootstrap = @()
$cfgPath = "$Repo/.claude/project-workflow.json"
if (Test-Path -LiteralPath $cfgPath) {
  $cfg = [System.IO.File]::ReadAllText($cfgPath) | ConvertFrom-Json
  if ($cfg.PSObject.Properties['projects_dir'] -and $null -ne $cfg.projects_dir) {
    if (-not ($cfg.projects_dir -is [string]) -or -not $cfg.projects_dir) {
      throw "projects_dir in $cfgPath must be a non-empty string."
    }
    $projectsDir = $cfg.projects_dir
  }
  if ($cfg.PSObject.Properties['bootstrap'] -and $null -ne $cfg.bootstrap) {
    # A JSON string would run as one executable name, and PowerShell 5.1 drops empty
    # native arguments and mangles embedded double quotes: refuse all three.
    if (-not ($cfg.bootstrap -is [System.Array])) {
      throw "bootstrap in $cfgPath must be a JSON list of strings (argv) or null, not a single value."
    }
    foreach ($a in $cfg.bootstrap) {
      if (-not ($a -is [string]) -or $a -eq '' -or $a.Contains('"')) {
        throw "bootstrap in $cfgPath has an element that is not a string, is empty, or contains a double quote: [$a]"
      }
    }
    $bootstrap = @($cfg.bootstrap)
    if ($bootstrap.Count -eq 0) { throw "bootstrap in $cfgPath is an empty list; use null for none." }
  }
}
if (-not $RunFolder) { $RunFolder = "$($projectsDir.TrimEnd('/'))/away/$Date" }
$runDir = "$wt/$RunFolder"
$stop = [datetime]::ParseExact($StopAt, 'yyyy-MM-dd HH:mm', $null)
$first = (Get-Date).AddMinutes($FirstRunInMin)

# Preconditions: refuse rather than overwrite.
if ($stop -le $first) { throw "StopAt $StopAt is not after the first firing ($first)." }
if (-not (Test-Path $Queue)) { throw "Queue file not found: $Queue" }
if (-not (Select-String -Path $Queue -Pattern '^\|\s*id\s*\|' -Quiet)) { throw "Queue has no '| id |' table header: $Queue" }
if (Test-Path $wt) { throw "Worktree path exists: $wt" }
if (Test-Path $scratch) { throw "Scratch path exists: $scratch" }
& git -C $Repo rev-parse --verify --quiet "refs/heads/$branch" | Out-Null
if ($LASTEXITCODE -eq 0) { throw "Branch exists: $branch" }
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { throw "Task exists: $task" }
if (-not (Test-Path $Bash)) { throw "Git Bash not found: $Bash (pass -Bash)" }
# Resolve through a login bash: PowerShell finds the npm claude.ps1 shim, which bash cannot run.
$claude = (& $Bash -lc 'command -v claude' | Select-Object -First 1)
$python = (& $Bash -lc 'command -v python' | Select-Object -First 1)
if (-not $claude -or -not $python) { throw "claude or python not on the login-bash PATH" }

Write-Host "away-run: $task"
Write-Host "  worktree $wt on $branch (from main)"
Write-Host "  run dir  $runDir"
Write-Host "  scratch  $scratch"
Write-Host "  bootstrap $(if ($bootstrap.Count -gt 0) { $bootstrap -join ' ' } else { 'none' })"
Write-Host "  firings  every $IntervalMin min from $($first.ToString('yyyy-MM-dd HH:mm')) to $StopAt"
Write-Host "  POSTURE: iterations run with --permission-mode bypassPermissions."

if ($PSCmdlet.ShouldProcess($wt, "git worktree add -b $branch main")) {
  & git -C $Repo worktree add $wt -b $branch main
  if ($LASTEXITCODE -ne 0) { throw "git worktree add failed" }
}

if ($PSCmdlet.ShouldProcess($runDir, 'seed run folder from templates and the queue')) {
  New-Item -ItemType Directory -Force -Path $runDir | Out-Null
  Copy-Item -LiteralPath $Queue -Destination "$runDir/queue.md"
  foreach ($f in 'decisions.md', 'log.md') {
    $t = [System.IO.File]::ReadAllText("$skill/templates/$f").Replace('<date>', $Date)
    Write-Lf "$runDir/$f" $t
  }
  & git -C $wt add -- "$RunFolder/queue.md" "$RunFolder/decisions.md" "$RunFolder/log.md"
  & git -C $wt commit -q -m "chore(away): arm $Date run folder"
  if ($LASTEXITCODE -ne 0) { throw "commit of the run folder failed" }
}

# Bootstrap before the task exists, so the first firing never meets a half-built worktree.
if ($bootstrap.Count -gt 0) {
  $argv = @($bootstrap | ForEach-Object { $_.Replace('{worktree}', $wt) })
  if ($PSCmdlet.ShouldProcess($wt, "bootstrap (cwd $Repo): $($argv -join ' ')")) {
    $exe = $argv[0]
    $rest = @($argv | Select-Object -Skip 1)
    Push-Location -LiteralPath $Repo
    try {
      $global:LASTEXITCODE = 0
      & $exe @rest
      $rc = $LASTEXITCODE
    } finally {
      Pop-Location
    }
    if ($rc -ne 0) {
      throw "bootstrap exited $rc; no task registered. Worktree $wt and branch $branch are left for inspection."
    }
  }
}

if ($PSCmdlet.ShouldProcess($scratch, 'stage watchdog.sh, entry-prompt.md and away.env')) {
  New-Item -ItemType Directory -Force -Path $scratch | Out-Null
  Copy-Item -LiteralPath "$skill/watchdog.sh" -Destination "$scratch/watchdog.sh"
  Copy-Item -LiteralPath "$skill/templates/entry-prompt.md" -Destination "$scratch/entry-prompt.md"
  $envText = @(
    "WORKTREE='$wt'", "RUN_DIR='$runDir'", "SCRATCH='$scratch'", "STOP_AT='$StopAt'",
    "MAX_TURNS=$MaxTurns", "BUDGET_USD=$BudgetUsd", "RESERVE_MIN=$ReserveMin",
    "WINDOW_CAP_USD=$WindowCapUsd", "LEASE_MIN=$LeaseMin",
    "CLAUDE='$claude'", "PY='$python'", ''
  ) -join "`n"
  Write-Lf "$scratch/away.env" $envText
}

$action = New-ScheduledTaskAction -Execute $Bash -Argument ('-l "{0}/watchdog.sh" "{0}/away.env"' -f $scratch)
$trigger = New-ScheduledTaskTrigger -Once -At $first `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMin) -RepetitionDuration ($stop - $first)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun:$WakeToRun `
  -ExecutionTimeLimit (($stop - $first) + (New-TimeSpan -Hours 1))
if ($NoRegister) {
  Write-Host "No task registered. Run it in the foreground from Git Bash:"
  Write-Host "  bash `"$scratch/watchdog.sh`" `"$scratch/away.env`""
} elseif ($PSCmdlet.ShouldProcess($task, 'Register-ScheduledTask')) {
  Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
    -Description "away-run watchdog for $repoName until $StopAt" | Out-Null
  Get-ScheduledTask -TaskName $task | Format-Table TaskName, State -AutoSize
}

if ($bootstrap.Count -eq 0) { Write-Host "No bootstrap configured ($cfgPath key 'bootstrap')." }
Write-Host "Disarm: powershell -NoProfile -File `"$skill/disarm.ps1`" -Repo $Repo -Date $Date"
