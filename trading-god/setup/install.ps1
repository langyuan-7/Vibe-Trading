<#
.SYNOPSIS
    Install Trading God on this Windows PC: Vibe-Trading, the approval desk,
    OpenBB and the investor playbooks, wired to a moomoo PAPER account and
    to your local Claude Code login.

.DESCRIPTION
    Run it from the Vibe-Trading checkout:

        powershell -ExecutionPolicy Bypass -File trading-god\setup\install.ps1

    It is safe to run again. It reuses what is already installed and keeps
    your settings in %USERPROFILE%\.trading-god and %USERPROFILE%\.vibe-trading.
    Everything it installs goes under %USERPROFILE%\.trading-god\venvs.

.PARAMETER Yes
    Install missing tools (Git, uv, Node.js, Claude Code) without asking.

.PARAMETER SkipFrontend
    Do not build the Vibe-Trading web page. The desk page works either way.

.PARAMETER Model
    The Claude model the agent uses: "sonnet" (default) or "opus".
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$SkipFrontend,
    [string]$Model = "sonnet"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$HubDir = Split-Path -Parent $PSScriptRoot
$RepoDir = Split-Path -Parent $HubDir
$TgHome = Join-Path $env:USERPROFILE ".trading-god"
$VibeVenv = Join-Path $TgHome "venvs\vibe"
$ObbVenv = Join-Path $TgHome "venvs\openbb"
$VibePy = Join-Path $VibeVenv "Scripts\python.exe"
$ObbPy = Join-Path $ObbVenv "Scripts\python.exe"
$MinNode = [version]"22.22.0"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }

function Write-Note([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }

function Invoke-Native([string]$What, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    # Pick up tools installed a moment ago without opening a new window.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $local = Join-Path $env:USERPROFILE ".local\bin"
    $env:Path = "$local;$user;$machine"
}

function Confirm-Install([string]$What) {
    if ($Yes) { return $true }
    $answer = Read-Host "    $What is not installed. Install it now? [Y/n]"
    return ($answer -eq "" -or $answer -match "^[Yy]")
}

function Install-Winget([string]$Id, [string]$Label, [string]$Verb = "install") {
    if (-not (Test-Command "winget")) {
        throw "$Label is needed and winget is not available. Install $Label yourself, then run this script again."
    }
    Invoke-Native "Installing $Label" "winget" @(
        $Verb, "--id", $Id, "--exact", "--source", "winget",
        "--accept-package-agreements", "--accept-source-agreements"
    )
    Update-SessionPath
}

function Get-NodeVersion {
    if (-not (Test-Command "node")) { return $null }
    try { return [version]((& node --version).Trim().TrimStart("v")) } catch { return $null }
}

Write-Host "Trading God installer. Paper trading only: nothing here can trade real money." -ForegroundColor White
Write-Host "Vibe-Trading checkout: $RepoDir"
if (-not (Test-Path (Join-Path $RepoDir "requirements-lock.txt"))) {
    throw "Run this script from the Vibe-Trading checkout: trading-god\setup\install.ps1"
}
Update-SessionPath

# -- 1. Tools -----------------------------------------------------------------
Write-Step "1/6 Checking tools"

if (-not (Test-Command "git")) {
    if (-not (Confirm-Install "Git for Windows (Claude Code needs it)")) { throw "Git for Windows is required." }
    Install-Winget "Git.Git" "Git for Windows"
}
Write-Ok ("git: " + (& git --version))

if (-not (Test-Command "uv")) {
    if (-not (Confirm-Install "uv (installs Python and its packages)")) { throw "uv is required." }
    if (Test-Command "winget") {
        Install-Winget "astral-sh.uv" "uv"
    } else {
        Invoke-RestMethod "https://astral.sh/uv/install.ps1" | Invoke-Expression
        Update-SessionPath
    }
}
Write-Ok ("uv: " + (& uv --version))

if (-not $SkipFrontend) {
    $node = Get-NodeVersion
    if ($null -eq $node -or $node -lt $MinNode) {
        $what = if ($null -eq $node) { "Node.js LTS (for the Vibe-Trading web page)" } else { "Node.js $MinNode or newer (you have $node)" }
        if (Confirm-Install $what) {
            $verb = if ($null -eq $node) { "install" } else { "upgrade" }
            try { Install-Winget "OpenJS.NodeJS.LTS" "Node.js LTS" $verb } catch { Write-Note $_.Exception.Message }
            $node = Get-NodeVersion
        }
        if ($null -eq $node -or $node -lt $MinNode) {
            Write-Note "Skipping the Vibe-Trading web page: it needs Node.js $MinNode or newer. The desk page still works."
            $SkipFrontend = $true
        }
    }
    if (-not $SkipFrontend) { Write-Ok "node: v$node" }
}

# Vibe-Trading starts claude with subprocess, which needs claude.exe: it cannot
# run npm's claude.ps1, and cmd.exe mangles its arguments through claude.cmd.
function Find-ClaudeExe {
    $native = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
    if (Test-Path $native) { return $native }
    return Get-Command "claude" -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -eq ".exe" } | Select-Object -First 1 -ExpandProperty Source
}
$ClaudePath = Find-ClaudeExe
if (-not $ClaudePath) {
    $why = if (Test-Command "claude") { "Claude Code native build (your current claude is an npm script the agent cannot start; your login is kept)" } else { "Claude Code (the agent's brain, using your Claude login)" }
    if (-not (Confirm-Install $why)) { throw "Claude Code (claude.exe) is required." }
    Invoke-RestMethod "https://claude.ai/install.ps1" | Invoke-Expression
    Update-SessionPath
    $ClaudePath = Find-ClaudeExe
    if (-not $ClaudePath) { throw "Claude Code was installed but claude.exe was not found. Open a new PowerShell window and run this script again." }
}
Write-Ok "claude: $ClaudePath"

