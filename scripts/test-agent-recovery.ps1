$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'agent-recovery-policy.ps1')
$now = Get-Date
$base = @{ State='Ready'; LastResult=4294967295; LastRun=$now.AddMinutes(-10); Now=$now; Paused=$false; RecentAttempts=0 }
function Assert-Recovery([hashtable]$Overrides, [bool]$Expected) {
  $values = $base.Clone()
  foreach ($key in $Overrides.Keys) { $values[$key]=$Overrides[$key] }
  if ((Test-AgentRecoveryNeeded @values) -ne $Expected) { throw "Recovery policy failed: $($Overrides.Keys -join ',')" }
}
Assert-Recovery @{} $true
Assert-Recovery @{State='Running'} $false
Assert-Recovery @{State='Disabled'} $false
Assert-Recovery @{State='Queued'} $false
Assert-Recovery @{LastResult=0} $false
Assert-Recovery @{Paused=$true} $false
Assert-Recovery @{RecentAttempts=3} $false
Assert-Recovery @{LastRun=$now.AddMinutes(-4)} $false
Assert-Recovery @{RecentAttempts=2;LastRun=$now.AddMinutes(-5)} $true
Write-Output 'Recovery policy: 9 cases passed (no service actions).'
