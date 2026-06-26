param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

Set-Location $ProjectPath

$targets = @(
    "runners/run_mm.py",
    "runners/run_cc.py"
)

Write-Output "=== BOT STATUS ==="
foreach ($t in $targets) {
    $pyProcs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "python.exe" -and
        $_.CommandLine -and $_.CommandLine -match [regex]::Escape($t)
    }

    $supervisors = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "powershell.exe" -and
        $_.CommandLine -and
        $_.CommandLine -like "*while*" -and
        $_.CommandLine -match [regex]::Escape($t)
    }

    if ($pyProcs.Count -gt 0) {
        $pyPids = ($pyProcs | Select-Object -ExpandProperty ProcessId | Sort-Object -Unique) -join ", "
        if ($supervisors.Count -gt 0) {
            $supPids = ($supervisors | Select-Object -ExpandProperty ProcessId | Sort-Object -Unique) -join ", "
            Write-Output "[RUNNING] $t | Python PID: $pyPids | Supervisor PID: $supPids"
        }
        else {
            Write-Output "[RUNNING] $t | Python PID: $pyPids"
        }
    }
    elseif ($supervisors.Count -gt 0) {
        $supPids = ($supervisors | Select-Object -ExpandProperty ProcessId | Sort-Object -Unique) -join ", "
        Write-Output "[SUPERVISOR_ON] $t | Supervisor PID: $supPids | Python: transient/off"
    }
    else {
        Write-Output "[STOPPED] $t"
    }
}

Write-Output ""
$logs = @(
    "logs/run_mm_dlr.log",
    "logs/run_mm_cauc.log",
    "logs/run_cc.log"
)

Write-Output "=== LOG HEARTBEAT ==="
foreach ($l in $logs) {
    if (Test-Path $l) {
        $it = Get-Item $l
        Write-Output ("$l | LastWrite=" + $it.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss"))
    }
    else {
        Write-Output "$l | not found"
    }
}
