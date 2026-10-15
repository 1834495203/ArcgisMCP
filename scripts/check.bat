@echo off
rem Self check: print the resolved config, start the arcpy worker and ping it.
rem Does not start the MCP server. Keep this file ASCII-only.
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

"%PY%" -m arcgis_mcp --check %*
