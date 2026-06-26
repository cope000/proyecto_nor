param(
    [string]$ProjectPath = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [string]$PythonExe   = "c:/Users/54344/Desktop/A3/.venv/Scripts/python.exe",
    [int]$Port           = 8502
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectPath

$env:PYTHONUNBUFFERED = "1"

Write-Output "[DASH] Iniciando dashboard en http://localhost:$Port ..."
Write-Output "[DASH] Proyecto: $ProjectPath"
Write-Output "[DASH] Python: $PythonExe"
Write-Output "[DASH] Ctrl+C para detener."

try {
    if (-not (Test-Path $PythonExe)) {
        throw "No se encontro Python en: $PythonExe"
    }

    & $PythonExe -m streamlit run dashboard/dashboard.py --server.port $Port --server.headless true
}
catch {
    Write-Output ""
    Write-Output "[ERROR] No se pudo iniciar el dashboard."
    Write-Output ("[ERROR] " + $_.Exception.Message)
}
finally {
    Write-Output ""
    Write-Output "[DASH] Proceso finalizado. Presiona Enter para cerrar..."
    Read-Host | Out-Null
}
