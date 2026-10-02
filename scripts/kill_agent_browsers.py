"""Kill every agent-launched Chrome (Chrome binary + agent --user-data-dir only). Used by start.sh / stop.sh."""
from jobagent.browsers import _agent_chrome_pids, kill_tree

print(f"killed {sum(kill_tree(pid, timeout=3) for pid in _agent_chrome_pids())} agent browser processes")
