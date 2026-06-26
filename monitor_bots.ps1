param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [int]$IntervalSeconds = 5,
    [int]$TailLines = 2,
    [switch]$Once
)

Set-Location $ProjectPath

$logs = @(
    "logs/run_mm_dlr.log",
    "logs/run_mm_cauc.log",
    "logs/run_cc.log"
)

function Show-Heartbeat {
    $now = Get-Date
    Write-Output ""
    Write-Output "=== HEARTBEAT ==="
    foreach ($log in $logs) {
        if (-not (Test-Path $log)) {
            Write-Output ("{0} | MISSING" -f $log)
            continue
        }
        $lw = (Get-Item $log).LastWriteTime
        $age = [math]::Round(($now - $lw).TotalSeconds)
        $state = if ($age -le 20) { "OK" } elseif ($age -le 60) { "LENTO" } else { "VIEJO" }
        Write-Output ("{0} | LastWrite={1:yyyy-MM-dd HH:mm:ss} | Age={2}s | {3}" -f $log, $lw, $age, $state)
    }
}

function Show-Tails {
    Write-Output ""
    Write-Output "=== ULTIMAS LINEAS ==="
    foreach ($log in $logs) {
        Write-Output ""
        Write-Output (">>> " + $log)
        if (-not (Test-Path $log)) {
            Write-Output "(archivo no encontrado)"
            continue
        }
        $tail = Get-Content $log -Tail $TailLines
        if (-not $tail) {
            Write-Output "(sin contenido)"
        }
        else {
            $tail | ForEach-Object { $_ }
        }
    }
}

function Show-Status {
    Write-Output "=== BOT STATUS ==="
    if (Test-Path ".\status_bots.ps1") {
        powershell -NoProfile -ExecutionPolicy Bypass -File .\status_bots.ps1
    }
    else {
        Write-Output "No existe status_bots.ps1"
    }
}

while ($true) {
    Clear-Host
    Write-Output ("NOR MONITOR | " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    Write-Output ("ProjectPath=" + $ProjectPath)
    Show-Status
    Show-Heartbeat
    Show-Tails

    if ($Once) {
        break
    }

    Start-Sleep -Seconds $IntervalSeconds
}
