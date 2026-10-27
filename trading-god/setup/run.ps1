<#
.SYNOPSIS
    What the desktop icons run: start, stop or update Trading God, then keep the
    window open long enough to read the result.

.DESCRIPTION
    shortcuts.ps1 creates the icons. On success the window closes by itself after a
    few seconds. On an error or a warning it waits for Enter, so nothing scrolls away.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "update")]
    [string]$Action
)

$Host.UI.RawUI.WindowTitle = "Trading God - $Action"
$script = Join-Path $PSScriptRoot "$Action.ps1"
$warned = $false
$failed = $false
try {
    # Warnings come through the pipeline so a nested script's count too.
    & $script 3>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.WarningRecord]) {
            $warned = $true
            Write-Host "WARNING: $($_.Message)" -ForegroundColor Yellow
        } else {
            $_
        }
    }
} catch {
    $failed = $true
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
}

Write-Host ""
if ($failed -or $warned) {
    Read-Host "Read the messages above, then press Enter to close this window"
} else {
    Write-Host "Done. This window closes in 10 seconds."
    Start-Sleep -Seconds 10
}
