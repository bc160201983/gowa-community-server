@echo off
rem GoW Ascension community server on a Windows PC.
rem   run-windows.bat lan        players on your own network (same router); they find it with Refresh in the launcher
rem   run-windows.bat internet   players anywhere: forward TCP 36101-36102 and UDP 36103 on your router to this PC
rem Needs Python 3.11+ (python.org, or the game package's runtime\python). Allow Python in the Windows Firewall.
setlocal
set MODE=%1
if "%MODE%"=="" set MODE=lan
set PY=python
where python >nul 2>nul || (if exist "%~dp0..\runtime\python\python.exe" set PY="%~dp0..\runtime\python\python.exe")
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do if not defined LANIP set LANIP=%%a
set LANIP=%LANIP: =%
if /i "%MODE%"=="internet" (
  for /f %%p in ('powershell -NoProfile -Command "(Invoke-RestMethod -Uri https://api.ipify.org)"') do set PUBIP=%%p
  echo Public address: %PUBIP%   ^(players type this in the launcher's Server box^)
  %PY% -I "%~dp0server\gowa_server.py" --host 0.0.0.0 --public --advertise %PUBIP% --data "%~dp0data" --log "%~dp0server.log"
) else (
  echo LAN address: %LANIP%   ^(players on your network press Refresh in the launcher and pick it^)
  %PY% -I "%~dp0server\gowa_server.py" --host %LANIP% --lan --name "%COMPUTERNAME% LAN" --data "%~dp0data" --log "%~dp0server.log"
)
