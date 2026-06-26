param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [string]$PythonExe = "c:/Users/54344/Desktop/A3/.venv/Scripts/python.exe"
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectPath

$logsDir = Join-Path $ProjectPath "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

$pidFile = Join-Path $ProjectPath "bots_pids.txt"
if (Test-Path $pidFile) {
    Remove-Item $pidFile -Force
}

$restartLog = Join-Path $logsDir "supervisor_restarts.log"
if (-not (Test-Path $restartLog)) {
    New-Item -ItemType File -Path $restartLog | Out-Null
}

# ─── SCHEDULER: espera hasta el horario target ───────────────────────────────
function Wait-UntilTime {
    param(
        [int]$TargetHour,
        [int]$TargetMinute,
        [string]$Label
    )

    $now = Get-Date
    $target = (Get-Date -Hour $TargetHour -Minute $TargetMinute -Second 0 -Millisecond 0)

    if ($now -gt $target) {
        Write-Output "[$Label] Horario $($TargetHour):$($TargetMinute.ToString('D2')) ya paso. Lanzando igual."
        return
    }

    $secsLeft = [int]($target - $now).TotalSeconds
    Write-Output "[$Label] Esperando hasta $($TargetHour):$($TargetMinute.ToString('D2')) ART ($secsLeft segundos)..."

    while ((Get-Date) -lt $target) {
        $secsLeft = [int]($target - (Get-Date)).TotalSeconds
        Write-Output "[$Label] Faltan $secsLeft segundos..."
        Start-Sleep -Seconds 10
    }

    Write-Output "[$Label] Hora alcanzada. Lanzando."
}
# ─────────────────────────────────────────────────────────────────────────────

function Test-BotRunning {
    param(
        [string]$ScriptName,
        [string]$ScriptArgs
    )

    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "python.exe" -and
        $_.CommandLine -and
        $_.CommandLine -match [regex]::Escape($ScriptName) -and
        ($ScriptArgs -eq "" -or $_.CommandLine -match [regex]::Escape($ScriptArgs))
    }

    return ($procs.Count -gt 0)
}

function Start-BotSupervisorJob {
    param(
        [string]$Name,
        [string]$ScriptName,
        [string]$ScriptArgs
    )

    if (Test-BotRunning -ScriptName $ScriptName -ScriptArgs $ScriptArgs) {
        Write-Output "[SKIP] $Name ya esta corriendo."
        return
    }

    $job = Start-Job -Name ("supervisor_" + $Name) -ScriptBlock {
        param($ProjectPath, $PythonExe, $BotName, $ScriptName, $ScriptArgs, $RestartLogPath)

        $ErrorActionPreference = "Continue"
        Set-Location $ProjectPath

        $maxRestartsPerHour = 10
        $restartCount = 0
        $hourStart = Get-Date
        $logFile = Join-Path $ProjectPath ("logs/run_{0}.log" -f $BotName)
        $argTokens = @()
        if ($ScriptArgs) {
            $argTokens = $ScriptArgs -split '\s+' | Where-Object { $_ -ne "" }
        }

        while ($true) {
            if (((Get-Date) - $hourStart).TotalHours -ge 1) {
                $restartCount = 0
                $hourStart = Get-Date
            }

            if ($restartCount -ge $maxRestartsPerHour) {
                $waitMsg = "[$BotName] Max reinicios/hora alcanzado. Esperando 10 min."
                Write-Host $waitMsg
                Add-Content -Path $RestartLogPath -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " | " + $BotName + " | max_restarts_reached | wait_seconds=600")
                Start-Sleep -Seconds 600
                $restartCount = 0
                $hourStart = Get-Date
                continue
            }

            Write-Host "[$BotName] Iniciando... (restart #$restartCount)"
            Add-Content -Path $logFile -Value ("`n===== RESTART " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " =====")

            & $PythonExe $ScriptName @argTokens *>> $logFile
            $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }

            Write-Host "[$BotName] Proceso termino con codigo $exitCode"

            # Si el log muestra fallo de autenticación, aplicar backoff largo.
            $authFailed = $false
            try {
                if (Test-Path $logFile) {
                    $tail = Get-Content -Path $logFile -Tail 120 -ErrorAction Stop
                    if ($tail | Select-String -SimpleMatch "Authentication fails. Incorrect User or Password") {
                        $authFailed = $true
                    }
                }
            }
            catch {
                Write-Host "[$BotName] No se pudo inspeccionar log para auth failure: $($_.Exception.Message)"
            }

            if ($authFailed) {
                $authMsg = "[$BotName] Auth failure detectado. Backoff 10 min antes de reintentar."
                Write-Host $authMsg
                Add-Content -Path $RestartLogPath -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " | " + $BotName + " | auth_failure_detected | wait_seconds=600 | exit_code=" + $exitCode)
                Start-Sleep -Seconds 600
                continue
            }

            $restartCount++
            Add-Content -Path $RestartLogPath -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " | " + $BotName + " | restart #" + $restartCount + " | exit_code=" + $exitCode)
            Write-Host "[$BotName] Reiniciando en 5s..."
            Start-Sleep -Seconds 5
        }
    } -ArgumentList $ProjectPath, $PythonExe, $Name, $ScriptName, $ScriptArgs, $restartLog

    Add-Content -Path $pidFile -Value ("${Name}:job_id=" + $job.Id)
    Write-Output "[OK] $Name supervisor job iniciado. JobID=$($job.Id)"
}

# ─── LANZAMIENTO CON SCHEDULER ───────────────────────────────────────────────

# DLR y CAUC: arrancan a las 10:00 ART (esperar hasta 09:59)
Wait-UntilTime -TargetHour 9 -TargetMinute 59 -Label "DLR/CAUC"
Start-BotSupervisorJob -Name "mm_dlr"  -ScriptName "runners/run_mm.py" -ScriptArgs "--instrument DLR --run-seconds 0"
Start-BotSupervisorJob -Name "mm_cauc" -ScriptName "runners/run_mm.py" -ScriptArgs "--instrument CAUC --run-seconds 0"

# CC: sin espera (asumiendo independiente o siempre-on)
Start-BotSupervisorJob -Name "cc" -ScriptName "runners/run_cc.py" -ScriptArgs "--cycles 0"

# SOJ: arranca a las 11:00 ART (esperar hasta 10:59)
Wait-UntilTime -TargetHour 10 -TargetMinute 59 -Label "SOJ"
Start-BotSupervisorJob -Name "mm_soj" -ScriptName "runners/run_mm.py" -ScriptArgs "--instrument SOJ --run-seconds 0"

# ─────────────────────────────────────────────────────────────────────────────

Write-Output ""
Write-Output "Supervisores iniciados. Logs en logs/"
Write-Output "Reinicios: logs/supervisor_restarts.log"
Write-Output "Para ver estado: .\status_bots.ps1"
Write-Output "Para apagar: .\stop_all_bots.ps1"

Get-Job | Wait-Job
