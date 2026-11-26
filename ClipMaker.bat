@echo off
rem Double-click to open the UI. Drag a video file onto this .bat to queue it.
cd /d "%~dp0"
start "" pythonw app.py %*
