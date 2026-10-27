<#
.SYNOPSIS
    Start Trading God: the OpenBB data server, the approval desk and
    Vibe-Trading, then register the daily pre-market scan.

.DESCRIPTION
    If moomoo OpenD is not running, this starts it from its Start menu or desktop
    shortcut and waits for you to log in. Then run:

        powershell -ExecutionPolicy Bypass -File trading-god\setup\start.ps1

    Each service opens in its own minimized window; close a window to stop
    that service, or run stop.ps1 to stop them all. Running this again only
    starts what is not already running.

.PARAMETER NoBrowser
    Do not open the desk page and Vibe-Trading in the browser.
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$TgHome = Join-Path $env:USERPROFILE ".trading-god"
$VibeScripts = Join-Path $TgHome "venvs\vibe\Scripts"
$ObbScripts = Join-Path $TgHome "venvs\openbb\Scripts"
$TgDesk = Join-Path $VibeScripts "tg-desk.exe"

# These match the defaults in desk.toml ([web] port and [schedule] vibe_url).
$OpenDPort = 11111
$OpenbbPort = 8001
$DeskPort = 8765
$VibePort = 8899
$OpenbbCategories = "nasdaq,sec,cboe,finra,fred,federal_reserve,bls,ustreasury,news,famafrench"

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        return ($attempt.AsyncWaitHandle.WaitOne(500) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-Port([int]$Port, [string]$Name, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 2
    }
    Write-Warning "$Name did not start within $Seconds seconds. Check its window for errors."
    return $false
}

function Start-Window([string]$Title, [string]$Exe, [string[]]$Arguments, [hashtable]$Environment = @{}) {
    # The command is passed encoded, so paths with spaces or quotes survive intact.
    $lines = @("`$Host.UI.RawUI.WindowTitle = '$Title'")
    foreach ($name in $Environment.Keys) {
        $lines += "`$env:$name = '" + ($Environment[$name] -replace "'", "''") + "'"
    }
    $quoted = ($Arguments | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join " "
    $lines += "& '" + ($Exe -replace "'", "''") + "' $quoted"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($lines -join "; "))
    Start-Process "powershell.exe" -WindowStyle Minimized -ArgumentList @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded
    ) | Out-Null
}

if (-not (Test-Path $TgDesk)) {
    throw "Trading God is not installed yet. Run install.ps1 in this folder first."
}

function Find-OpenD {
    # The moomoo OpenD installer leaves a shortcut; a portable copy may have one on the desktop.
    $folders = @("Programs", "CommonPrograms", "Desktop", "CommonDesktopDirectory") |
        ForEach-Object { [Environment]::GetFolderPath($_) } | Where-Object { $_ -and (Test-Path $_) }
    foreach ($folder in $folders) {
        $link = Get-ChildItem $folder -Recurse -Depth 3 -Filter "*.lnk" -ErrorAction SilentlyContinue |
            Where-Object { $_.BaseName -match "OpenD" -and $_.BaseName -notmatch "uninstall" } | Select-Object -First 1
        if ($link) { return $link.FullName }
    }
    return $null
}

if (Test-Port $OpenDPort) {
    Write-Host "moomoo OpenD: running" -ForegroundColor Green
} else {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match "OpenD" }
    $openD = if ($running) { $null } else { Find-OpenD }
    if ($openD) {
        Write-Host "Starting moomoo OpenD. Log in in its window if it asks."
        Start-Process $openD
    }
    if ($running -or $openD) {
        Wait-Port $OpenDPort "moomoo OpenD" 120 | Out-Null
    } else {
        Write-Warning "moomoo OpenD is not running and no shortcut to it was found. Start it and log in; the desk shows an error until then."
    }
    if (Test-Port $OpenDPort) { Write-Host "moomoo OpenD: running" -ForegroundColor Green }
}

if (Test-Port $OpenbbPort) {
    Write-Host "OpenBB data server: already running"
} else {
    Write-Host "Starting the OpenBB data server..."
    Start-Window "Trading God - OpenBB data" (Join-Path $ObbScripts "openbb-mcp.exe") @(
        "--tool-discovery", "--host", "127.0.0.1", "--port", "$OpenbbPort", "--allowed-categories", $OpenbbCategories
    )
}

if (Test-Port $DeskPort) {
    Write-Host "Approval desk: already running"
} else {
    Write-Host "Starting the approval desk..."
    Start-Window "Trading God - approval desk" $TgDesk @("serve")
}

if (Test-Port $VibePort) {
    Write-Host "Vibe-Trading: already running"
} else {
    Write-Host "Starting Vibe-Trading..."
    Start-Window "Trading God - Vibe-Trading" (Join-Path $VibeScripts "vibe-trading.exe") @(
        "serve", "--host", "127.0.0.1", "--port", "$VibePort"
    ) @{ VIBE_TRADING_ENABLE_SCHEDULER = "1" }
}

Write-Host "Waiting for the services (OpenBB takes up to a minute)..."
$deskUp = Wait-Port $DeskPort "The approval desk" 60
$vibeUp = Wait-Port $VibePort "Vibe-Trading" 120
$openbbUp = Wait-Port $OpenbbPort "The OpenBB data server" 180
if (-not $openbbUp) {
    Write-Warning "The agent can still research with Vibe-Trading's own data tools, but without OpenBB's."
}

# Picks up a changed skill after a git pull; Vibe-Trading reads skills per run.
& $TgDesk skills
if ($LASTEXITCODE -ne 0) { Write-Warning "Could not refresh the Trading God skill. Run this later: $TgDesk setup" }

if ($vibeUp) {
    & $TgDesk schedule
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not register the pre-market scan. Run this later: $TgDesk schedule" }
}

if (-not $NoBrowser) {
    if ($deskUp) { Start-Process "http://127.0.0.1:$DeskPort" }
    if ($vibeUp) { Start-Process "http://127.0.0.1:$VibePort" }
}

Write-Host ""
Write-Host "Trading God is running on the moomoo PAPER account." -ForegroundColor Green
Write-Host "  Desk page:     http://127.0.0.1:$DeskPort  (approve or reject proposals)"
Write-Host "  Vibe-Trading:  http://127.0.0.1:$VibePort  (chat with the agent, see scan reports)"
Write-Host "  The scan runs at 8:35 a.m. New York time on weekdays while this PC is awake."
