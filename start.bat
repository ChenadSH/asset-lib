@echo off
rem Start the asset-lib search service.
rem Keep this file ASCII-only: cmd.exe parses .bat files in the OEM codepage
rem (GBK on Chinese Windows), so non-ASCII comments turn into garbage commands.
rem Also avoid a bare ")" inside if-blocks - it closes the block early.
cd /d "%~dp0"

if "%PY%"=="" set PY=python

if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  echo [init] config.yaml created from config.example.yaml
  echo [next] Edit config.yaml and set source_root to your asset folder,
  echo        e.g. E:\ or E:\Design, then run this script again.
  echo        Or just set an env var first:  set ASSET_LIB_SOURCE=E:\
  pause
  exit /b 1
)

echo Starting asset-lib service ... press Ctrl+C to stop.
"%PY%" -m app.cli serve
pause
