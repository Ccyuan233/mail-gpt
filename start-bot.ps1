$ErrorActionPreference = 'Stop'
$mailGptState = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'data\autostart-task.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$mailGptTask = Get-ScheduledTask -TaskName $mailGptState.task_name -ErrorAction Stop
if ($mailGptTask.Actions.Count -ne 1 -or $mailGptTask.Actions[0].WorkingDirectory -ne $PSScriptRoot -or
    $mailGptTask.Actions[0].Arguments -ne $mailGptState.arguments) {
    throw 'Scheduled task identity differs from the installed bot.'
}
& (Join-Path $PSScriptRoot 'run.ps1') -Command resume
if ($LASTEXITCODE -ne 0) { throw 'Could not resume bot.' }
Start-ScheduledTask -TaskName $mailGptState.task_name
Write-Output 'Mail bot supervisor started; duplicate starts are ignored.'
