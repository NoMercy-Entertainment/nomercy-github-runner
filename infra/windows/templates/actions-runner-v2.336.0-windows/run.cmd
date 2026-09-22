@echo off
rem Started by the job host (agent\jobhost.py) inside this runner's Job
rem Object, as the runner's own virtual account, from the runner's reg
rem directory - where the template, this file included, was copied.
rem
rem GitHub's runner ships its own run.cmd, so it lives in `agent\` and this
rem is the one the job host starts. Waits for the registration register.ps1
rem makes, then hands over.
rem
rem No --once: the runner stays, takes job after job, and a drain is the
rem Ctrl+Break the job host passes on - which this runner answers by
rem finishing the job it has and then exiting (design 12.6).
setlocal
cd /d "%~dp0agent"

:wait
if exist ".runner" goto run
rem No `timeout` here: a service has no console for it to read. The path is
rem absolute: the job host gives the runner no PATH to find it by, and a
rem wait that cannot sleep spins at full CPU (2026-09-22).
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 5"
goto wait

:run
call run.cmd
exit /b %ERRORLEVEL%
