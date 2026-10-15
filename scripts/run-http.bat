@echo off
rem Start the MCP server in HTTP / streamable-http mode.
rem Default endpoint: http://127.0.0.1:8765/mcp
rem Extra args are passed through, e.g.: run-http.bat --port 9000
rem NOTE: keep this file ASCII-only. cmd.exe parses .bat files with the OEM
rem codepage, so non-ASCII bytes here break line parsing.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\src"

if not exist "%PY%" (
  echo [arcgis-mcp] venv python not found: %PY%
  echo [arcgis-mcp] run scripts\install.bat first
  exit /b 1
)

"%PY%" -m arcgis_mcp --transport http --host 127.0.0.1 --port 8765 %*
