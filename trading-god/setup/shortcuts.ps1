<#
.SYNOPSIS
    Put Start, Stop and Update icons for Trading God on the desktop.

.DESCRIPTION
    Run it once from the Vibe-Trading checkout (install.ps1 also runs it):

        powershell -ExecutionPolicy Bypass -File trading-god\setup\shortcuts.ps1

    Running it again replaces the icons. Delete an icon to remove it.

.PARAMETER AtLogin
    Also start Trading God when you sign in to Windows.
#>
[CmdletBinding()]
param(
    [switch]$AtLogin
)

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Run = Join-Path $PSScriptRoot "run.ps1"
# Windows PowerShell 5.1 is on every Windows 10 PC, whichever PowerShell runs this.
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Shell = New-Object -ComObject WScript.Shell

function New-Shortcut([string]$Folder, [string]$Name, [string]$Action, [string]$Description) {
    $path = Join-Path $Folder "$Name.lnk"
    $link = $Shell.CreateShortcut($path)
    $link.TargetPath = $PowerShell
    $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Run`" $Action"
    $link.WorkingDirectory = $RepoDir
    $link.Description = $Description
    $link.IconLocation = "$PowerShell,0"
    $link.Save()
    Write-Host "  $path" -ForegroundColor Green
}

$desktop = [Environment]::GetFolderPath("Desktop")
Write-Host "Desktop icons:"
New-Shortcut $desktop "Start Trading God" "start" "Start moomoo OpenD if needed, OpenBB, the desk and Vibe-Trading"
New-Shortcut $desktop "Stop Trading God" "stop" "Stop OpenBB, the desk and Vibe-Trading (OpenD keeps running)"
New-Shortcut $desktop "Update Trading God" "update" "Get the latest Trading God code and restart it"

if ($AtLogin) {
    Write-Host "Start at sign-in:"
    New-Shortcut ([Environment]::GetFolderPath("Startup")) "Start Trading God" "start" "Start Trading God at sign-in"
}
