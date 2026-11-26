@echo off
rem Restart the background watcher (use after editing config.toml)
schtasks /End /TN "TikTok Clip Maker" >nul 2>&1
timeout /t 2 >nul
schtasks /Run /TN "TikTok Clip Maker"
echo Watcher restarted.
pause
