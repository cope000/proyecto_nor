param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

Set-Location $ProjectPath

Write-Output "Reiniciando bots NOR..."

# Stop phase
if (Test-Path ".\stop_all_bots.ps1") {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\stop_all_bots.ps1
}
else {
    Write-Output "No existe stop_all_bots.ps1"
    exit 1
}

Start-Sleep -Seconds 3

# Start phase
if (Test-Path ".\start_all_bots.ps1") {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\start_all_bots.ps1
}
else {
    Write-Output "No existe start_all_bots.ps1"
    exit 1
}

Start-Sleep -Seconds 2

# Status phase
if (Test-Path ".\status_bots.ps1") {
    powershell -NoProfile -ExecutionPolicy Bypass -File .\status_bots.ps1
}
else {
    Write-Output "No existe status_bots.ps1"
}
