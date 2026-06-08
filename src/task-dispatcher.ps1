#!/usr/bin/env pwsh
# Sutando task dispatcher (Windows) - push-driven task processor.
#
# Solves the latency problem Windows hits because Claude Code 2.x removed the
# Monitor tool. The long-running sutando-core agent only wakes on cron ticks;
# between ticks, new task files sit in tasks/ until the next fire. This script
# is the workaround: it watches tasks/ continuously, and for each new task
# invokes `claude --print` as a one-shot subprocess. Each task completes in
# seconds, independent of cron timing.
#
# Roles:
#   sutando-core (long-running TUI) -> autonomous proactive-loop work, cron
#                                       jobs, health checks. Picks up tasks
#                                       too, but on its own schedule.
#   task-dispatcher (this script)   -> low-latency owner-task processing.
#
# Both can read the same task file; we use a per-file lock (claim/rename
# pattern) to make sure exactly one of them processes any given task.
#
# Usage:
#   pwsh -File src/task-dispatcher.ps1               # foreground (for debugging)
#   pwsh -File src/task-dispatcher.ps1 -Background   # detach to a new window
#
# Output goes to <workspace>/logs/task-dispatcher.log.

[CmdletBinding()]
param([switch]$Background)

$ErrorActionPreference = 'Continue'
$REPO = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Resolve workspace - matches startup.ps1 / workspace_default.ps1 contract.
if ($env:SUTANDO_WORKSPACE) {
    $WORKSPACE = $env:SUTANDO_WORKSPACE -replace '^~', $HOME
} else {
    $WORKSPACE = Join-Path $HOME '.sutando\workspace'
}
$TASKS    = Join-Path $WORKSPACE 'tasks'
$RESULTS  = Join-Path $WORKSPACE 'results'
$ARCHIVE  = Join-Path $TASKS 'archive'
$LOGS     = Join-Path $WORKSPACE 'logs'
$LOGFILE  = Join-Path $LOGS 'task-dispatcher.log'
foreach ($d in $TASKS, $RESULTS, $ARCHIVE, $LOGS) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# --- Background detach -------------------------------------------------------
if ($Background) {
    # Self-relaunch in a hidden window; parent returns immediately.
    $psExe = (Get-Command pwsh -ErrorAction SilentlyContinue) ?? (Get-Command powershell -ErrorAction SilentlyContinue)
    Start-Process -FilePath $psExe.Source -ArgumentList @(
        '-NoProfile', '-File', $PSCommandPath
    ) -WindowStyle Hidden | Out-Null
    Write-Host "task-dispatcher launched in background. Log: $LOGFILE"
    exit 0
}

# --- PID sentinel + single-instance guard -----------------------------------
$pidFile = Join-Path $WORKSPACE 'state\task-dispatcher.pid'
New-Item -ItemType Directory -Force -Path (Split-Path $pidFile) | Out-Null
if (Test-Path $pidFile) {
    $stalePid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($stalePid -and (Get-Process -Id $stalePid -ErrorAction SilentlyContinue)) {
        Write-Host "task-dispatcher already running (pid $stalePid). Exiting."
        exit 0
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}
Set-Content -Path $pidFile -Value $PID -NoNewline

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LOGFILE -Value $line
}

# Locate claude.exe once.
$claudeCmd = Get-Command claude -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.ToLower().EndsWith('.exe') } |
    Select-Object -First 1
if (-not $claudeCmd) {
    Log "ERROR: claude.exe not found on PATH. Install Claude Code and retry."
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 1
}
$CLAUDE = $claudeCmd.Source
Log "task-dispatcher started. claude=$CLAUDE workspace=$WORKSPACE"

# --- Per-task processing -----------------------------------------------------

# Parse the task file body (header lines `key: value` + a `task: <body>` line
# that may span multiple lines until the next `--- recent voice transcript ---`
# marker or EOF). Returns the user-facing prompt text.
function Get-TaskPrompt($path) {
    $content = Get-Content -Raw -Path $path -ErrorAction SilentlyContinue
    if (-not $content) { return $null }
    # Capture everything after `task: ` until the transcript marker / blank line at EOF.
    if ($content -match '(?ms)^task:\s*(.+?)(?:\r?\n---|\z)') {
        return $Matches[1].Trim()
    }
    return $content.Trim()
}

