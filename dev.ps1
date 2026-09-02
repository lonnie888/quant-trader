# Quant Trader - local dev runner
# Usage (PowerShell):  .\dev.ps1                # smoke test only
#                      .\dev.ps1 -Backtest     # smoke + backtest
#                      .\dev.ps1 -Install      # install deps first
param(
  [switch]$Install,
  [switch]$Backtest,
  [switch]$UpdateData
)

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
Set-Location $ROOT

if ($Install) {
  Write-Host "[dev] creating venv..." -ForegroundColor Cyan
  if (-not (Test-Path ".venv")) { python -m venv .venv }
  & .\.venv\Scripts\python.exe -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  & .\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
}

& .\.venv\Scripts\python.exe -m quant_trader.tests.smoke_test
if ($LASTEXITCODE -ne 0) { Write-Error "smoke test failed"; exit 1 }

if ($UpdateData) {
  Write-Host "[dev] updating data..." -ForegroundColor Cyan
  & .\.venv\Scripts\python.exe -m quant_trader.scripts.update_data
}

if ($Backtest) {
  Write-Host "[dev] running backtest..." -ForegroundColor Cyan
  & .\.venv\Scripts\python.exe -m quant_trader.scripts.run_backtest
  Write-Host "[dev] leaderboard: $ROOT\reports\leaderboard.md" -ForegroundColor Green
}