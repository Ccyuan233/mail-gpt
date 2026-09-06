param(
    [ValidateSet('doctor', 'login', 'smoke', 'run', 'review')]
    [string]$Command = 'doctor',
    [switch]$Offline,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$mailGptPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $mailGptPython)) {
    $mailGptPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $mailGptPython)) {
    $mailGptPython = (Get-Command python -ErrorAction Stop).Source
}
$mailGptArgs = @('-m', 'mail_gpt', $Command)
if ($Offline) { $mailGptArgs += '--offline' }
if ($Once) { $mailGptArgs += '--once' }
& $mailGptPython @mailGptArgs
exit $LASTEXITCODE
