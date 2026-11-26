@echo off
rem Show processed videos and the last log lines
cd /d "%~dp0"
python pipeline.py status
echo.
echo ----- last log lines -----
powershell -NoProfile -Command "Get-Content 'D:\TikTok\logs\pipeline.log' -Tail 15 -ErrorAction SilentlyContinue"
pause
