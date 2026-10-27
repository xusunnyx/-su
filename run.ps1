# 一键启动：准备演示数据 + 起服务（Windows PowerShell）
#   .\run.ps1
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

Write-Host "==> 准备演示数据" -ForegroundColor Cyan
python -m scripts.seed

Write-Host "==> 启动服务 http://127.0.0.1:8000/docs" -ForegroundColor Cyan
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
