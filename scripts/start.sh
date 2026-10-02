#!/usr/bin/env bash
# Run the full pipeline in the background, forever (discover -> triage -> apply every daemon.interval_minutes).
# Thin wrapper around `jobagent start` (same on Windows: scripts\start.ps1 / start.cmd): starts apply.processes
# daemons (shard 0 also discovers / triages / emails), logs/agent.pid has one pid per line, the machine is kept
# awake while they run. Extra args go to `jobagent run --daemon`.
# Stop with: scripts/stop.sh      Watch with: tail -f logs/agent.log      Check with: uv run jobagent daemon-status
set -uo pipefail
cd "$(dirname "$0")/.."
exec uv run jobagent start "$@"
