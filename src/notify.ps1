#requires -Version 5.1
<#
Sutando — Windows toast / balloon notification helper.

Shows a brief notification using the built-in Windows .NET NotifyIcon
balloon. No third-party module required (BurntToast etc. are not needed).

Usage:
  pwsh src/notify.ps1 -Title "Sutando" -Message "Task done"
  pwsh src/notify.ps1 -Message "Pending question waiting"
  pwsh src/notify.ps1 -Title "Sutando" -Message "Done" -Icon Info -TimeoutMs 6000

Notes:
  * Requires an interactive desktop session. Notifications WILL NOT show
    when the script is invoked from a Windows service / WMI background
    context with no logged-in user. Failures are silent (exit 0) so this
    can be wired into the task runner with no risk of breaking it.
  * The Icon parameter accepts Info / Warning / Error.
  * The balloon is shown and the script exits ~TimeoutMs+200ms later so
    the icon is disposed cleanly. Default TimeoutMs is 5000.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)] [string]$Title = "Sutando",
    [Parameter(Mandatory=$true)]  [string]$Message,
    [ValidateSet('Info','Warning','Error')] [string]$Icon = 'Info',
    [int]$TimeoutMs = 5000
)

$ErrorActionPreference = 'SilentlyContinue'

try {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    Add-Type -AssemblyName System.Drawing | Out-Null

    $notify = New-Object System.Windows.Forms.NotifyIcon
    $notify.Icon = [System.Drawing.SystemIcons]::Information
    $notify.Visible = $true
    $notify.BalloonTipTitle = $Title
    $notify.BalloonTipText = $Message
    $notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::$Icon
    $notify.ShowBalloonTip($TimeoutMs)

    # Keep the icon alive long enough for the balloon to render. Using a
    # tight Sleep+Dispose pair would race the shell and the balloon would
    # never appear. Add a small fudge over the requested timeout.
    Start-Sleep -Milliseconds ($TimeoutMs + 250)

    $notify.Visible = $false
    $notify.Dispose()
    exit 0
} catch {
    # Fail soft — the runner does not depend on notifications working.
    # Log to stderr only so callers that capture stdout aren't disturbed.
    [Console]::Error.WriteLine("notify.ps1: $_")
    exit 0
}
