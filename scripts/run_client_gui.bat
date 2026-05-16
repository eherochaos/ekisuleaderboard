@echo off
setlocal
cd /d "%~dp0\.."

if exist ".venv\Scripts\pythonw.exe" (
  ".venv\Scripts\pythonw.exe" -m eiketsu_env.client_gui
) else (
  python -m eiketsu_env.client_gui
)
