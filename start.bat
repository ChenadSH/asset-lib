@echo off
chcp 65001 >nul
cd /d "%~dp0"

if "%PY%"=="" set PY=python

if not exist config.yaml (
  copy config.example.yaml config.yaml >nul
  echo 已从 config.example.yaml 生成 config.yaml
  echo 请编辑 config.yaml，把 source_root 改成你的素材目录（例如 E:\ 或 E:\Design），然后重新运行本脚本。
  echo 也可以先设置环境变量：set ASSET_LIB_SOURCE=E:\
  pause
  exit /b 1
)

echo 启动素材库检索服务…
"%PY%" -m app.cli serve
pause
