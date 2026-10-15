@echo off
rem Generate the sample data set: data\Demo.gdb plus data\shp.
rem Runs the ArcGIS Pro bundled Python because it needs arcpy.
rem Keep this file ASCII-only.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0.."
set "ARCPY_PY=D:\software\arcgispro\bin\Python\envs\arcgispro-py3\python.exe"

if not exist "%ARCPY_PY%" (
  echo [arcgis-mcp] arcpy python not found: %ARCPY_PY%
  exit /b 1
)

"%ARCPY_PY%" "%ROOT%\scripts\make_sample_data.py"
