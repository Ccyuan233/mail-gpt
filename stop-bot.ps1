$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'run.ps1') -Command pause
if ($LASTEXITCODE -ne 0) { throw 'Could not pause bot.' }
Write-Output 'Pause saved. The current request finishes safely; scheduled retries remain paused until start-bot.ps1.'
