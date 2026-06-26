param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

Set-Location $ProjectPath

$targets = @(
    "runners/run_mm.py",
    "runners/run_cc.py"
)

$allPids = @()
foreach ($t in $targets) {
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine -match [regex]::Escape($t)
    }
    $allPids += ($procs | Select-Object -ExpandProperty ProcessId)
}

$allPids = $allPids | Sort-Object -Unique
if (-not $allPids -or $allPids.Count -eq 0) {
    Write-Output "No hay bots corriendo."
    exit 0
}

Write-Output ("Intentando detener PIDs: " + ($allPids -join ", "))
foreach ($procId in $allPids) {
    cmd /c "taskkill /PID $procId /F" | Out-Null
}

Write-Output "Bots detenidos."
