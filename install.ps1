# install.ps1 - one-time setup for the free TikTok clip maker (Windows).
# Run from this folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "TikTok Clip Maker"

Write-Host "== 1/5 Python ==" -ForegroundColor Cyan
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd -or $pyCmd.Source -like "*WindowsApps*") {
    throw "Python 3.11+ not found. Run:  winget install Python.Python.3.12   then open a NEW PowerShell and re-run this script."
}
& python -c "import sys; assert sys.version_info >= (3,11)"; if ($LASTEXITCODE) { throw "Python 3.11+ required" }
$pyw = Join-Path (Split-Path $pyCmd.Source) "pythonw.exe"

Write-Host "== 2/5 FFmpeg ==" -ForegroundColor Cyan
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    throw "FFmpeg installed. Open a NEW PowerShell window and run this script again (PATH refresh)."
}

Write-Host "== 3/5 Python packages ==" -ForegroundColor Cyan
& python -m pip install --upgrade -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE) { throw "pip install failed" }

Write-Host "== 4/5 Folders + downloading the Whisper model (one time, ~500 MB for 'small') ==" -ForegroundColor Cyan
& python -c @"
import sys, pathlib, tomllib
sys.path.insert(0, r'$here')
import pipeline
cfg = pipeline.load_config(pathlib.Path(r'$here\config.toml'))
from faster_whisper import WhisperModel
w = cfg['whisper']
WhisperModel(w.get('model','small'), device=w.get('device','cpu'), compute_type='int8' if w.get('device','cpu')=='cpu' else 'float16')
print('Folders created under', cfg['paths']['root'], '- model ready')
"@
if ($LASTEXITCODE) { throw "Model download / folder setup failed" }

Write-Host "== 5/5 Background task '$taskName' (starts at logon, restarts if it crashes) ==" -ForegroundColor Cyan
$action   = New-ScheduledTaskAction -Execute $pyw -Argument "`"$here\pipeline.py`" run" -WorkingDirectory $here
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew -Priority 7
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host ""
Write-Host "Done - the watcher is running in the background." -ForegroundColor Green
Write-Host "  Drop videos into:   D:\TikTok\01_inbox            (or 01_inbox\@creator\ for credit)"
Write-Host "  Finished parts:     D:\TikTok\03_output\<video>\"
Write-Host "  Log:                D:\TikTok\logs\pipeline.log"
Write-Host "  status.bat / restart.bat in this folder for quick checks."
