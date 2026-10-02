# Windows: run the full pipeline in the background, forever. Thin wrapper around `jobagent start`.
# Stop with: scripts\stop.ps1    Watch with: Get-Content logs\agent.log -Wait -Tail 50    Check: uv run jobagent daemon-status
# Extra arguments go to `jobagent run --daemon`.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
uv run jobagent start @args
exit $LASTEXITCODE
