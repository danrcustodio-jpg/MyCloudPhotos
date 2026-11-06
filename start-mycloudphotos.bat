@echo off
title My Cloud Photos
cd /d "C:\Users\Owner\MyCloudPhotos"

REM If the app is already up, just open the browser
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:8787/' -UseBasicParsing -TimeoutSec 2).StatusCode | Out-Null; exit 0 } catch { exit 1 }"
if %ERRORLEVEL%==0 goto open

echo Starting My Cloud Photos...
start "My Cloud Photos" "%~dp0.venv\Scripts\python.exe" run.py

REM Wait for the server to accept connections
powershell -NoProfile -Command "$ok=$false; 1..30 | ForEach-Object { try { (Invoke-WebRequest -Uri 'http://127.0.0.1:8787/' -UseBasicParsing -TimeoutSec 1).StatusCode | Out-Null; $ok=$true; break } catch { Start-Sleep -Milliseconds 500 } }; if (-not $ok) { exit 1 }"
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo Could not start the server. Check that Z: is mapped and try again.
  pause
  exit /b 1
)

:open
start "" "http://127.0.0.1:8787/"
exit /b 0
