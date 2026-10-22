$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) { python -m venv .venv }
& .venv/Scripts/python.exe -c 'import fastapi, uvicorn, langgraph, httpx, playwright' 2>$null
if ($LASTEXITCODE -ne 0) {
    & .venv/Scripts/python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
}
Write-Host 'ShopMate: http://127.0.0.1:8765  |  Ctrl+C 停止'
& .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