# -- 2. Python ----------------------------------------------------------------
Write-Step "2/6 Python 3.11 and 3.12, managed by uv (any Python you already have is left alone)"
Invoke-Native "Installing Python" "uv" @("python", "install", "3.11", "3.12")

# -- 3. Vibe-Trading and the desk -------------------------------------------------
Write-Step "3/6 Vibe-Trading and the Trading God desk"
if (-not (Test-Path $VibePy)) {
    Invoke-Native "Creating the Vibe-Trading environment" "uv" @("venv", "--python", "3.11", $VibeVenv)
}
# The hash-locked set is the one Vibe-Trading's CI checks on Windows.
Invoke-Native "Installing Vibe-Trading's locked dependencies" "uv" @(
    "pip", "install", "--python", $VibePy, "--require-hashes", "-r", (Join-Path $RepoDir "requirements-lock.txt")
)
Invoke-Native "Installing Vibe-Trading" "uv" @("pip", "install", "--python", $VibePy, "--no-deps", "-e", $RepoDir)
Invoke-Native "Installing the Trading God desk and the moomoo SDK" "uv" @("pip", "install", "--python", $VibePy, "-e", $HubDir)

# -- 4. OpenBB ------------------------------------------------------------------
Write-Step "4/6 OpenBB data server (its own environment: it needs newer libraries than Vibe-Trading)"
if (-not (Test-Path $ObbPy)) {
    Invoke-Native "Creating the OpenBB environment" "uv" @("venv", "--python", "3.12", $ObbVenv)
}
Invoke-Native "Installing OpenBB" "uv" @("pip", "install", "--python", $ObbPy, "openbb==5.0.0", "openbb-mcp-server==2.0.1")
Write-Note "Building OpenBB's command list (about a minute)..."
Invoke-Native "Building OpenBB" (Join-Path $ObbVenv "Scripts\openbb-build.exe") @()

# -- 5. Web page ----------------------------------------------------------------
Write-Step "5/6 Vibe-Trading web page"
if ($SkipFrontend) {
    Write-Note "Skipped."
} else {
    Push-Location (Join-Path $RepoDir "frontend")
    try {
        Invoke-Native "Installing the web page's packages" "npm" @("ci")
        Invoke-Native "Building the web page" "npm" @("run", "build")
    } finally {
        Pop-Location
    }
}

# -- 6. Wiring ------------------------------------------------------------------
Write-Step "6/6 Connecting Vibe-Trading to the desk, OpenBB, moomoo and the playbooks"
$TgDesk = Join-Path $VibeVenv "Scripts\tg-desk.exe"
Invoke-Native "Configuring Vibe-Trading" $TgDesk @(
    "setup", "--desk-python", $VibePy, "--claude-path", $ClaudePath, "--model", $Model
)

Write-Host ""
Write-Host "Checking moomoo OpenD and the paper account..."
& $TgDesk check
if ($LASTEXITCODE -ne 0) {
    Write-Note "moomoo is not reachable yet. Start moomoo OpenD, log in, then run: $TgDesk check"
}

Write-Host ""
Write-Host "Checking your Claude Code login..."
& $ClaudePath auth status
if ($LASTEXITCODE -ne 0) {
    Write-Note "Sign in once with: claude auth login"
}

Write-Host ""
Write-Host "Installed." -ForegroundColor Green
Write-Host "  Settings:   $TgHome\desk.toml and $TgHome\watchlist.txt"
Write-Host "  Start:      powershell -ExecutionPolicy Bypass -File `"$PSScriptRoot\start.ps1`""
Write-Host "  Desk page:  http://127.0.0.1:8765  (approve or reject proposals here)"
