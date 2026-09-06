$ErrorActionPreference = 'Stop'
$mailGptStatePath = Join-Path $PSScriptRoot 'data\worker-process.json'
$mailGptState = Get-Content -LiteralPath $mailGptStatePath -Raw | ConvertFrom-Json
$mailGptWorker = Get-Process -Id $mailGptState.pid -ErrorAction SilentlyContinue
if (-not $mailGptWorker) {
    Write-Output 'Mail bot is already stopped.'
    exit 0
}
if (-not $mailGptState.start_ticks -or
    $mailGptWorker.StartTime.ToUniversalTime().Ticks.ToString() -ne $mailGptState.start_ticks -or
    $mailGptWorker.Path -ne $mailGptState.executable) {
    throw 'Process identity changed; refusing to stop an unrelated process.'
}
Stop-Process -InputObject $mailGptWorker
Write-Output 'Mail bot stopped. Interrupted requests may need review before restarting.'
