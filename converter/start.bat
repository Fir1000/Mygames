@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Mygames Converter

where python >nul 2>nul
if errorlevel 1 (
  echo ไม่พบ Python - ติดตั้งจาก https://www.python.org/downloads/ แล้วติ๊ก "Add python.exe to PATH"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo กำลังเตรียมโปรแกรมครั้งแรก รอสักครู่...
  python -m venv .venv || (pause & exit /b 1)
)

rem Trust the Windows certificate store too (needed when antivirus scans HTTPS).
".venv\Scripts\python.exe" make_certs.py ".venv\cacert.pem"
set "PIP_CERT=%~dp0.venv\cacert.pem"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

rem Upgrade yt-dlp every start: YouTube/TikTok change often and old versions break.
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet --upgrade -r requirements.txt || (
  echo ติดตั้งไม่สำเร็จ ตรวจสอบอินเทอร์เน็ตแล้วลองใหม่
  pause
  exit /b 1
)

".venv\Scripts\python.exe" server.py
pause
