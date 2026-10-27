<#
.SYNOPSIS
    Update Trading God: pull the latest code, reinstall what changed, then restart.

.DESCRIPTION
    Run it from the Vibe-Trading checkout, or use the "Update Trading God" icon:

        powershell -ExecutionPolicy Bypass -File trading-god\setup\update.ps1

    The Python packages are installed in editable mode, so most changes need only
    the restart. Dependencies are reinstalled when their lists changed, and the
    Vibe-Trading web page is rebuilt when its code changed. Settings in
    %USERPROFILE%\.trading-god are never touched.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$HubDir = Split-Path -Parent $PSScriptRoot
$RepoDir = Split-Path -Parent $HubDir
$VibePy = Join-Path $env:USERPROFILE ".trading-god\venvs\vibe\Scripts\python.exe"

function Invoke-Native([string]$What, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

# A window opened from an icon may predate tools installed since.
$env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" +
    [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
    [Environment]::GetEnvironmentVariable("Path", "Machine")

if (-not (Test-Path $VibePy)) {
    throw "Trading God is not installed yet. Run install.ps1 in this folder first."
}

Write-Host "Getting the latest code..."
$before = (& git -C $RepoDir rev-parse HEAD).Trim()
& git -C $RepoDir pull --ff-only
if ($LASTEXITCODE -ne 0) {
    throw "git pull failed. If it mentions local changes, run 'git -C `"$RepoDir`" status' to see them."
}
$after = (& git -C $RepoDir rev-parse HEAD).Trim()

if ($before -eq $after) {
    Write-Host "Already up to date." -ForegroundColor Green
    & (Join-Path $PSScriptRoot "start.ps1")
    return
}

$changed = @(& git -C $RepoDir diff --name-only $before $after)
Write-Host "$($changed.Count) files changed." -ForegroundColor Green

& (Join-Path $PSScriptRoot "stop.ps1")

if ($changed -match "^(requirements-lock\.txt|pyproject\.toml|trading-god/pyproject\.toml)$") {
    Write-Host "Reinstalling the Python packages, whose lists changed..."
    Invoke-Native "Installing Vibe-Trading's locked dependencies" "uv" @(
        "pip", "install", "--python", $VibePy, "--require-hashes", "-r", (Join-Path $RepoDir "requirements-lock.txt")
    )
    Invoke-Native "Installing Vibe-Trading" "uv" @("pip", "install", "--python", $VibePy, "--no-deps", "-e", $RepoDir)
    Invoke-Native "Installing the Trading God desk" "uv" @("pip", "install", "--python", $VibePy, "-e", $HubDir)
}

$frontend = Join-Path $RepoDir "frontend"
if (($changed -match "^frontend/") -and (Test-Path (Join-Path $frontend "dist"))) {
    Write-Host "Rebuilding the Vibe-Trading web page..."
    Push-Location $frontend
    try {
        Invoke-Native "Installing the web page's packages" "npm" @("ci")
        Invoke-Native "Building the web page" "npm" @("run", "build")
    } finally {
        Pop-Location
    }
}

& (Join-Path $PSScriptRoot "start.ps1")