# Claim a task by renaming it to `.processing` - atomic on NTFS. Returns the
# new path on success, $null if some other process already claimed it.
function Claim-Task($file) {
    $claim = "$($file.FullName).processing"
    try {
        Move-Item -Path $file.FullName -Destination $claim -ErrorAction Stop
        return $claim
    } catch {
        return $null
    }
}

function Process-Task($claimedPath, $taskId) {
    $prompt = Get-TaskPrompt $claimedPath
    if (-not $prompt) {
        Log "${taskId}: empty prompt, skipping"
        return
    }
    Log "${taskId}: processing ($(($prompt -split '\r?\n')[0].Substring(0, [Math]::Min(60, ($prompt -split '\r?\n')[0].Length))))"

    # Send the task body to claude --print via stdin. --dangerously-skip-permissions
    # so the agent doesn't pause asking for tool-call approvals - matches the
    # interactive sutando-core posture.
    $stderrPath = "$claimedPath.stderr"
    $result = $prompt | & $CLAUDE --print --dangerously-skip-permissions 2>$stderrPath
    $exitCode = $LASTEXITCODE
    $stderr = Get-Content -Raw -Path $stderrPath -ErrorAction SilentlyContinue
    Remove-Item -Path $stderrPath -ErrorAction SilentlyContinue

    if ($exitCode -ne 0) {
        $errMsg = if ($stderr) { $stderr.Trim() } else { "claude exited $exitCode" }
        Log "${taskId}: FAILED (exit=$exitCode): $errMsg"
        $result = "task-dispatcher: claude --print exited $exitCode. stderr:`n$errMsg"
    }

    # Write result. Bridges/web UI watch results/task-<id>.txt.
    $resultFile = Join-Path $RESULTS "$taskId.txt"
    Set-Content -Path $resultFile -Value $result -NoNewline
    Log "${taskId}: done -> $resultFile"

    # Archive the task file so we don't reprocess it on rescan.
    $archived = Join-Path $ARCHIVE "$taskId.txt"
    try {
        Move-Item -Path $claimedPath -Destination $archived -Force
    } catch {
        Log "${taskId}: archive failed: $_"
        # Last resort: delete so we don't loop on it.
        Remove-Item -Path $claimedPath -ErrorAction SilentlyContinue
    }
}

# --- Watch loop --------------------------------------------------------------

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $TASKS
$watcher.Filter = 'task-*.txt'
$watcher.IncludeSubdirectories = $false
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::CreationTime

# Drain any tasks already present on disk before we start watching.
function Drain-Pending {
    Get-ChildItem -Path $TASKS -File -Filter 'task-*.txt' -ErrorAction SilentlyContinue | ForEach-Object {
        $claimed = Claim-Task $_
        if (-not $claimed) { return }
        $taskId = $_.BaseName
        try {
            Process-Task $claimed $taskId
        } catch {
            Log "${taskId}: uncaught exception: $_"
        }
    }
}

Drain-Pending

try {
    while ($true) {
        # WaitForChanged blocks until an FS event or timeout (2s). Timeout
        # exists so we can drain anything the watcher missed in a burst.
        $r = $watcher.WaitForChanged(
            [System.IO.WatcherChangeTypes]::Created -bor [System.IO.WatcherChangeTypes]::Renamed -bor [System.IO.WatcherChangeTypes]::Changed,
            2000
        )
        # Tiny settle delay so the producer can finish writing before we read.
        # Tasks are usually <1KB so 50ms is plenty; bump if you see truncated reads.
        if (-not $r.TimedOut) { Start-Sleep -Milliseconds 50 }
        Drain-Pending
    }
} finally {
    $watcher.Dispose()
    if (Test-Path $pidFile) { Remove-Item $pidFile -ErrorAction SilentlyContinue }
    Log "task-dispatcher stopped."
}
