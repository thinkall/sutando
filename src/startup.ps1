# Sutando — Windows startup
#
# Starts the minimum services needed to run Sutando on Windows with
# GitHub Copilot CLI as the core agent. No Mac, no Gemini API, no Twilio.
#
# Services started:
#   1. Agent API (port 7843)        — HTTP + web form for task submission
#   2. Copilot task runner          — polls tasks/, runs `copilot -p ...`
#   3. Edge-TTS watcher             — polls results/, generates .mp3
#
# Each service runs detached (its own job) and logs to logs\<name>.log.
# Re-run is idempotent: an already-running service is left alone.
#
# Usage:
#   pwsh src/startup.ps1                   # start everything (localhost)
#   pwsh src/startup.ps1 -Stop             # stop everything
#   pwsh src/startup.ps1 -Restart          # stop + start
#   pwsh src/startup.ps1 -Lan              # bind agent-api to 0.0.0.0
#                                            (requires SUTANDO_API_TOKEN)

[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Lan
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $Repo

$LogDir = Join-Path $Repo 'logs'
$PidDir = Join-Path $Repo 'state'
New-Item -ItemType Directory -Force -Path $LogDir, $PidDir, (Join-Path $Repo 'tasks'), (Join-Path $Repo 'results') | Out-Null

# --- Helpers ---------------------------------------------------------------

function Write-Step([string]$msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok([string]$msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "  XX  $msg" -ForegroundColor Red }

function Get-PidFile([string]$name) { Join-Path $PidDir "$name.pid" }

function Test-PidAlive([int]$processId) {
    if (-not $processId) { return $false }
    try { $null = Get-Process -Id $processId -ErrorAction Stop; return $true }
    catch { return $false }
}

function Stop-Service([string]$name) {
    $pidFile = Get-PidFile $name
    if (-not (Test-Path $pidFile)) { Write-Step "$name not running"; return }
    $procId = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    if (Test-PidAlive $procId) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Ok "stopped $name (pid $procId)"
        } catch {
            Write-Warn "failed to stop $name (pid $procId): $_"
        }
    } else {
        Write-Step "$name pid $procId no longer alive"
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

function Start-Service {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$Exe,
        [Parameter(Mandatory)] [string[]]$ArgList,
        [hashtable]$Env = @{}
    )
    $pidFile = Get-PidFile $Name
    if (Test-Path $pidFile) {
        $existingPid = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
        if (Test-PidAlive $existingPid) {
            Write-Ok "$Name already running (pid $existingPid)"
            return
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
    $logFile = Join-Path $LogDir "$Name.log"
    $errFile = $logFile + '.err'

    # Apply per-service env vars to the child process by setting them in
    # this shell first; the spawned cmd inherits the current environment.
    # Save/restore so we don't leak them into other Start-Service calls.
    $saved = @{}
    foreach ($k in $Env.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, [string]$Env[$k], 'Process')
    }
    try {
        # Spawn detached via WMI Win32_Process.Create. We avoid Start-Process
        # with -RedirectStandardOutput because that opens anonymous pipes
        # whose handles stay alive in the parent's .NET Process objects,
        # preventing the script from exiting cleanly. WMI's Create is true
        # fire-and-forget — no parent/child handle relationship.
        # Build a properly quoted cmd command line that:
        #  - explicitly re-sets every env var the service needs (WMI doesn't
        #    always inherit the caller's env block reliably), and
        #  - redirects stdio to log files via cmd's > / 2> operators.
        $quotedExe = '"' + $Exe + '"'
        $quotedArgs = $ArgList | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }
        # Always forward these from the parent so the service has access to
        # the .env values the user expects. Per-service overrides come last.
        $envForward = @{
            SUTANDO_API_TOKEN = $env:SUTANDO_API_TOKEN
            COPILOT_TASK_TIMEOUT_MS = $env:COPILOT_TASK_TIMEOUT_MS
            COPILOT_BIN = $env:COPILOT_BIN
            EDGE_TTS_VOICE = $env:EDGE_TTS_VOICE
            EDGE_TTS_RATE = $env:EDGE_TTS_RATE
            EDGE_TTS_MAX_CHARS = $env:EDGE_TTS_MAX_CHARS
            PYTHONIOENCODING = 'utf-8'
        }
        foreach ($k in $Env.Keys) { $envForward[$k] = [string]$Env[$k] }
        $setPrefix = ''
        foreach ($k in $envForward.Keys) {
            $v = $envForward[$k]
            if ($null -ne $v -and $v -ne '') {
                # cmd.exe `set` does NOT support quoting around the value,
                # so we must escape special chars (^ & < > | %).
                $vEsc = ($v -replace '([\^&<>\|%])', '^$1')
                $setPrefix += "set `"$k=$vEsc`" && "
            }
        }
        $cmdLine = "cmd.exe /d /c `"$setPrefix$quotedExe $($quotedArgs -join ' ') >`"$logFile`" 2>`"$errFile`"`""
        $startupInfo = ([WMIClass]'Win32_ProcessStartup').CreateInstance()
        $startupInfo.ShowWindow = 0  # SW_HIDE
        $result = ([WMIClass]'Win32_Process').Create($cmdLine, $Repo, $startupInfo)
        if ($result.ReturnValue -ne 0) {
            Write-Err "$Name failed to start (WMI ReturnValue=$($result.ReturnValue))"
            return
        }
        $cmdPid = $result.ProcessId

        # The cmd wrapper isn't the actual service process — the python/node
        # is its child. Wait briefly, then look up the child PID via
        # ParentProcessId so Stop-Service can kill the right process later.
        # cmd.exe also spawns a conhost.exe console host that we ignore.
        # Loop up to ~10s because Win32_Process queries can be slow on a
        # busy system and node in particular sometimes takes a moment to
        # fork from cmd.
        $childPid = $null
        for ($i = 0; $i -lt 100; $i++) {
            Start-Sleep -Milliseconds 100
            $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$cmdPid" -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne 'conhost.exe' } |
                Select-Object -First 1
            if ($children) { $childPid = $children.ProcessId; break }
        }
        if (-not $childPid) {
            # cmd may have already finished launching and reaped, in which
            # case the grandchild orphaned. Fall back to logging the cmd PID
            # — Stop-Service will at least try it.
            $childPid = $cmdPid
            Write-Warn "$Name child PID lookup timed out; tracking cmd wrapper pid $cmdPid"
        }
        $childPid | Out-File -Encoding ascii -FilePath $pidFile
        Write-Ok "$Name started (pid $childPid, logs/$Name.log)"
    } finally {
        foreach ($k in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process')
        }
    }
}

# --- Stop path ------------------------------------------------------------

if ($Stop -or $Restart) {
    Write-Host "Stopping Sutando services..."
    Stop-Service 'tts-watcher'
    Stop-Service 'task-runner'
    Stop-Service 'agent-api'
    if ($Stop -and -not $Restart) { return }
}

# --- Preflight ------------------------------------------------------------

Write-Host ""
Write-Host "Sutando (Windows + GitHub Copilot CLI) startup..." -ForegroundColor Cyan
Write-Host ""

$missing = $false
foreach ($cmd in @('node', 'python', 'copilot')) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) { Write-Ok "$cmd found ($($found.Source))" }
    else { Write-Err "$cmd not found on PATH"; $missing = $true }
}
if ($missing) {
    Write-Host ""
    Write-Host "Install the missing tools:" -ForegroundColor Yellow
    Write-Host "  Node 22+        — https://nodejs.org/"
    Write-Host "  Python 3.10+    — https://python.org/"
    Write-Host "  Copilot CLI     — npm i -g @github/copilot  (then run: copilot login)"
    exit 1
}

# Load .env into the current PowerShell process so child services pick it up.
$envFile = Join-Path $Repo '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $idx = $line.IndexOf('=')
            $k = $line.Substring(0, $idx).Trim()
            $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
    Write-Ok ".env loaded"
} else {
    Write-Warn ".env not found — copy .env.windows.example to .env if you want to customize"
}

# Verify edge-tts is installed (preferred: python -m edge_tts) — install on demand.
$ttsCheck = & python -c "import edge_tts" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Step "edge-tts not installed, running: python -m pip install --user edge-tts"
    & python -m pip install --user edge-tts 2>&1 | Out-Null
    $ttsCheck = & python -c "import edge_tts" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "edge-tts install failed — text-only mode (no audio output). Run: python -m pip install edge-tts"
    } else {
        Write-Ok "edge-tts installed"
    }
} else {
    Write-Ok "edge-tts available"
}

# Install Node deps if node_modules missing.
# NOTE: For the Windows path we don't actually need any npm packages — the
# task runner uses only Node stdlib. Skip npm install unless the user has
# explicitly opted in (e.g. they want to run other parts of Sutando too).
# `npm install` on Windows currently fails on bodhi-realtime-agent (a Mac-
# only voice dep with a bash-style postinstall script) — see docs/WINDOWS.md.
if ($env:SUTANDO_NPM_INSTALL -eq '1' -and -not (Test-Path (Join-Path $Repo 'node_modules'))) {
    Write-Step "node_modules missing — running npm install (this may take a while)"
    & npm install 2>&1 | Tee-Object -FilePath (Join-Path $LogDir 'npm-install.log') | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok "npm install complete" }
    else { Write-Warn "npm install had warnings (logs/npm-install.log) — continuing" }
} elseif (-not (Test-Path (Join-Path $Repo 'node_modules'))) {
    Write-Step "skipping npm install (Windows path needs none — set SUTANDO_NPM_INSTALL=1 to force)"
}

# --- LAN binding ----------------------------------------------------------

$bind = if ($Lan -or $env:AGENT_API_BIND) {
    if ($Lan) { '0.0.0.0' } else { $env:AGENT_API_BIND }
} else { '127.0.0.1' }

if ($bind -notin @('127.0.0.1', '::1', 'localhost') -and -not $env:SUTANDO_API_TOKEN) {
    Write-Err "AGENT_API_BIND=$bind requires SUTANDO_API_TOKEN in .env (random string)."
    Write-Host "  Generate one: -join ((1..32) | % { [char]((97..122) + (48..57) | Get-Random) })"
    exit 2
}

# --- Start services -------------------------------------------------------

Write-Host ""
Write-Host "Starting services..."

Start-Service `
    -Name 'agent-api' `
    -Exe 'python' `
    -ArgList @('-u', (Join-Path $Repo 'src\agent-api.py')) `
    -Env @{ AGENT_API_BIND = $bind }

Start-Service `
    -Name 'task-runner' `
    -Exe 'node' `
    -ArgList @((Join-Path $Repo 'src\copilot-task-runner.mjs'))

Start-Service `
    -Name 'tts-watcher' `
    -Exe 'python' `
    -ArgList @('-u', (Join-Path $Repo 'src\edge-tts-watcher.py'))

# --- Banner ---------------------------------------------------------------

Start-Sleep -Milliseconds 1500
Write-Host ""
Write-Host "Sutando is up." -ForegroundColor Cyan
Write-Host ""
Write-Host "  Web form:" -NoNewline
$tokenSuffix = if ($env:SUTANDO_API_TOKEN) { "?token=$($env:SUTANDO_API_TOKEN)" } else { '' }
if ($bind -in @('127.0.0.1', '::1', 'localhost')) {
    Write-Host "  http://localhost:7843/$tokenSuffix"
} else {
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
        Select-Object -First 1 -ExpandProperty IPAddress)
    if (-not $lanIp) { $lanIp = 'YOUR.LAN.IP' }
    Write-Host "  http://${lanIp}:7843/$tokenSuffix    (LAN — open this on your phone)"
    Write-Host "                Also reachable on http://localhost:7843/$tokenSuffix from this PC."
}
Write-Host ""
Write-Host "  Logs:       Get-Content -Wait logs\agent-api.log"
Write-Host "              Get-Content -Wait logs\task-runner.log"
Write-Host "              Get-Content -Wait logs\tts-watcher.log"
Write-Host ""
Write-Host "  Stop:       pwsh src\startup.ps1 -Stop"
Write-Host "  Restart:    pwsh src\startup.ps1 -Restart"
Write-Host ""
