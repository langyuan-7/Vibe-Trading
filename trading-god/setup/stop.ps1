<#
.SYNOPSIS
    Stop the Trading God services that start.ps1 started. moomoo OpenD is left running.
#>
[CmdletBinding()]
param()

$ports = [ordered]@{ "Vibe-Trading" = 8899; "Approval desk" = 8765; "OpenBB data server" = 8001 }

foreach ($name in $ports.Keys) {
    $listeners = Get-NetTCPConnection -LocalPort $ports[$name] -State Listen -ErrorAction SilentlyContinue
    if (-not $listeners) {
        Write-Host "${name}: not running"
        continue
    }
    foreach ($processId in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    Write-Host "${name}: stopped" -ForegroundColor Green
}
