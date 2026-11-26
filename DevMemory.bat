@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0plugin"
python -m devmem app %*
