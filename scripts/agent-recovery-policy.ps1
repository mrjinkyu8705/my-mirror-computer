function Test-AgentRecoveryNeeded {
  param(
    [string]$State,
    [long]$LastResult,
    [datetime]$LastRun,
    [datetime]$Now,
    [bool]$Paused,
    [int]$RecentAttempts
  )
  # A successful/intentional stop must stay stopped. Running/disabled/queued
  # tasks are never interrupted. The normal scheduler gets five minutes first.
  return (-not $Paused -and $State -eq 'Ready' -and $LastResult -ne 0 -and
    ($Now - $LastRun).TotalMinutes -ge 5 -and $RecentAttempts -lt 3)
}
