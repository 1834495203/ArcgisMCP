@echo off
rem Start the MCP server in stdio mode, for clients that only support stdio.
rem Keep this file ASCII-only: cmd.exe parses .bat with the OEM codepage.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\src"

if not exist "%PY%" (
  echo [arcgis-mcp] venv python not found: %PY%
  exit /b 1
)

"%PY%" -m arcgis_mcp --transport stdio %*
