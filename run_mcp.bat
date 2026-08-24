@echo off
REM Quartermaster - MCP stdio server (normally launched BY the AI client, e.g. Claude/Antigravity/pi)
cd /d "%~dp0"
python -m src.mcp_server
