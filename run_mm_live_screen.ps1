param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [string]$PythonExe = "c:/Users/54344/Desktop/A3/.venv/Scripts/python.exe",
    [switch]$StopExisting = $true
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectPath

if ($StopExisting) {
    $mmProcs = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and $_.CommandLine -match [regex]::Escape("runners/run_mm.py")
    }
    foreach ($p in $mmProcs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Output ("[STOP] run_mm.py PID=" + $p.ProcessId)
        }
        catch {
            Write-Output ("[WARN] No pude detener PID=" + $p.ProcessId + " | " + $_.Exception.Message)
        }
    }
}

$logFile = Join-Path $ProjectPath "logs/run_mm_dlr.log"
Add-Content -Path $logFile -Value ("`n===== LIVE SCREEN START " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " =====")

$env:PYTHONUNBUFFERED = "1"
Write-Output "[LIVE] MM en pantalla. Ctrl+C para cortar."
Write-Output ("[LIVE] Log: " + $logFile)

& $PythonExe -u "runners/run_mm.py" --instrument DLR --run-seconds 0 2>&1 | Tee-Object -FilePath $logFile -Append
