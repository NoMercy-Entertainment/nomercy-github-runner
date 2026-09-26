@echo off
title Windows Activation Script
setlocal EnableDelayedEXpansion


:: Check for administrative rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrative privileges...
    powershell -Command "Start-Process '%~f0' -Verb runAs"
    exit /b
)

cscript "C:\Windows\System32\slmgr.vbs" /ipk MH37W-N47XK-V7XM9-C7227-GCQG9
cscript "C:\Windows\System32\slmgr.vbs" /skms kms9.msguides.com
cscript "C:\Windows\System32\slmgr.vbs" /ato