param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$mailGptRoot = $PSScriptRoot
$mailGptIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$mailGptHash = [Security.Cryptography.SHA256]::Create()
try {
    $mailGptSuffix = ([BitConverter]::ToString($mailGptHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($mailGptRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 12)
} finally { $mailGptHash.Dispose() }
$mailGptTaskName = "MailGPT-$mailGptSuffix"
$mailGptPython = Join-Path $mailGptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $mailGptPython)) {
    $mailGptPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe'
}
if (-not (Test-Path -LiteralPath $mailGptPython)) {
    $mailGptPython = (Get-Command pythonw.exe -ErrorAction Stop).Source
}
$mailGptEnv = Join-Path $mailGptRoot '.env'
if (-not $Remove) {
    # Task Scheduler does not inherit the desktop application's PATH.
    $mailGptLines = [IO.File]::ReadAllLines($mailGptEnv, [Text.Encoding]::UTF8)
    $mailGptCodexLine = @($mailGptLines | Where-Object { $_ -match '^\s*CODEX_PATH=' })
    $mailGptCodexName = 'codex'
    if ($mailGptCodexLine.Count) { $mailGptCodexName = ($mailGptCodexLine[-1] -split '=', 2)[1].Trim().Trim('"').Trim("'") }
    $mailGptCodex = (Get-Command $mailGptCodexName -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    if ([IO.Path]::GetExtension($mailGptCodex) -ne '.exe') { throw 'CODEX_PATH must be a native executable.' }
    $mailGptLines = @($mailGptLines | Where-Object { $_ -notmatch '^\s*CODEX_PATH=' }) + @('CODEX_PATH=' + $mailGptCodex)
    [IO.File]::WriteAllLines($mailGptEnv, $mailGptLines, [Text.UTF8Encoding]::new($false))
    & (Join-Path $mailGptRoot 'run.ps1') -Command doctor
    if ($LASTEXITCODE -ne 0) { throw 'CLI preflight failed; autostart was not changed.' }
}
$mailGptArguments = '-m mail_gpt --env "' + $mailGptEnv + '" supervise'
$mailGptExisting = Get-ScheduledTask -TaskName $mailGptTaskName -ErrorAction SilentlyContinue
if ($mailGptExisting -and ($mailGptExisting.Actions.Count -ne 1 -or
    $mailGptExisting.Actions[0].WorkingDirectory -ne $mailGptRoot -or
    $mailGptExisting.Actions[0].Arguments -ne $mailGptArguments)) {
    throw 'Task name belongs to a different action; refusing to overwrite or remove it.'
}
if ($Remove) {
    & (Join-Path $mailGptRoot 'run.ps1') -Command pause
    if ($LASTEXITCODE -ne 0) { throw 'Could not request a safe pause.' }
    if ($mailGptExisting) { Unregister-ScheduledTask -TaskName $mailGptTaskName -Confirm:$false }
    Write-Output 'Autostart removed. Current mail turn may finish before the bot stops.'
    exit 0
}
$mailGptAction = New-ScheduledTaskAction -Execute $mailGptPython -Argument $mailGptArguments -WorkingDirectory $mailGptRoot
$mailGptLogon = New-ScheduledTaskTrigger -AtLogOn -User $mailGptIdentity.Name
$mailGptRetry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
$mailGptSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$mailGptPrincipal = New-ScheduledTaskPrincipal -UserId $mailGptIdentity.Name -LogonType Interactive -RunLevel Limited
$mailGptTask = New-ScheduledTask -Action $mailGptAction -Trigger @($mailGptLogon, $mailGptRetry) -Settings $mailGptSettings -Principal $mailGptPrincipal -Description 'Private Mail GPT supervisor. Starts after Windows login; checks again every 5 minutes. Pause is persistent. No Windows password stored.'
Register-ScheduledTask -TaskName $mailGptTaskName -InputObject $mailGptTask -Force | Out-Null
[ordered]@{task_name=$mailGptTaskName; working_directory=$mailGptRoot; executable=$mailGptPython; arguments=$mailGptArguments; installed_at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $mailGptRoot 'data\autostart-task.json') -Encoding UTF8
Write-Output "Installed $mailGptTaskName. Starts after Windows login; no password stored."
Write-Output 'Run start-bot.ps1 to resume and start it now.'
