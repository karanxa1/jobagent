#!/usr/bin/env bash
# Stop every daemon shard AND every agent-launched Chrome (never touches your own Chrome profile).
cd "$(dirname "$0")/.."
if [[ -f logs/agent.pid ]]; then
  while read -r p; do
    [[ -z "$p" ]] && continue
    pkill -TERM -P "$p" 2>/dev/null; kill "$p" 2>/dev/null && echo "stopped $p"
  done < logs/agent.pid
  rm -f logs/agent.pid
fi
uv run python scripts/kill_agent_browsers.py || true
