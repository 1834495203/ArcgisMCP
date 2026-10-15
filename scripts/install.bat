@echo off
rem One-shot setup: create .venv, install mcp, register src on sys.path.
rem Needs a Python 3.10+ on PATH to create the virtual environment.
rem Keep this file ASCII-only: cmd.exe parses .bat with the OEM codepage.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

if exist "%PY%" goto deps

echo [1/3] creating virtual environment %VENV% ...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.12 -m venv "%VENV%" 2>nul
  if not exist "%PY%" py -3 -m venv "%VENV%"
) else (
  python -m venv "%VENV%"
)
if not exist "%PY%" (
  echo [arcgis-mcp] failed to create the venv - install Python 3.10+ and put it on PATH
  exit /b 1
)

:deps
echo [2/3] installing dependencies ...
"%PY%" -m pip install -q --index-url %MIRROR% -r "%ROOT%\requirements.txt"
if errorlevel 1 (
  echo [arcgis-mcp] dependency install failed
  exit /b 1
)

echo [3/3] registering the src directory inside the venv ...
> "%VENV%\Lib\site-packages\arcgis_mcp_src.pth" echo %ROOT%\src

"%PY%" -c "import arcgis_mcp, mcp; print('installed:', arcgis_mcp.__file__)"
if errorlevel 1 (
  echo [arcgis-mcp] registration failed - scripts\run-http.bat still works, it sets PYTHONPATH
  exit /b 1
)

echo.
echo next steps:
echo   1. edit %ROOT%\config\datasources.json and add your own data sources
echo   2. run scripts\check.bat to verify the arcpy environment
echo   3. run scripts\run-http.bat to start the MCP server
