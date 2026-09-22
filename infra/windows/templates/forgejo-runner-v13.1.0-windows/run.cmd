@echo off
rem Started by the job host (agent\jobhost.py) inside this runner's Job
rem Object, as the runner's own virtual account, from the runner's reg
rem directory - where the template, this file included, was copied.
rem
rem Waits for the registration the agent makes with register.ps1, then runs
rem forgejo-runner with a shutdown_timeout. Unset or zero, forgejo-runner
rem cancels its jobs the moment it is signalled (its config.example.yaml says
rem so), and the drain's signal is the Ctrl+Break the job host passes on.
setlocal
cd /d "%~dp0"

:wait
if exist ".runner" goto run
rem No `timeout` here: a service has no console for it to read. The path is
rem absolute: the job host gives the runner no PATH to find it by, and a
rem wait that cannot sleep spins at full CPU (2026-09-22).
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 5"
goto wait

:run
> config.yaml echo runner:
>> config.yaml echo   file: %~dp0.runner
>> config.yaml echo   capacity: 1
>> config.yaml echo   timeout: 3h
>> config.yaml echo   shutdown_timeout: 3h
>> config.yaml echo host:
>> config.yaml echo   workdir_parent: %RUNNER_WORK_DIR%
"%~dp0forgejo-runner.exe" daemon --config "%~dp0config.yaml"
exit /b %ERRORLEVEL%
