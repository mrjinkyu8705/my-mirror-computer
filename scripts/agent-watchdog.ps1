param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agent-recovery-policy.ps1')
$repo = Split-Path $PSScriptRoot -Parent
$stateDir = Join-Path $repo 'local-artifacts\operations\agent-recovery'
$stateFile = Join-Path $stateDir 'attempts.json'
$paused = Test-Path -LiteralPath (Join-Path $stateDir 'paused')
$task = Get-ScheduledTask -TaskName 'MirrorHostAgent'
$info = Get-ScheduledTaskInfo -TaskName 'MirrorHostAgent'
$now = Get-Date
$attempts = @()
if (Test-Path -LiteralPath $stateFile) {
  $attempts = @(Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json |
    Where-Object { ($now.ToUniversalTime() - [datetime]::Parse($_).ToUniversalTime()).TotalMinutes -lt 60 })
}
$needed = Test-AgentRecoveryNeeded -State ([string]$task.State) -LastResult $info.LastTaskResult `
  -LastRun $info.LastRunTime -Now $now -Paused $paused -RecentAttempts $attempts.Count
if ($CheckOnly) {
  [pscustomobject]@{ State=[string]$task.State; LastResult=$info.LastTaskResult; Paused=$paused; RecentAttempts=$attempts.Count; WouldStart=$needed }
  return
}
if (-not $needed) { return }
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
# Persist the attempt before starting, including failed starts, to bound retries.
ConvertTo-Json -InputObject @($attempts + $now.ToUniversalTime().ToString('o')) |
  Set-Content -LiteralPath $stateFile -Encoding utf8
$log = Join-Path $stateDir 'recovery.log'
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) {
  Move-Item -LiteralPath $log -Destination ($log + '.1') -Force
}
try {
  # Re-read state so a concurrent user start is left alone.
  if ((Get-ScheduledTask -TaskName 'MirrorHostAgent').State -eq 'Ready') {
    Start-ScheduledTask -TaskName 'MirrorHostAgent'
    Add-Content -LiteralPath $log -Value "$($now.ToString('o')) recovery-start previous-result=$($info.LastTaskResult)" -Encoding utf8
  }
} catch {
  Add-Content -LiteralPath $log -Value "$($now.ToString('o')) recovery-failed type=$($_.Exception.GetType().Name)" -Encoding utf8
  throw
}
