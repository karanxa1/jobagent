#!/usr/bin/env bash
# Run the full pipeline in the background, forever (discover -> triage -> apply every daemon.interval_minutes).
# Starts apply.processes daemons (shard 0 also discovers / triages / emails); logs/agent.pid has one pid per line.
# Stop with: scripts/stop.sh      Watch with: tail -f logs/agent.log
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
if [[ -f logs/agent.pid ]]; then
  while read -r p; do
    if [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; then echo "already running (pid $p)"; exit 0; fi
  done < logs/agent.pid
fi
# clear agent browsers orphaned by a crash or a kill -9 of an earlier run
uv run python scripts/kill_agent_browsers.py || true
n=$(uv run python -c "import yaml; print(int((yaml.safe_load(open('config.yaml')).get('apply') or {}).get('processes', 1)))")
: > logs/agent.pid
for ((i = 0; i < n; i++)); do
  # caffeinate keeps the Mac awake while the agent runs
  JOBAGENT_SHARD=$i JOBAGENT_SHARDS=$n nohup caffeinate -i uv run jobagent run --daemon "$@" >> logs/agent.log 2>&1 &
  echo $! >> logs/agent.pid
  # shard 0 requeues jobs a dead run left in progress: let it do that before the others start claiming
  [[ $i -eq 0 && $n -gt 1 ]] && sleep 20
done
echo "started $n daemon(s): $(tr '\n' ' ' < logs/agent.pid); tail -f logs/agent.log"
