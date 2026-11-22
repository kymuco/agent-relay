# Fast daily startup, without CMD or an additional bootstrap Python process.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host "AgentRelay is not installed. First run: .\start.cmd --setup-only" -ForegroundColor Yellow
    exit 1
}
& $python (Join-Path $PSScriptRoot "agent_relay.py")
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
