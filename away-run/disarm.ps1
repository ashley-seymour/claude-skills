<#
.SYNOPSIS
  Disarm an away run: write STOP, unregister the away-<repo>-<date> task (or
  every away-* task with -All), and confirm none is left.
.EXAMPLE
  powershell -NoProfile -File disarm.ps1 -Repo <repo> -Date 2026-10-01
.NOTES
  ASCII only (PowerShell 5.1). Leaves the worktree and away/<date> branch for review.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string] $Repo = '',
  [string] $Date = (Get-Date -Format 'yyyy-MM-dd'),
  [switch] $All
)
$ErrorActionPreference = 'Stop'

if ($All) {
  $tasks = @(Get-ScheduledTask -TaskName 'away-*' -ErrorAction SilentlyContinue)
} else {
  if (-not $Repo) { throw 'Pass -Repo (with -Date), or -All.' }
  $Repo = (Resolve-Path $Repo).Path.Replace('\', '/')
  $repoName = Split-Path $Repo -Leaf
  $scratch = (Split-Path $Repo -Parent).Replace('\', '/') + "/$repoName-worktrees/away-$Date-run"
  if ((Test-Path $scratch) -and $PSCmdlet.ShouldProcess("$scratch/STOP", 'write STOP')) {
    Set-Content -LiteralPath "$scratch/STOP" -Value "disarmed $(Get-Date -Format 'yyyy-MM-dd HH:mm')" -Encoding ASCII
  }
  $tasks = @(Get-ScheduledTask -TaskName "away-$repoName-$Date" -ErrorAction SilentlyContinue)
  if (Test-Path "$scratch/watchdog.log") {
    Write-Host "watchdog.log tail ($scratch):"
    Get-Content -LiteralPath "$scratch/watchdog.log" -Tail 5 | ForEach-Object { Write-Host "  $_" }
  }
}

foreach ($t in $tasks) {
  if ($PSCmdlet.ShouldProcess($t.TaskName, 'Unregister-ScheduledTask')) {
    Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
    Write-Host "unregistered $($t.TaskName)"
  }
}

$left = @(Get-ScheduledTask -TaskName 'away-*' -ErrorAction SilentlyContinue)
if ($left.Count -eq 0) {
  Write-Host "Get-ScheduledTask -TaskName 'away-*': none registered"
} else {
  Write-Host "Still registered: $(($left | ForEach-Object { $_.TaskName }) -join ', ')"
  if (-not $WhatIfPreference) { exit 1 }
}

$live = @(Get-Process -Name claude -ErrorAction SilentlyContinue)
Write-Host "claude processes running: $($live.Count) (an iteration in flight ends on its own; STOP halts the loop after it)"
