$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$original = Get-ScheduledTask -TaskName 'MirrorHostAgent'
# The watchdog only starts an existing task; input injection remains elevated
# in MirrorHostAgent. The observer itself needs no administrator privileges.
$principal = New-ScheduledTaskPrincipal -UserId $original.Principal.UserId -LogonType Interactive -RunLevel Limited
# A console-subsystem executable can flash before -WindowStyle is processed.
# wscript starts PowerShell hidden from creation and propagates its exit code.
$action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') `
  -Argument "//B //NoLogo `"$(Join-Path $PSScriptRoot 'agent-watchdog-hidden.vbs')`"" `
  -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'MirrorHostAgentWatchdog' -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null
Write-Output 'MirrorHostAgentWatchdog installed; existing agent was not restarted.'
