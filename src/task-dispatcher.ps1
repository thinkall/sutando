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

# Parse a `key: value` header line from the task body. Returns the trimmed
# value (or $null). Used to extract channel_id so each surface gets its own
# resumable claude session. Stops scanning at the first `task:` line so a
# multi-line task body whose content includes e.g. `channel_id: forge` can
# not forge headers - mirrors the TS-side stop-at-task convention from
# PR #982 (`task-bridge.ts:_isVoiceTask`).
function Get-TaskHeader($path, $key) {
    $content = Get-Content -Raw -Path $path -ErrorAction SilentlyContinue
    if (-not $content) { return $null }
    foreach ($line in ($content -split '\r?\n')) {
        if ($line -match '^task:') { return $null }
        if ($line -match "^${key}:\s*(.+?)\s*$") {
            return $Matches[1].Trim()
        }
    }
    return $null
}

# Session map persists across dispatcher restarts so a channel keeps the same
# resumable claude conversation. Schema:
#   { "<channel_id>": "<session-uuid>", ... }
# Path: <workspace>/state/dispatcher-sessions.json
$SESSION_FILE = Join-Path $WORKSPACE 'state\dispatcher-sessions.json'
$sessionLock  = New-Object Object

function Load-SessionMap {
    if (-not (Test-Path $SESSION_FILE)) { return @{} }
    try {
        $raw = Get-Content -Raw -Path $SESSION_FILE -ErrorAction Stop
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        $h = @{}
        foreach ($p in $obj.PSObject.Properties) { $h[$p.Name] = $p.Value }
        return $h
    } catch {
        Log "session map: failed to load ($_); starting empty"
        return @{}
    }
}

function Save-SessionMap($map) {
    try {
        $json = $map | ConvertTo-Json -Depth 3 -Compress
        # Write to a tmp file then move - avoids torn writes if the dispatcher
        # is killed mid-write while another process is reading.
        $tmp = "$SESSION_FILE.tmp"
        Set-Content -Path $tmp -Value $json -NoNewline
        Move-Item -Path $tmp -Destination $SESSION_FILE -Force
    } catch {
        Log "session map: failed to save ($_)"
    }
}

# Single in-memory copy; serialize all reads/writes under $sessionLock so
# concurrent task processing doesn't race on Get/Set.
$script:sessions = Load-SessionMap
Log "session map: loaded $(($script:sessions.Keys | Measure-Object).Count) channel(s)"

# New-Guid v4 - used when a channel needs a fresh session.
function New-SessionId { [guid]::NewGuid().ToString() }

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

    # Resolve channel + session. Default channel `local` covers tasks with no
    # channel_id (older test fixtures, ad-hoc producers). Per-channel mapping
    # so voice/discord/telegram each retain their own conversation thread.
    $channel = Get-TaskHeader $claimedPath 'channel_id'
    if (-not $channel) { $channel = 'local' }

    [System.Threading.Monitor]::Enter($sessionLock)
    try {
        $sessionId = $script:sessions[$channel]
        $isNew = $false
        if (-not $sessionId) {
            $sessionId = New-SessionId
            $script:sessions[$channel] = $sessionId
            Save-SessionMap $script:sessions
            $isNew = $true
        }
    } finally {
        [System.Threading.Monitor]::Exit($sessionLock)
    }

    $promptHead = ($prompt -split '\r?\n')[0]
    $promptHead = $promptHead.Substring(0, [Math]::Min(60, $promptHead.Length))
    Log "${taskId}: processing channel=$channel session=$sessionId$(if ($isNew) { ' (NEW)' }) ($promptHead)"

    # First call to a channel uses --session-id (creates the session with that
    # UUID); subsequent calls use --resume to continue it. Both accept stdin
    # as the prompt body via --print. --output-format json so we can extract
    # the .result field cleanly and detect errors structurally.
    # so the agent doesn't pause asking for tool-call approvals - matches the
    # interactive sutando-core posture.
    $stderrPath = "$claimedPath.stderr"
    if ($isNew) {
        # Create the session with our pre-generated UUID; subsequent turns
        # will --resume it.
        $stdout = $prompt | & $CLAUDE --print --output-format json `
            --session-id $sessionId `
            --dangerously-skip-permissions 2>$stderrPath
    } else {
        $stdout = $prompt | & $CLAUDE --print --output-format json `
            --resume $sessionId `
            --dangerously-skip-permissions 2>$stderrPath
    }
    $exitCode = $LASTEXITCODE
    $stderr = Get-Content -Raw -Path $stderrPath -ErrorAction SilentlyContinue
    Remove-Item -Path $stderrPath -ErrorAction SilentlyContinue

    # Default to whatever claude printed; we override below on parse success.
    $result = if ($stdout -is [array]) { $stdout -join "`n" } else { [string]$stdout }

    if ($exitCode -ne 0) {
        $errMsg = if ($stderr) { $stderr.Trim() } else { "claude exited $exitCode" }
        Log "${taskId}: FAILED (exit=$exitCode): $errMsg"
        $result = "task-dispatcher: claude --print exited $exitCode. stderr:`n$errMsg"
    } else {
        # --output-format json emits a single-line JSON object with .result =
        # the assistant's text and .session_id = the live session UUID. Parse
        # and unwrap so consumers (web UI, bridges) see just the message.
        try {
            $obj = $result | ConvertFrom-Json -ErrorAction Stop
            if ($obj.is_error) {
                $errBody = if ($obj.result) { $obj.result } else { 'claude reported is_error=true' }
                Log "${taskId}: claude reported is_error=true: $errBody"
                $result = "task-dispatcher: claude error: $errBody"
            } elseif ($null -ne $obj.result) {
                $result = [string]$obj.result
                # Capture the returned session_id - it should match what we
                # sent. If claude rotated it (rare, but spec-allowed), persist
                # the new id so the next turn resumes the right thread.
                if ($obj.session_id -and $obj.session_id -ne $sessionId) {
                    Log "${taskId}: session id rotated $sessionId -> $($obj.session_id)"
                    [System.Threading.Monitor]::Enter($sessionLock)
                    try {
                        $script:sessions[$channel] = $obj.session_id
                        Save-SessionMap $script:sessions
                    } finally {
                        [System.Threading.Monitor]::Exit($sessionLock)
                    }
                }
            } else {
                Log "${taskId}: JSON had no .result field, falling through to raw stdout"
            }
        } catch {
            # Not JSON or malformed - keep the raw stdout we already assigned.
            Log "${taskId}: stdout was not JSON ($_), passing through raw"
        }
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
