#!/usr/bin/env pwsh
# Sutando task watcher for Windows - PowerShell twin of src/watch-tasks-stream.sh.
#
# Watches $SUTANDO_WORKSPACE/tasks/ and emits "TASK_FILE: <name>" per new file.
# Uses FileSystemWatcher (CLR) - no fswatch dependency required on Windows.
#
# Usage:
#   pwsh -File src/watch-tasks-stream.ps1

if ($env:SUTANDO_WORKSPACE) {
    $WORKSPACE = $env:SUTANDO_WORKSPACE -replace '^~', $HOME
} else {
    $WORKSPACE = Join-Path $HOME '.sutando\workspace'
}

$TASKS = Join-Path $WORKSPACE 'tasks'
New-Item -ItemType Directory -Force -Path $TASKS | Out-Null

# Write a PID sentinel so startup.ps1 can detect & reap a stale watcher.
$pidFile = Join-Path $WORKSPACE 'state\watch-tasks-stream.pid'
New-Item -ItemType Directory -Force -Path (Split-Path $pidFile) | Out-Null
Set-Content -Path $pidFile -Value $PID -NoNewline

# Emit any tasks already on disk before we start watching - matches the macOS
# behavior where startup.sh races vs. tasks landing during boot.
Get-ChildItem -Path $TASKS -File -Filter 'task-*.txt' | ForEach-Object {
    Write-Output "TASK_FILE: $($_.Name)"
}

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $TASKS
$watcher.Filter = 'task-*.txt'
$watcher.IncludeSubdirectories = $false
$watcher.EnableRaisingEvents = $true
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite

$action = {
    $name = $Event.SourceEventArgs.Name
    Write-Output "TASK_FILE: $name"
}

Register-ObjectEvent $watcher 'Created' -Action $action | Out-Null
Register-ObjectEvent $watcher 'Renamed' -Action $action | Out-Null

try {
    while ($true) { Start-Sleep -Seconds 60 }
} finally {
    $watcher.EnableRaisingEvents = $false
    $watcher.Dispose()
    if (Test-Path $pidFile) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
}
