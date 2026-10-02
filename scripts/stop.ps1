# Windows: stop every daemon shard and every orphaned agent-launched Chrome. Thin wrapper around `jobagent stop`.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
uv run jobagent stop @args
exit $LASTEXITCODE
