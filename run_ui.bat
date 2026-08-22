@echo off
REM VaultMCP - local web UI -> http://localhost:7890
cd /d "%~dp0"
python -m src.server
